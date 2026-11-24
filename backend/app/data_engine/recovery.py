"""Completeness monitoring and recovery (Deliverable 2D). Provider-independent.

One cycle (run by ``app.workers.market_data_recovery``, or once from the CLI)::

    adopt evidence from 2B jobs that predate coverage tracking
    -> translate closed live-stream interruptions into per-series gaps
       (only the parts inside the feed's expected hours; a night outage loses nothing)
    -> completeness scan of the tracked series over the look-back window:
         expected (calendar x feed hours, settled) - coverage evidence - known gaps
           bars:   uncovered -> gap (UNCOVERED)
           trades: collected live -> reconciliation job; not collected -> gap
    -> schedule recovery jobs for settled OPEN gaps (one active job per gap)
    -> run due jobs: one 2B ingestion job per attempt, then *re-evaluate completeness*
         nothing left          -> job COMPLETED, gap RECOVERED
         left, evidence grew   -> PARTIALLY_COMPLETED; the remainder becomes a new gap
         permanent provider no -> FAILED, gap UNAVAILABLE (explicitly unrecoverable)
         transient failure     -> retried with backoff; after the last attempt FAILED and
                                  the gap stays OPEN (still missing, never "recovered")

A job is never COMPLETED because a task ended: only because the coverage evidence now
spans the missing interval. Recovered observations are stored with
``source = HISTORICAL_REST`` and ``available_at`` = the recovery time, so point-in-time
reads never see them before Atlas actually had them.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.config import MarketDataSettings
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.calendar import FeedHours, local_date
from app.data_engine.completeness import (
    EPOCH,
    ONE_MICROSECOND,
    Interval,
    ceil_bar,
    check_trackable,
    expected,
    floor_bar,
    intersect,
    live_bar_coverage,
    merge,
    rest_coverage,
    settled_before,
    subtract,
)
from app.data_engine.enums import (
    CoverageEvidence,
    DataQualityEventType,
    DataQualityStatus,
    DataType,
    GapCause,
    GapRecoveryStatus,
    GapStatus,
    IngestionSymbolStatus,
    PriceAdjustment,
    RecoveryJobKind,
    RecoveryJobStatus,
    Severity,
    StreamChannel,
    StreamGapKind,
)
from app.data_engine.errors import MarketDataError
from app.data_engine.historical import HistoricalFetchRequest, HistoricalIngestionService
from app.data_engine.providers.base import MarketDataProvider
from app.data_engine.timeframes import US_EQUITY_TIMEZONE, Timeframe
from app.database.models import (
    ActorType,
    Asset,
    AssetStatus,
    CoverageRecord,
    DataQualityEventRecord,
    IngestionJobRecord,
    IngestionJobSymbolRecord,
    MappingStatus,
    MarketDataGapRecord,
    RawMarketDataEventRecord,
    RecoveryJobRecord,
    StreamGapRecord,
    StreamSessionRecord,
)
from app.database.repositories import (
    AssetMappingRepository,
    AuditLogRepository,
    CompletenessRepository,
    MarketDataEventRepository,
    RecoveryJobRepository,
    Series,
)
from app.database.sessions import session_scope

logger = get_logger(__name__)

AUDIT_ACTOR = "market_data_recovery"
LIVE_BAR_TIMEFRAME = Timeframe.parse("1Min")
# Errors after which retrying cannot help: the provider will not supply the interval.
PERMANENT_ERROR_CODES = frozenset(
    {
        "unsupported_capability",
        "provider_entitlement_denied",
        "provider_request_rejected",
        "unknown_asset",
        "invalid_provider_symbol",
    }
)
RETRY_BASE = timedelta(minutes=1)
RETRY_MAX = timedelta(hours=1)
# A FAILED job's gap is retried again after this long (it stays OPEN meanwhile).
FAILED_GAP_RETRY_AFTER = timedelta(hours=6)
JOB_LEASE = timedelta(minutes=15)

_CHANNEL_DATA = {
    StreamChannel.BARS.value: DataType.BAR,
    StreamChannel.UPDATED_BARS.value: DataType.BAR,
    StreamChannel.TRADES.value: DataType.TRADE,
    StreamChannel.QUOTES.value: DataType.QUOTE,
}
_STREAM_CAUSE = {
    StreamGapKind.CONNECTION_LOST: GapCause.CONNECTION_LOST,
    StreamGapKind.WORKER_NOT_RUNNING: GapCause.WORKER_NOT_RUNNING,
    StreamGapKind.DATA_DROPPED: GapCause.DATA_DROPPED,
    StreamGapKind.DATA_NOT_PERSISTED: GapCause.DATA_NOT_PERSISTED,
}


@dataclass(frozen=True)
class Tracked:
    """A completeness-tracked series of one configured symbol."""

    series: Series
    symbol: str
    provider_symbol: str

    @property
    def timeframe(self) -> Timeframe | None:
        return None if self.series.timeframe is None else Timeframe.parse(self.series.timeframe)


@dataclass
class CycleReport:
    counts: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)


class RecoveryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: MarketDataProvider,
        settings: MarketDataSettings,
        clock: Clock,
        historical: HistoricalIngestionService | None = None,
        *,
        worker_id: str = "recovery",
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._settings = settings
        self._clock = clock
        self._historical = historical or HistoricalIngestionService(
            session_factory, provider, settings, clock
        )
        self.worker_id = worker_id
        self._hours: FeedHours = provider.feed_access().session_hours
        self._settle = timedelta(seconds=settings.market_data_recovery_settle_seconds)

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    @property
    def feed(self) -> str:
        return self._provider.feed

    # ------------------------------------------------------------ tracked series
    def tracked(self) -> list[Tracked]:
        """Series tracked for MARKET_DATA_SYMBOLS: each recovery timeframe of bars, and
        trades when trades are recovered and streamed."""
        result = []
        adjustment = self._settings.historical_data_adjustment
        streamed = set(self._settings.market_data_stream_channels)
        data_types = set(self._settings.market_data_recovery_data_types)
        with session_scope(self._sessions) as session:
            mappings = AssetMappingRepository(session)
            for symbol in self._settings.market_data_symbols:
                asset = session.scalars(
                    select(Asset).where(Asset.symbol == symbol, Asset.status == AssetStatus.ACTIVE)
                ).one_or_none()
                mapping = (
                    None
                    if asset is None
                    else mappings.current_for_asset(asset.id, self.provider_id)
                )
                if asset is None or mapping is None or mapping.status is not MappingStatus.ACTIVE:
                    logger.warning("recovery.symbol_not_tracked", symbol=symbol)
                    continue
                provider_symbol = self._provider.normalize_symbol(mapping.provider_symbol)
                for text in self._settings.market_data_recovery_timeframes:
                    check_trackable(Timeframe.parse(text))
                    result.append(
                        Tracked(
                            Series(
                                asset.id,
                                self.provider_id,
                                self.feed,
                                DataType.BAR,
                                text,
                                adjustment,
                            ),
                            symbol,
                            provider_symbol,
                        )
                    )
                if DataType.TRADE in data_types and StreamChannel.TRADES in streamed:
                    result.append(
                        Tracked(
                            Series(asset.id, self.provider_id, self.feed, DataType.TRADE),
                            symbol,
                            provider_symbol,
                        )
                    )
        return result

    # ------------------------------------------------------------ cycle
    async def run_cycle(self, *, run_jobs: bool = True, max_jobs: int = 20) -> CycleReport:
        report = CycleReport()
        now = self._clock.now()
        report.counts["coverage_adopted"] += self.adopt_job_coverage()
        report.counts["coverage_adopted"] += self.adopt_session_coverage()
        report.counts["stream_gaps_translated"] += self.translate_stream_gaps(now)
        scanned = self.scan(now)
        report.counts.update(scanned)
        report.counts["jobs_scheduled"] += self.schedule(now)
        if run_jobs:
            for outcome in await self.run_due(max_jobs=max_jobs):
                report.counts[f"jobs_{outcome.lower()}"] += 1
        report.counts["stream_gaps_settled"] += self.settle_stream_gaps(self._clock.now())
        return report

    def recover_interrupted(self) -> int:
        """At start-up (single worker): jobs left RUNNING by a dead worker run again."""
        self._historical.recover_interrupted_jobs()
        with session_scope(self._sessions) as session:
            released = RecoveryJobRepository(session).release_leases(
                provider=self.provider_id,
                feed=self.feed,
                reason="interrupted: the recovery worker stopped during this attempt",
            )
        if released:
            logger.warning("recovery.jobs_released", jobs=released)
        return released

    # ------------------------------------------------------------ evidence from 2B jobs
    def adopt_job_coverage(self, limit: int = 200) -> int:
        """Coverage for finished 2B symbol runs that have none (jobs from before 2D)."""
        adopted = 0
        with session_scope(self._sessions) as session:
            covered = select(CoverageRecord.id).where(
                CoverageRecord.ingestion_job_id == IngestionJobSymbolRecord.job_id,
                CoverageRecord.asset_id == IngestionJobSymbolRecord.asset_id,
            )
            rows = session.execute(
                select(IngestionJobSymbolRecord, IngestionJobRecord)
                .join(IngestionJobRecord, IngestionJobRecord.id == IngestionJobSymbolRecord.job_id)
                .where(
                    IngestionJobRecord.provider == self.provider_id,
                    IngestionJobRecord.feed == self.feed,
                    IngestionJobRecord.started_at.is_not(None),
                    IngestionJobSymbolRecord.status.in_(
                        [IngestionSymbolStatus.COMPLETED, IngestionSymbolStatus.NO_DATA]
                    ),
                    ~exists(covered),
                )
                .limit(limit)
            ).all()
            repo = CompletenessRepository(session)
            for symbol_run, job in rows:
                timeframe = None if job.timeframe is None else Timeframe.parse(job.timeframe)
                if timeframe is not None:
                    try:
                        check_trackable(timeframe)
                    except ValueError:
                        continue
                assert job.started_at is not None  # noqa: S101  # filtered above
                interval = rest_coverage(
                    job.data_type,
                    timeframe,
                    job.range_start,
                    job.range_end,
                    job.started_at,
                    self._settle,
                    self._hours,
                )
                if interval is None:
                    continue
                repo.add_coverage(
                    Series(
                        symbol_run.asset_id,
                        job.provider,
                        job.feed,
                        job.data_type,
                        job.timeframe,
                        job.adjustment if job.data_type is DataType.BAR else None,
                    ),
                    interval,
                    CoverageEvidence.REST_RETRIEVAL,
                    at=self._clock.now(),
                    ingestion_job_id=job.id,
                )
                adopted += 1
        return adopted

    def adopt_session_coverage(self, limit: int = 50) -> int:
        """Live coverage for ended stream sessions that have none (sessions from before
        2D): from the confirmed subscription to the session's last stored frame, minus
        intervals in which frames were dropped. Bars count as evidence; for trades and
        quotes it only marks the interval for reconciliation."""
        adopted = 0
        with session_scope(self._sessions) as session:
            has_coverage = select(CoverageRecord.id).where(
                CoverageRecord.stream_session_id == StreamSessionRecord.id
            )
            sessions = session.scalars(
                select(StreamSessionRecord)
                .where(
                    StreamSessionRecord.provider == self.provider_id,
                    StreamSessionRecord.feed == self.feed,
                    StreamSessionRecord.ended_at.is_not(None),
                    StreamSessionRecord.subscribed_at.is_not(None),
                    ~exists(has_coverage),
                )
                .limit(limit)
            ).all()
            repo = CompletenessRepository(session)
            for stream_session in sessions:
                assert stream_session.subscribed_at is not None  # noqa: S101  # filtered
                last_stored = session.scalar(
                    select(func.max(RawMarketDataEventRecord.received_at)).where(
                        RawMarketDataEventRecord.stream_session_id == stream_session.id
                    )
                )
                if last_stored is None or last_stored <= stream_session.subscribed_at:
                    continue
                dropped = [
                    (g.gap_start, g.gap_end or g.gap_start)
                    for g in session.scalars(
                        select(StreamGapRecord).where(
                            StreamGapRecord.previous_session_id == stream_session.id,
                            StreamGapRecord.kind == StreamGapKind.DATA_DROPPED,
                        )
                    )
                ]
                spans = subtract([(stream_session.subscribed_at, last_stored)], dropped)
                targets = self._targets_for_symbols(
                    session,
                    sorted(
                        {
                            s
                            for values in stream_session.confirmed_subscription.values()
                            for s in values
                        }
                    ),
                )
                confirmed = stream_session.confirmed_subscription
                for provider_symbol, (asset_id, _) in targets.items():
                    for channel, data_type in (
                        (StreamChannel.BARS, DataType.BAR),
                        (StreamChannel.TRADES, DataType.TRADE),
                        (StreamChannel.QUOTES, DataType.QUOTE),
                    ):
                        if provider_symbol not in confirmed.get(channel.value, []):
                            continue
                        timeframe = str(LIVE_BAR_TIMEFRAME) if data_type is DataType.BAR else None
                        series = Series(
                            asset_id,
                            self.provider_id,
                            self.feed,
                            data_type,
                            timeframe,
                            self._settings.historical_data_adjustment if timeframe else None,
                        )
                        for span in spans:
                            interval = (
                                live_bar_coverage(span[0], span[1], LIVE_BAR_TIMEFRAME)
                                if data_type is DataType.BAR
                                else span
                            )
                            if interval is None:
                                continue
                            repo.add_coverage(
                                series,
                                interval,
                                CoverageEvidence.LIVE_STREAM,
                                at=self._clock.now(),
                                stream_session_id=stream_session.id,
                            )
                            adopted += 1
        return adopted

    # ------------------------------------------------------------ live interruptions
    def translate_stream_gaps(self, now: datetime) -> int:
        """Closed stream interruptions -> per-series gaps inside expected hours."""
        tracked = {
            (t.series.asset_id, t.series.data_type, t.series.timeframe): t for t in self.tracked()
        }
        translated = 0
        with session_scope(self._sessions) as session:
            stream_gaps = session.scalars(
                select(StreamGapRecord)
                .where(
                    StreamGapRecord.provider == self.provider_id,
                    StreamGapRecord.feed == self.feed,
                    StreamGapRecord.gap_end.is_not(None),
                    StreamGapRecord.recovery_status == GapRecoveryStatus.PENDING,
                )
                .order_by(StreamGapRecord.gap_start)
                .with_for_update(skip_locked=True)
            ).all()
            for stream_gap in stream_gaps:
                assert stream_gap.gap_end is not None  # noqa: S101  # filtered above
                loss = (
                    min(
                        stream_gap.gap_start, stream_gap.last_message_before or stream_gap.gap_start
                    ),
                    stream_gap.gap_end,
                )
                created = self._series_gaps_for_stream_gap(session, stream_gap, loss, tracked, now)
                stream_gap.recovery_status = (
                    GapRecoveryStatus.IN_PROGRESS if created else GapRecoveryStatus.RECOVERED
                )
                if not created:
                    self._audit(
                        session,
                        "market_data.outage_no_expected_data",
                        "market_data_stream_gap",
                        str(stream_gap.id),
                        reason="the interruption lay outside the feed's expected hours",
                        details={"start": loss[0], "end": loss[1], "kind": stream_gap.kind.value},
                    )
                translated += 1
        return translated

    def _series_gaps_for_stream_gap(
        self,
        session: Session,
        stream_gap: StreamGapRecord,
        loss: Interval,
        tracked: dict[tuple[uuid.UUID, DataType, str | None], Tracked],
        now: datetime,
    ) -> int:
        repo = CompletenessRepository(session)
        data_types = {_CHANNEL_DATA[c] for c in stream_gap.channels if c in _CHANNEL_DATA}
        created = 0
        cause = _STREAM_CAUSE[stream_gap.kind]
        targets = self._targets_for_symbols(session, stream_gap.symbols)
        for symbol, (asset_id, provider_symbol) in targets.items():
            for data_type in sorted(data_types):
                if data_type is DataType.BAR:
                    timeframe: Timeframe | None = LIVE_BAR_TIMEFRAME
                    duration = LIVE_BAR_TIMEFRAME.fixed_duration
                    assert duration is not None  # noqa: S101
                    # Bars published during the loss start one bar earlier.
                    window = (
                        floor_bar(loss[0] - duration, LIVE_BAR_TIMEFRAME),
                        ceil_bar(loss[1], LIVE_BAR_TIMEFRAME),
                    )
                    series = Series(
                        asset_id,
                        self.provider_id,
                        self.feed,
                        DataType.BAR,
                        str(LIVE_BAR_TIMEFRAME),
                        self._settings.historical_data_adjustment,
                    )
                else:
                    timeframe, window = None, loss
                    series = Series(asset_id, self.provider_id, self.feed, data_type)
                parts = expected(data_type, timeframe, window[0], window[1], self._hours)
                if not parts:
                    continue
                repo.lock_series(series)
                gaps = repo.record_gaps(
                    series,
                    provider_symbol,
                    parts,
                    cause,
                    at=now,
                    stream_gap_id=stream_gap.id,
                    details={
                        "stream_gap_kind": stream_gap.kind.value,
                        "loss_start": loss[0].isoformat(),
                        "loss_end": loss[1].isoformat(),
                        "tracked": (asset_id, data_type, series.timeframe) in tracked,
                    },
                )
                created += len(gaps)
                for gap in gaps:
                    self._gap_identified(session, gap, symbol)
        if created:
            self._audit(
                session,
                "market_data.outage_detected",
                "market_data_stream_gap",
                str(stream_gap.id),
                reason=stream_gap.reason[:500],
                details={
                    "kind": stream_gap.kind.value,
                    "start": loss[0],
                    "end": loss[1],
                    "series_gaps": created,
                    "priority": "HIGH" if loss[1] - loss[0] > timedelta(minutes=5) else "NORMAL",
                },
            )
        return created

    def _targets_for_symbols(
        self, session: Session, symbols: list[str]
    ) -> dict[str, tuple[uuid.UUID, str]]:
        """Provider symbols of a stream gap -> (asset, provider symbol) via current mappings."""
        mappings = AssetMappingRepository(session)
        result = {}
        for provider_symbol in symbols:
            history = mappings.history(self.provider_id, provider_symbol)
            current = [m for m in history if m.valid_to is None]
            if current:
                result[provider_symbol] = (current[-1].asset_id, provider_symbol)
        return result

    def settle_stream_gaps(self, now: datetime) -> int:
        """Stream interruptions whose series gaps are all resolved get a final status."""
        settled = 0
        with session_scope(self._sessions) as session:
            in_progress = session.scalars(
                select(StreamGapRecord).where(
                    StreamGapRecord.provider == self.provider_id,
                    StreamGapRecord.feed == self.feed,
                    StreamGapRecord.recovery_status == GapRecoveryStatus.IN_PROGRESS,
                )
            ).all()
            enabled = set(self._settings.market_data_recovery_data_types)
            for stream_gap in in_progress:
                gaps = self._descendants(session, stream_gap.id)
                # Gaps of data types that are not recovered stay OPEN (still missing) but do
                # not keep the interruption pending forever.
                waiting = [
                    g
                    for g in gaps
                    if g.status in (GapStatus.OPEN, GapStatus.RECOVERING) and g.data_type in enabled
                ]
                if not gaps or waiting:
                    continue
                unrecovered = any(g.status is GapStatus.OPEN for g in gaps)
                statuses = {
                    g.status
                    for g in gaps
                    if g.status not in (GapStatus.PARTIALLY_RECOVERED, GapStatus.OPEN)
                }
                if unrecovered:
                    stream_gap.recovery_status = (
                        GapRecoveryStatus.PARTIALLY_RECOVERED
                        if GapStatus.RECOVERED in statuses
                        else GapRecoveryStatus.UNRECOVERABLE
                    )
                elif statuses <= {GapStatus.RECOVERED}:
                    stream_gap.recovery_status = GapRecoveryStatus.RECOVERED
                elif GapStatus.RECOVERED in statuses:
                    stream_gap.recovery_status = GapRecoveryStatus.PARTIALLY_RECOVERED
                else:
                    stream_gap.recovery_status = GapRecoveryStatus.UNRECOVERABLE
                settled += 1
        return settled

    @staticmethod
    def _descendants(session: Session, stream_gap_id: int) -> list[MarketDataGapRecord]:
        gaps = list(
            session.scalars(
                select(MarketDataGapRecord).where(
                    MarketDataGapRecord.stream_gap_id == stream_gap_id
                )
            )
        )
        frontier = [g.id for g in gaps]
        while frontier:
            children = list(
                session.scalars(
                    select(MarketDataGapRecord).where(
                        MarketDataGapRecord.parent_gap_id.in_(frontier)
                    )
                )
            )
            gaps.extend(children)
            frontier = [g.id for g in children]
        return gaps

    # ------------------------------------------------------------ completeness scan
    def scan_bounds(self, now: datetime) -> Interval:
        """From New York midnight ``lookback_days`` ago until now."""
        first_day = local_date(now) - timedelta(
            days=self._settings.market_data_completeness_lookback_days
        )
        start = datetime.combine(first_day, time(0), tzinfo=US_EQUITY_TIMEZONE).astimezone(UTC)
        return start, now

    def evaluate(
        self, tracked: Tracked, bounds: Interval, now: datetime
    ) -> dict[str, list[Interval]]:
        """Completeness of one series over ``bounds`` (read-only).

        ``expected``: settled intervals in which data could exist; ``covered``: evidence;
        ``missing``: expected and not covered; ``unverified`` (trades only): collected live
        but not yet reconciled with the provider's history.
        """
        series, timeframe = tracked.series, tracked.timeframe
        end = min(
            bounds[1], settled_before(series.data_type, timeframe, now, self._settle, self._hours)
        )
        if end <= bounds[0]:
            return {"expected": [], "covered": [], "missing": [], "unverified": []}
        expected_parts = expected(series.data_type, timeframe, bounds[0], end, self._hours)
        with session_scope(self._sessions) as session:
            repo = CompletenessRepository(session)
            rest = repo.covered(series, (bounds[0], end), (CoverageEvidence.REST_RETRIEVAL,))
            live = repo.covered(series, (bounds[0], end), (CoverageEvidence.LIVE_STREAM,))
        if series.data_type is DataType.BAR:
            covered = merge(rest + live)
            assert timeframe is not None  # noqa: S101  # bars always have one
            # Bar starts in [a, b) are those in [ceil(a), ceil(b)): keep bounds on the grid.
            snapped = [
                (ceil_bar(a, timeframe), ceil_bar(b, timeframe))
                for a, b in subtract(expected_parts, covered)
            ]
            return {
                "expected": expected_parts,
                "covered": intersect(covered, (bounds[0], end)),
                "missing": [(a, b) for a, b in snapped if b > a],
                "unverified": [],
            }
        else:  # a live stream can skip trades: only the provider's history proves them
            covered = merge(rest)
            unverified = intersect(subtract(live, rest), (bounds[0], end))
            unverified = [p for e in expected_parts for p in intersect(unverified, e)]
        missing = subtract(subtract(expected_parts, covered), unverified)
        return {
            "expected": expected_parts,
            "covered": intersect(covered, (bounds[0], end)),
            "missing": missing,
            "unverified": unverified,
        }

    def scan(self, now: datetime) -> Counter[str]:
        counts: Counter[str] = Counter()
        bounds = self.scan_bounds(now)
        window = timedelta(minutes=self._settings.market_data_trade_reconciliation_minutes)
        for tracked in self.tracked():
            state = self.evaluate(tracked, bounds, now)
            with session_scope(self._sessions) as session:
                repo = CompletenessRepository(session)
                repo.lock_series(tracked.series)
                gaps = repo.record_gaps(
                    tracked.series,
                    tracked.provider_symbol,
                    state["missing"],
                    GapCause.UNCOVERED,
                    at=now,
                    details={"detected_by": "completeness_scan"},
                )
                for gap in gaps:
                    self._gap_identified(session, gap, tracked.symbol)
                counts["gaps_detected"] += len(gaps)
                # Reconcile whole windows only (one request per window, not per cycle).
                cutoff = EPOCH + ((now - self._settle) - EPOCH) // window * window
                ready = intersect(state["unverified"], (bounds[0], cutoff))
                if ready:
                    counts["reconciliations_scheduled"] += self._schedule_reconciliations(
                        session, tracked, ready, window, now
                    )
        return counts

    def _schedule_reconciliations(
        self,
        session: Session,
        tracked: Tracked,
        intervals: list[Interval],
        window: timedelta,
        now: datetime,
    ) -> int:
        jobs = RecoveryJobRepository(session)
        repo = CompletenessRepository(session)
        busy = [
            (j.missing_start, j.missing_end)
            for j in jobs.active_reconciliations(
                tracked.series, (intervals[0][0], intervals[-1][1])
            )
        ]
        gapped = [
            (g.gap_start, g.gap_end)
            for g in repo.unresolved_gaps(tracked.series, (intervals[0][0], intervals[-1][1]))
        ]
        created = 0
        for start, end in subtract(intervals, busy + gapped):
            cursor = start
            while cursor < end:
                chunk_end = min(end, cursor + window)
                session.add(
                    self._job(
                        tracked, RecoveryJobKind.RECONCILIATION, None, (cursor, chunk_end), now
                    )
                )
                created += 1
                cursor = chunk_end
        session.flush()
        return created

    # ------------------------------------------------------------ scheduling
    def schedule(self, now: datetime) -> int:
        """Recovery jobs for settled OPEN gaps of recovered data types."""
        enabled = set(self._settings.market_data_recovery_data_types)
        created = 0
        with session_scope(self._sessions) as session:
            jobs = RecoveryJobRepository(session)
            open_gaps = session.scalars(
                select(MarketDataGapRecord).where(
                    MarketDataGapRecord.provider == self.provider_id,
                    MarketDataGapRecord.feed == self.feed,
                    MarketDataGapRecord.status == GapStatus.OPEN,
                )
            ).all()
            for gap in open_gaps:
                if gap.data_type not in enabled:
                    if not gap.details.get("recovery_disabled"):
                        gap.details = {**gap.details, "recovery_disabled": True}
                    continue
                timeframe = None if gap.timeframe is None else Timeframe.parse(gap.timeframe)
                if gap.gap_end > settled_before(
                    gap.data_type, timeframe, now, self._settle, self._hours
                ):
                    continue  # the provider may not have published it all yet
                previous = jobs.for_gap(gap.id)
                if previous and (
                    previous[-1].status in (RecoveryJobStatus.PENDING, RecoveryJobStatus.RUNNING)
                    or (
                        previous[-1].status is RecoveryJobStatus.FAILED
                        and previous[-1].completed_at is not None
                        and now - previous[-1].completed_at < FAILED_GAP_RETRY_AFTER
                    )
                ):
                    continue
                tracked = Tracked(
                    Series(
                        gap.asset_id,
                        gap.provider,
                        gap.feed,
                        gap.data_type,
                        gap.timeframe,
                        gap.adjustment,
                    ),
                    gap.provider_symbol,
                    gap.provider_symbol,
                )
                job = jobs.create(
                    self._job(
                        tracked, RecoveryJobKind.RECOVERY, gap.id, (gap.gap_start, gap.gap_end), now
                    )
                )
                if job is not None:
                    created += 1
                    self._audit(
                        session,
                        "market_data.backfill_initiated",
                        "market_data_recovery_job",
                        str(job.id),
                        details={"gap_id": gap.id, "start": gap.gap_start, "end": gap.gap_end},
                    )
        return created

    def _job(
        self,
        tracked: Tracked,
        kind: RecoveryJobKind,
        gap_id: int | None,
        interval: Interval,
        now: datetime,
    ) -> RecoveryJobRecord:
        series = tracked.series
        return RecoveryJobRecord(
            id=uuid.uuid4(),
            kind=kind,
            gap_id=gap_id,
            asset_id=series.asset_id,
            provider=series.provider,
            feed=series.feed,
            provider_symbol=tracked.provider_symbol,
            data_type=series.data_type,
            timeframe=series.timeframe,
            adjustment=series.adjustment,
            missing_start=interval[0],
            missing_end=interval[1],
            requested_start=interval[0],
            requested_end=interval[1] - ONE_MICROSECOND,  # inclusive on the provider side
            status=RecoveryJobStatus.PENDING,
            attempts=0,
            max_attempts=self._settings.market_data_recovery_max_attempts,
            next_attempt_at=now,
            requested_by=f"{AUDIT_ACTOR}:{self.worker_id}"[:64],
            created_at=now,
        )

    def request_recovery(
        self,
        symbol: str,
        data_type: DataType,
        timeframe: Timeframe | None,
        start: datetime,
        end: datetime,
        requested_by: str,
    ) -> list[MarketDataGapRecord]:
        """An operator asks for [start, end) to be verified: the parts without evidence (and
        not already known gaps) become REQUESTED gaps, recovered by the next cycle."""
        tracked = self._tracked_symbol(symbol, data_type, timeframe)
        now = self._clock.now()
        state = self.evaluate(tracked, (start, end), now)
        with session_scope(self._sessions) as session:
            repo = CompletenessRepository(session)
            repo.lock_series(tracked.series)
            gaps = repo.record_gaps(
                tracked.series,
                tracked.provider_symbol,
                state["missing"] + state["unverified"],
                GapCause.REQUESTED,
                at=now,
                details={"requested_by": requested_by[:64]},
            )
            for gap in gaps:
                self._gap_identified(session, gap, symbol)
            session.expunge_all()
        return gaps

    def _tracked_symbol(
        self, symbol: str, data_type: DataType, timeframe: Timeframe | None
    ) -> Tracked:
        if data_type is DataType.BAR:
            if timeframe is None:
                raise ValueError("bars need a timeframe")
            check_trackable(timeframe)
        with session_scope(self._sessions) as session:
            asset = session.scalars(
                select(Asset).where(Asset.symbol == symbol, Asset.status == AssetStatus.ACTIVE)
            ).one_or_none()
            mapping = (
                None
                if asset is None
                else AssetMappingRepository(session).current_for_asset(asset.id, self.provider_id)
            )
            if asset is None or mapping is None or mapping.status is not MappingStatus.ACTIVE:
                raise MarketDataError(
                    f"'{symbol}' is not a registered asset with an active mapping"
                )
            adjustment: PriceAdjustment | None = (
                self._settings.historical_data_adjustment if data_type is DataType.BAR else None
            )
            return Tracked(
                Series(
                    asset.id,
                    self.provider_id,
                    self.feed,
                    data_type,
                    None if timeframe is None else str(timeframe),
                    adjustment,
                ),
                symbol,
                self._provider.normalize_symbol(mapping.provider_symbol),
            )

    # ------------------------------------------------------------ execution
    async def run_due(self, *, max_jobs: int = 20) -> list[str]:
        now = self._clock.now()
        with session_scope(self._sessions) as session:
            claimed = RecoveryJobRepository(session).claim_due(
                provider=self.provider_id,
                feed=self.feed,
                now=now,
                owner=self.worker_id[:64],
                lease_until=now + JOB_LEASE,
                limit=max_jobs,
            )
        outcomes = []
        for job_id in claimed:
            outcomes.append(await self.run_job(job_id))
        return outcomes

    async def run_job(self, job_id: uuid.UUID) -> str:
        """One attempt of a claimed (RUNNING) job; returns the resulting status."""
        with session_scope(self._sessions) as session:
            job = RecoveryJobRepository(session).get(job_id)
            if job is None:
                return "MISSING"
            snapshot = _JobView.of(job)
            if job.gap_id is not None:
                gap = CompletenessRepository(session).get_gap(job.gap_id, lock=True)
                if gap is not None and gap.status is GapStatus.OPEN:
                    gap.status = GapStatus.RECOVERING
                    gap.attempts += 1
        started = self._clock.now()
        logger.info(
            "recovery.job_started",
            job_id=str(job_id),
            kind=snapshot.kind.value,
            data_type=snapshot.data_type.value,
            symbol=snapshot.provider_symbol,
            start=snapshot.missing[0].isoformat(),
            end=snapshot.missing[1].isoformat(),
            attempt=snapshot.attempts,
        )
        ingestion_id: uuid.UUID | None = None
        error: tuple[str, str] | None = None
        counters: dict[str, int] = {}
        try:
            created = self._historical.create_job(
                HistoricalFetchRequest(
                    asset_ids=(snapshot.asset_id,),
                    data_type=snapshot.data_type,
                    timeframe=None
                    if snapshot.timeframe is None
                    else Timeframe.parse(snapshot.timeframe),
                    start=snapshot.requested[0],
                    end=snapshot.requested[1],
                ),
                requested_by=f"recovery:{job_id}"[:64],
            )
            ingestion_id = created.job_id
            result = await self._historical.run_job(ingestion_id)
            symbol_result = result.symbols[0]
            counters = {
                "records_retrieved": symbol_result.counters.records_retrieved,
                "records_recovered": symbol_result.counters.records_stored,
                "records_duplicate": symbol_result.counters.records_duplicate,
                "records_revised": symbol_result.counters.records_revised,
                "records_rejected": symbol_result.counters.records_rejected,
            }
            if symbol_result.status is IngestionSymbolStatus.FAILED:
                # The 2B symbol error reads "<code>: <message>".
                code, _, message = (
                    symbol_result.error or "provider_error: retrieval failed"
                ).partition(": ")
                error = (code, message or "retrieval failed")
        except MarketDataError as exc:
            error = (exc.code, exc.message)
        except Exception as exc:  # recorded and retried; never reported as recovered
            logger.exception("recovery.job_crashed", job_id=str(job_id))
            error = ("internal_error", f"unexpected error: {type(exc).__name__}")
        return self._finish(job_id, snapshot, ingestion_id, counters, error, started)

    def _finish(
        self,
        job_id: uuid.UUID,
        snapshot: _JobView,
        ingestion_id: uuid.UUID | None,
        counters: dict[str, int],
        error: tuple[str, str] | None,
        started: datetime,
    ) -> str:
        now = self._clock.now()
        tracked = snapshot.tracked()
        state = self.evaluate(tracked, snapshot.missing, now)
        remaining = state["missing"] + state["unverified"]
        # Parts of the interval that are not yet settled are not a failure: they wait.
        verified_any = bool(
            intersect(state["covered"], snapshot.missing) and ingestion_id is not None
        )
        with session_scope(self._sessions) as session:
            jobs = RecoveryJobRepository(session)
            job = jobs.get(job_id, lock=True)
            assert job is not None  # noqa: S101
            if ingestion_id is not None:
                job.ingestion_job_ids = [*job.ingestion_job_ids, ingestion_id]
            for name, value in counters.items():
                setattr(job, name, getattr(job, name) + value)
            job.remaining = [{"start": s.isoformat(), "end": e.isoformat()} for s, e in remaining]
            job.lease_owner, job.lease_expires_at = None, None
            gap = (
                None
                if job.gap_id is None
                else CompletenessRepository(session).get_gap(job.gap_id, lock=True)
            )
            if gap is not None:
                for name, value in counters.items():
                    setattr(gap, name, getattr(gap, name) + value)
            permanent = error is not None and error[0] in PERMANENT_ERROR_CODES
            if not remaining:
                status = RecoveryJobStatus.COMPLETED
                job.error = None  # earlier failed attempts are in the log and attempts count
                self._recovered(session, job, gap, now)
                if job.records_rejected:
                    # Retrieved in full, but some records were invalid: those intervals have
                    # no valid observation. Retrying would return the same records.
                    status = RecoveryJobStatus.PARTIALLY_COMPLETED
                    if gap is not None:
                        gap.status = GapStatus.PARTIALLY_RECOVERED
                        gap.resolution = (
                            f"{gap.resolution}; {job.records_rejected} record(s) were invalid "
                            "and rejected (see quality events): those intervals have no valid "
                            "observation"
                        )[:2000]
            elif error is None or verified_any or permanent:
                status = (
                    RecoveryJobStatus.PARTIALLY_COMPLETED
                    if verified_any
                    else RecoveryJobStatus.FAILED
                )
                self._remaining(session, job, gap, remaining, error, permanent, now)
            elif job.attempts < job.max_attempts:
                status = RecoveryJobStatus.PENDING
                delay = min(RETRY_MAX, RETRY_BASE * 2 ** max(0, job.attempts - 1))
                job.next_attempt_at = now + delay
                job.error = redact_text(f"{error[0]}: {error[1]}")[:2000]
                if gap is not None:
                    gap.status = GapStatus.OPEN  # waits for the retry; still missing
                logger.warning(
                    "recovery.job_retry",
                    job_id=str(job_id),
                    attempt=job.attempts,
                    retry_in_seconds=delay.total_seconds(),
                    code=error[0],
                )
            else:
                status = RecoveryJobStatus.FAILED
                job.error = redact_text(f"{error[0]}: {error[1]}")[:2000]
                if gap is not None:
                    gap.status = GapStatus.OPEN
                    gap.resolution = (
                        f"recovery failed after {job.attempts} attempt(s): {job.error}"[:2000]
                    )
                self._audit(
                    session,
                    "market_data.backfill_failed",
                    "market_data_recovery_job",
                    str(job.id),
                    reason=job.error,
                    details={"gap_id": job.gap_id, "attempts": job.attempts, "priority": "HIGH"},
                )
            job.status = status
            if status is not RecoveryJobStatus.PENDING:
                job.completed_at = now
            duration = (now - started).total_seconds()
            details = {**counters, "duration_seconds": round(duration, 3)}
            logger.info(
                "recovery.job_finished",
                job_id=str(job_id),
                status=status.value,
                remaining=len(remaining),
                **details,
            )
            return status.value

    def _recovered(
        self,
        session: Session,
        job: RecoveryJobRecord,
        gap: MarketDataGapRecord | None,
        now: datetime,
    ) -> None:
        interval = (job.missing_start, job.missing_end)
        if job.kind is RecoveryJobKind.RECONCILIATION:
            if job.records_recovered > 0:
                # The provider's history held observations the live stream never delivered.
                repo = CompletenessRepository(session)
                missed = MarketDataGapRecord(
                    asset_id=job.asset_id,
                    provider=job.provider,
                    feed=job.feed,
                    provider_symbol=job.provider_symbol,
                    data_type=job.data_type,
                    timeframe=job.timeframe,
                    adjustment=job.adjustment,
                    gap_start=job.missing_start,
                    gap_end=job.missing_end,
                    cause=GapCause.STREAM_MISSED_DATA,
                    status=GapStatus.RECOVERED,
                    detected_at=now,
                    resolved_at=now,
                    attempts=job.attempts,
                    records_retrieved=job.records_retrieved,
                    records_recovered=job.records_recovered,
                    records_duplicate=job.records_duplicate,
                    records_revised=job.records_revised,
                    records_rejected=job.records_rejected,
                    resolution=(
                        f"live collection lacked {job.records_recovered} "
                        f"{job.data_type.value.lower()}(s) that the provider's history holds "
                        "(not delivered by the stream, or received but lost before they were "
                        "written); retrieved and stored"
                    ),
                    details={"reconciliation_job_id": str(job.id)},
                )
                repo.lock_series(
                    Series(job.asset_id, job.provider, job.feed, job.data_type, job.timeframe)
                )
                session.add(missed)
                session.flush()
                self._quality(
                    session,
                    job,
                    DataQualityEventType.STREAM_MISSED_DATA,
                    Severity.WARNING,
                    missed.resolution or "",
                    DataQualityStatus.RECOVERED,
                )
                self._audit(
                    session,
                    "market_data.stream_missed_data",
                    "market_data_gap",
                    str(missed.id),
                    reason=missed.resolution,
                    details={
                        "job_id": str(job.id),
                        "start": interval[0],
                        "end": interval[1],
                        "priority": "HIGH",
                    },
                )
            return
        if gap is None:
            return
        gap.status = GapStatus.RECOVERED
        gap.resolved_at = now
        gap.resolution = (
            f"provider history for the interval retrieved in full: {gap.records_recovered} "
            f"new, {gap.records_duplicate} already stored"
            + ("" if gap.records_retrieved else " (the provider has no observations in it)")
        )
        self._quality(
            session,
            job,
            DataQualityEventType.RECOVERY,
            Severity.INFO,
            gap.resolution,
            DataQualityStatus.RECOVERED,
        )
        self._audit(
            session,
            "market_data.backfill_succeeded",
            "market_data_gap",
            str(gap.id),
            reason=gap.resolution,
            details={"job_id": str(job.id), "start": interval[0], "end": interval[1]},
        )

    def _remaining(
        self,
        session: Session,
        job: RecoveryJobRecord,
        gap: MarketDataGapRecord | None,
        remaining: list[Interval],
        error: tuple[str, str] | None,
        permanent: bool,
        now: datetime,
    ) -> None:
        """An attempt left part of the interval unverified.

        * nothing verified, transient -> the gap stays OPEN (retried later);
        * nothing verified, permanent -> the gap is UNAVAILABLE;
        * part verified -> the gap is PARTIALLY_RECOVERED and the remainder becomes new
          gap(s): OPEN, or UNAVAILABLE after a permanent provider refusal.
        """
        reason = None if error is None else redact_text(f"{error[0]}: {error[1]}")[:2000]
        job.error = reason
        if gap is None:  # an unfinished reconciliation: the next scan schedules it again
            return
        bounds = (gap.gap_start, gap.gap_end)
        verified = subtract([bounds], remaining)
        if not verified:
            if permanent:
                gap.status = GapStatus.UNAVAILABLE
                gap.resolved_at = now
                gap.resolution = reason
                self._unrecoverable(session, job, gap, reason)
            else:
                gap.status = GapStatus.OPEN
                gap.resolution = reason or "retrieval produced no completeness evidence"
            return
        series = Series(
            job.asset_id, job.provider, job.feed, job.data_type, job.timeframe, job.adjustment
        )
        repo = CompletenessRepository(session)
        repo.lock_series(series)
        gap.status = GapStatus.PARTIALLY_RECOVERED
        gap.resolved_at = now
        gap.resolution = (
            f"verified {len(verified)} part(s); {len(remaining)} part(s) remain"
            + (f" ({reason})" if reason else "")
        )[:2000]
        session.flush()  # the parent no longer blocks its remainder (exclusion constraint)
        children = repo.record_gaps(
            series,
            gap.provider_symbol,
            intersect(remaining, bounds),
            gap.cause,
            at=now,
            stream_gap_id=gap.stream_gap_id,
            parent_gap_id=gap.id,
            details={"parent_gap_id": gap.id},
        )
        for child in children:
            if permanent:
                child.status = GapStatus.UNAVAILABLE
                child.resolved_at = now
                child.resolution = reason
                self._unrecoverable(session, job, child, reason)
        self._audit(
            session,
            "market_data.backfill_partially_succeeded",
            "market_data_gap",
            str(gap.id),
            reason=gap.resolution,
            details={"job_id": str(job.id), "remaining_gaps": [c.id for c in children]},
        )

    def _unrecoverable(
        self, session: Session, job: RecoveryJobRecord, gap: MarketDataGapRecord, reason: str | None
    ) -> None:
        message = f"the provider cannot supply this interval: {reason or 'unknown reason'}"
        self._quality(
            session,
            job,
            DataQualityEventType.UNRECOVERABLE_INTERVAL,
            Severity.ERROR,
            message,
            DataQualityStatus.UNAVAILABLE,
            interval=(gap.gap_start, gap.gap_end),
        )
        self._audit(
            session,
            "market_data.interval_unrecoverable",
            "market_data_gap",
            str(gap.id),
            reason=message,
            details={"job_id": str(job.id), "priority": "HIGH"},
        )

    # ------------------------------------------------------------ records
    def _gap_identified(self, session: Session, gap: MarketDataGapRecord, symbol: str) -> None:
        message = (
            f"{symbol} {gap.data_type.value.lower()}"
            f"{' ' + gap.timeframe if gap.timeframe else ''} missing "
            f"{gap.gap_start.isoformat()} - {gap.gap_end.isoformat()} ({gap.cause.value})"
        )
        session.add(
            DataQualityEventRecord(
                event_type=DataQualityEventType.MISSING_INTERVAL,
                severity=Severity.WARNING,
                provider=gap.provider,
                feed=gap.feed,
                asset_id=gap.asset_id,
                provider_symbol=gap.provider_symbol,
                data_type=gap.data_type,
                timeframe=gap.timeframe,
                interval_start=gap.gap_start,
                interval_end=gap.gap_end,
                detected_at=gap.detected_at,
                resulting_status=DataQualityStatus.MISSING,
                message=message,
                details={"gap_id": gap.id, "cause": gap.cause.value},
            )
        )
        self._audit(
            session,
            "market_data.gap_identified",
            "market_data_gap",
            str(gap.id),
            reason=message,
            details={"cause": gap.cause.value, "start": gap.gap_start, "end": gap.gap_end},
        )

    def _quality(
        self,
        session: Session,
        job: RecoveryJobRecord,
        event_type: DataQualityEventType,
        severity: Severity,
        message: str,
        status: DataQualityStatus,
        interval: Interval | None = None,
    ) -> None:
        MarketDataEventRepository(session).add_quality_events(
            [
                DataQualityEventRecord(
                    event_type=event_type,
                    severity=severity,
                    provider=job.provider,
                    feed=job.feed,
                    asset_id=job.asset_id,
                    provider_symbol=job.provider_symbol,
                    data_type=job.data_type,
                    timeframe=job.timeframe,
                    interval_start=(interval or (job.missing_start, job.missing_end))[0],
                    interval_end=(interval or (job.missing_start, job.missing_end))[1],
                    detected_at=self._clock.now(),
                    resulting_status=status,
                    message=message[:2000],
                    details={"recovery_job_id": str(job.id), "gap_id": job.gap_id},
                    ingestion_job_id=job.ingestion_job_ids[-1] if job.ingestion_job_ids else None,
                )
            ]
        )

    def _audit(
        self,
        session: Session,
        action: str,
        entity_type: str,
        entity_id: str,
        *,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        AuditLogRepository(session).append(
            actor_type=ActorType.SUBSYSTEM,
            actor_id=AUDIT_ACTOR,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            reason=reason,
            details={
                "provider": self.provider_id,
                "feed": self.feed,
                "priority": "NORMAL",
                **{
                    k: v.isoformat() if isinstance(v, datetime) else v
                    for k, v in (details or {}).items()
                },
            },
        )


@dataclass(frozen=True)
class _JobView:
    """Detached copy of a job's identity for use outside its transaction."""

    id: uuid.UUID
    kind: RecoveryJobKind
    asset_id: uuid.UUID
    provider: str
    feed: str
    provider_symbol: str
    data_type: DataType
    timeframe: str | None
    adjustment: PriceAdjustment | None
    missing: Interval
    requested: Interval
    attempts: int

    @classmethod
    def of(cls, job: RecoveryJobRecord) -> _JobView:
        return cls(
            id=job.id,
            kind=job.kind,
            asset_id=job.asset_id,
            provider=job.provider,
            feed=job.feed,
            provider_symbol=job.provider_symbol,
            data_type=job.data_type,
            timeframe=job.timeframe,
            adjustment=job.adjustment,
            missing=(job.missing_start, job.missing_end),
            requested=(job.requested_start, job.requested_end),
            attempts=job.attempts,
        )

    def tracked(self) -> Tracked:
        return Tracked(
            Series(
                self.asset_id,
                self.provider,
                self.feed,
                self.data_type,
                self.timeframe,
                self.adjustment,
            ),
            self.provider_symbol,
            self.provider_symbol,
        )


__all__ = ["AUDIT_ACTOR", "CycleReport", "RecoveryService", "Tracked"]
