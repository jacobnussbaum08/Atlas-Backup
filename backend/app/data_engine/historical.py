"""Historical bar ingestion (Deliverable 2B).

Pipeline per job::

    HistoricalFetchRequest (Atlas symbols/assets, provider-independent)
      -> resolve assets and current provider mappings, check capabilities (no I/O)
      -> job + per-symbol rows (PENDING)
      -> per symbol, sequentially: provider.fetch_bars() pages
           -> one transaction per page:
                raw response (verbatim) -> request-consistency rejections
                -> quality assessment -> dedupe/revision against stored bars
                -> bars + quality events + counters
      -> per-symbol status (COMPLETED / NO_DATA / FAILED) -> job status

Guarantees:
* Idempotent: re-fetching identical bars stores nothing new (counted as duplicates).
  A changed bar becomes a new revision; the earlier revision is kept.
* Atomic per page: an interruption never leaves a page half-written. Re-running the
  same range is safe.
* Honest status: a job is COMPLETED only if every symbol retrieved every page;
  PARTIALLY_COMPLETED / FAILED name the failed symbols and the reason.
* The feed is the configured one; requesting another feed is an error and is
  recorded as a capability limitation, never a silent switch.

Deliverable 2D:
* Trades and quotes run through the same jobs (``data_type``), stored with the live
  observations' identity, so history overlapping what the stream delivered is counted as
  duplicates and only what the stream missed is stored.
* Each symbol that retrieves its range in full records **coverage evidence**
  (``market_data_coverage``) for the settled part of the range; a symbol that fails part
  way records evidence only up to the last observation it received (pages are
  contiguous in time). Completeness and recovery are judged from this evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.config import MarketDataSettings
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.completeness import (
    ONE_MICROSECOND,
    UnsupportedTimeframeError,
    check_trackable,
    rest_coverage,
)
from app.data_engine.enums import (
    CoverageEvidence,
    DataQualityEventType,
    DataQualityStatus,
    DataType,
    IngestionJobStatus,
    IngestionSymbolStatus,
    PriceAdjustment,
    Severity,
)
from app.data_engine.errors import MarketDataError, UnsupportedCapabilityError
from app.data_engine.live import quote_row, trade_row
from app.data_engine.providers.base import (
    BarPage,
    HistoricalBarsRequest,
    HistoricalObservationsRequest,
    MarketDataProvider,
    ObservationPage,
)
from app.data_engine.schemas import (
    DataIngestionJob,
    IngestionCounters,
    IngestionSymbolResult,
    ProviderBar,
    UtcDatetime,
)
from app.data_engine.timeframes import Timeframe, TimeframeField, bar_interval_end
from app.data_engine.validation import (
    QualityIssue,
    assess_bar,
    assess_quote,
    assess_trade,
    check_request_consistency,
    same_observation,
)
from app.database.models import (
    Asset,
    AssetStatus,
    DataQualityEventRecord,
    IngestionJobRecord,
    IngestionJobSymbolRecord,
    MappingStatus,
    RawMarketDataEventRecord,
)
from app.database.repositories import (
    AssetMappingRepository,
    CompletenessRepository,
    IngestionJobRepository,
    LiveObservationRepository,
    MarketBarRepository,
    MarketDataEventRepository,
    Series,
    SeriesKey,
)
from app.database.sessions import session_scope

logger = get_logger(__name__)

MAX_SYMBOLS_PER_JOB = 20
INTERRUPTED_REASON = "interrupted: the process running this job stopped before it finished"
# Several processes run ingestion jobs (API, recovery worker, discovery worker). A running
# job renews ``heartbeat_at`` every HEARTBEAT_SECONDS; only a job whose heartbeat is older
# than STALE_AFTER is presumed orphaned, so a restart never fails another process's job.
HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(minutes=5)


class UnknownAssetError(MarketDataError):
    code = "unknown_asset"
    http_status = 404


class IngestionJobStateError(MarketDataError):
    code = "ingestion_job_state"
    http_status = 409


class HistoricalFetchRequest(BaseModel):
    """Provider-independent request for historical bars."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = ()
    asset_ids: tuple[uuid.UUID, ...] = ()
    # Bars need a timeframe; trades and quotes (2D recovery) have none.
    data_type: DataType = DataType.BAR
    timeframe: TimeframeField | None = None
    start: UtcDatetime
    end: UtcDatetime
    # Optional: must equal the configured feed. Atlas never switches feeds per request.
    feed: str | None = None
    # Optional price adjustment for bars (default: HISTORICAL_DATA_ADJUSTMENT). A different
    # adjustment is a different series; it never mixes with the default one.
    adjustment: PriceAdjustment | None = None

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if bool(self.symbols) == bool(self.asset_ids):
            raise ValueError("give either symbols or asset_ids")
        targets = self.symbols or self.asset_ids
        if len(targets) > MAX_SYMBOLS_PER_JOB:
            raise ValueError(f"at most {MAX_SYMBOLS_PER_JOB} assets per job")
        if len(set(targets)) != len(targets):
            raise ValueError("duplicate assets in request")
        if any(s != s.strip().upper() or not s for s in self.symbols):
            raise ValueError("symbols must be upper-case Atlas symbols")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if (self.data_type is DataType.BAR) != (self.timeframe is not None):
            raise ValueError("bars need a timeframe; trades and quotes take none")
        return self


@dataclass(frozen=True)
class _Job:
    id: uuid.UUID
    provider: str
    feed: str
    data_type: DataType
    timeframe: Timeframe | None
    adjustment: PriceAdjustment
    start: datetime
    end: datetime

    @classmethod
    def of(cls, record: IngestionJobRecord) -> _Job:
        return cls(
            id=record.id,
            provider=record.provider,
            feed=record.feed,
            data_type=record.data_type,
            timeframe=None if record.timeframe is None else Timeframe.parse(record.timeframe),
            adjustment=record.adjustment,
            start=record.range_start,
            end=record.range_end,
        )

    @property
    def bar_timeframe(self) -> Timeframe:
        if self.timeframe is None:
            raise ValueError("not a bar job")
        return self.timeframe


@dataclass
class _SymbolRun:
    provider_symbol: str
    asset_id: uuid.UUID
    quote_currency: str
    mapping_periods: list[tuple[datetime, datetime | None, uuid.UUID]]
    last_bar: ProviderBar | None = None
    retrieved: int = 0
    # When this symbol's retrieval started (completeness evidence counts from here).
    started_at: datetime | None = None
    # Newest event time stored or confirmed duplicate so far (partial evidence).
    last_event_at: datetime | None = None

    def mapped_to_asset(self, at: datetime) -> bool:
        return any(
            start <= at and (end is None or at < end) and asset_id == self.asset_id
            for start, end, asset_id in self.mapping_periods
        )


@dataclass
class _PageOutcome:
    counts: Counter[str] = field(default_factory=Counter)
    events: list[DataQualityEventRecord] = field(default_factory=list)


def _values(bar: Any) -> dict[str, str | None]:
    def text(value: Any) -> str | None:
        return None if value is None else str(value)

    return {
        "open": text(bar.open),
        "high": text(bar.high),
        "low": text(bar.low),
        "close": text(bar.close),
        "volume": text(bar.volume),
        "vwap": text(bar.vwap),
        "trade_count": text(bar.trade_count),
        "is_complete": text(bar.is_complete),
    }


class HistoricalIngestionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: MarketDataProvider,
        settings: MarketDataSettings,
        clock: Clock,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._settings = settings
        self._clock = clock
        self._heartbeat_seconds = heartbeat_seconds

    # ------------------------------------------------------------ job creation
    def create_job(self, request: HistoricalFetchRequest, requested_by: str) -> DataIngestionJob:
        provider_id, feed = self._provider.provider_id, self._provider.feed
        adjustment = request.adjustment or self._settings.historical_data_adjustment
        if request.feed is not None and request.feed != feed:
            message = (
                f"requested feed '{request.feed}' but the configured feed is '{feed}'; "
                "Atlas never switches feeds per request"
            )
            self._record_limitation(request.feed, request.timeframe, message, request.data_type)
            raise UnsupportedCapabilityError(message)

        with session_scope(self._sessions) as session:
            targets = self._resolve_targets(session, request, provider_id)

        try:
            if request.timeframe is not None:
                self._provider.validate_historical_request(
                    HistoricalBarsRequest(
                        provider_symbols=tuple(symbol for symbol, _ in targets),
                        timeframe=request.timeframe,
                        start=request.start,
                        end=request.end,
                        adjustment=adjustment,
                        page_size=self._settings.historical_data_batch_size,
                    )
                )
            else:
                self._provider.validate_observations_request(
                    HistoricalObservationsRequest(
                        provider_symbols=tuple(symbol for symbol, _ in targets),
                        data_type=request.data_type,
                        start=request.start,
                        end=request.end,
                    )
                )
        except UnsupportedCapabilityError as exc:
            self._record_limitation(feed, request.timeframe, exc.message, request.data_type)
            raise

        with session_scope(self._sessions) as session:
            record = IngestionJobRepository(session).create(
                IngestionJobRecord(
                    id=uuid.uuid4(),
                    provider=provider_id,
                    feed=feed,
                    adjustment=adjustment,
                    data_type=request.data_type,
                    timeframe=None if request.timeframe is None else str(request.timeframe),
                    range_start=request.start,
                    range_end=request.end,
                    requested_symbols=[symbol for symbol, _ in targets],
                    status=IngestionJobStatus.PENDING,
                    # A fresh job is alive; it goes stale only if nobody runs it.
                    heartbeat_at=self._clock.now(),
                    requested_by=requested_by,
                ),
                targets,
            )
            job_id = record.id
        logger.info(
            "historical.job_created",
            job_id=str(job_id),
            provider=provider_id,
            feed=feed,
            data_type=request.data_type.value,
            timeframe=None if request.timeframe is None else str(request.timeframe),
            symbols=[s for s, _ in targets],
        )
        return self.get_job(job_id)

    def _resolve_targets(
        self, session: Session, request: HistoricalFetchRequest, provider_id: str
    ) -> list[tuple[str, uuid.UUID]]:
        asset_classes = list(self._provider.capabilities().asset_classes)
        mappings = AssetMappingRepository(session)
        assets: list[Asset] = []
        for symbol in request.symbols:
            found = session.scalars(
                select(Asset).where(
                    Asset.symbol == symbol,
                    Asset.asset_class.in_(asset_classes),
                    Asset.status == AssetStatus.ACTIVE,
                )
            ).all()
            if not found:
                raise UnknownAssetError(
                    f"'{symbol}' is not a registered active asset for {provider_id}; "
                    "register it from reference data first"
                )
            if len(found) > 1:
                raise UnknownAssetError(f"'{symbol}' is ambiguous: several assets use it")
            assets.append(found[0])
        for asset_id in request.asset_ids:
            asset = session.get(Asset, asset_id)
            if asset is None or asset.status is not AssetStatus.ACTIVE:
                raise UnknownAssetError(f"asset {asset_id} is not a registered active asset")
            assets.append(asset)

        targets = []
        for asset in assets:
            mapping = mappings.current_for_asset(asset.id, provider_id)
            if mapping is None or mapping.status is not MappingStatus.ACTIVE:
                raise UnknownAssetError(
                    f"'{asset.symbol}' has no active {provider_id} symbol mapping"
                )
            targets.append((self._provider.normalize_symbol(mapping.provider_symbol), asset.id))
        return targets

    # ------------------------------------------------------------ job execution
    async def run_job(self, job_id: uuid.UUID) -> DataIngestionJob:
        with session_scope(self._sessions) as session:
            repo = IngestionJobRepository(session)
            record = repo.get(job_id, lock=True)
            if record is None:
                raise IngestionJobStateError(f"ingestion job {job_id} does not exist")
            if record.status is not IngestionJobStatus.PENDING:
                raise IngestionJobStateError(
                    f"job {job_id} is {record.status.value}; only PENDING jobs can run"
                )
            now = self._clock.now()
            record.status = IngestionJobStatus.RUNNING
            record.started_at = now
            record.heartbeat_at = now
            job = _Job.of(record)
            symbols = [(s.provider_symbol, s.asset_id) for s in repo.symbols(job_id)]

        reclaimed = asyncio.Event()
        work = asyncio.ensure_future(self._run_symbols(job, symbols))
        beat = asyncio.create_task(self._keep_alive(job_id, work, reclaimed))
        try:
            await work
        except asyncio.CancelledError:
            if not reclaimed.is_set():
                raise
            # Another process declared this job orphaned; its status is final now.
            logger.error("historical.job_reclaimed", job_id=str(job_id))
            raise IngestionJobStateError(
                f"job {job_id} was marked interrupted while running; stopped"
            ) from None
        except Exception as exc:
            logger.exception("historical.job_crashed", job_id=str(job_id))
            self._finish_job(job_id, crash=f"unexpected error: {type(exc).__name__}")
            raise
        finally:
            beat.cancel()
        self._finish_job(job_id)
        result = self.get_job(job_id)
        logger.info(
            "historical.job_finished",
            job_id=str(job_id),
            status=result.status.value,
            stored=result.counters.records_stored,
            duplicate=result.counters.records_duplicate,
            rejected=result.counters.records_rejected,
            pages=result.counters.pages_retrieved,
        )
        return result

    async def _run_symbols(self, job: _Job, symbols: list[tuple[str, uuid.UUID]]) -> None:
        for provider_symbol, asset_id in symbols:
            await self._run_symbol(job, provider_symbol, asset_id)

    async def _keep_alive(
        self, job_id: uuid.UUID, work: asyncio.Future[None], reclaimed: asyncio.Event
    ) -> None:
        """Renew the heartbeat; stop ``work`` if the job is no longer RUNNING."""
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                alive = await asyncio.to_thread(self._touch, job_id)
            except SQLAlchemyError as exc:
                logger.warning("historical.heartbeat_failed", error_type=type(exc).__name__)
                continue
            if not alive:
                reclaimed.set()
                work.cancel()
                return

    def _touch(self, job_id: uuid.UUID) -> bool:
        with session_scope(self._sessions) as session:
            return IngestionJobRepository(session).touch(job_id, self._clock.now())

    async def _run_symbol(self, job: _Job, provider_symbol: str, asset_id: uuid.UUID) -> None:
        with session_scope(self._sessions) as session:
            asset = session.get(Asset, asset_id)
            assert asset is not None  # noqa: S101  # FK guarantees existence
            history = AssetMappingRepository(session).history(job.provider, provider_symbol)
            run = _SymbolRun(
                provider_symbol=provider_symbol,
                asset_id=asset_id,
                quote_currency=asset.quote_currency,
                mapping_periods=[(m.valid_from, m.valid_to, m.asset_id) for m in history],
                started_at=self._clock.now(),
            )
            IngestionJobRepository(session).set_symbol_status(
                job.id, provider_symbol, IngestionSymbolStatus.RUNNING
            )

        try:
            if job.timeframe is not None:
                request = HistoricalBarsRequest(
                    provider_symbols=(provider_symbol,),
                    timeframe=job.timeframe,
                    start=job.start,
                    end=job.end,
                    adjustment=job.adjustment,
                    page_size=self._settings.historical_data_batch_size,
                )
                async for page in self._provider.fetch_bars(request):
                    self._persist_page(job, run, page)
            else:
                observations = HistoricalObservationsRequest(
                    provider_symbols=(provider_symbol,),
                    data_type=job.data_type,
                    start=job.start,
                    end=job.end,
                )
                async for observation_page in self._provider.fetch_observations(observations):
                    self._persist_observations(job, run, observation_page)
        except MarketDataError as exc:
            self._fail_symbol(job, run, exc.code, exc.message)
            return
        except SQLAlchemyError as exc:
            # Pages committed before the failure are complete and consistent; re-running
            # the job range is idempotent.
            self._fail_symbol(job, run, "database_error", f"database error: {type(exc).__name__}")
            return

        status = IngestionSymbolStatus.COMPLETED if run.retrieved else IngestionSymbolStatus.NO_DATA
        with session_scope(self._sessions) as session:
            self._record_coverage(session, job, run, complete=True)
            if status is IngestionSymbolStatus.NO_DATA:
                MarketDataEventRepository(session).add_quality_events(
                    [
                        self._event(
                            job,
                            QualityIssue(
                                DataQualityEventType.NO_DATA,
                                Severity.WARNING,
                                f"provider returned no {job.data_type.value.lower()}s for the "
                                "requested range (no trades on the feed, a non-trading period, "
                                "or no coverage)",
                            ),
                            run,
                            interval=(job.start, job.end),
                        )
                    ]
                )
            IngestionJobRepository(session).set_symbol_status(job.id, provider_symbol, status)

    def _fail_symbol(self, job: _Job, run: _SymbolRun, code: str, message: str) -> None:
        message = redact_text(message)
        logger.warning(
            "historical.symbol_failed",
            job_id=str(job.id),
            provider_symbol=run.provider_symbol,
            code=code,
            detail=message,
        )
        with session_scope(self._sessions) as session:
            # Pages before the failure were committed and are contiguous in time.
            self._record_coverage(session, job, run, complete=False)
            event_type = (
                DataQualityEventType.CAPABILITY_LIMITATION
                if code in ("unsupported_capability", "provider_entitlement_denied")
                else DataQualityEventType.PROVIDER_ERROR
            )
            MarketDataEventRepository(session).add_quality_events(
                [
                    self._event(
                        job,
                        QualityIssue(event_type, Severity.ERROR, message, {"code": code}),
                        run,
                        interval=(job.start, job.end),
                    )
                ]
            )
            IngestionJobRepository(session).set_symbol_status(
                job.id, run.provider_symbol, IngestionSymbolStatus.FAILED, f"{code}: {message}"
            )

    def _persist_page(self, job: _Job, run: _SymbolRun, page: BarPage) -> None:
        now = self._clock.now()
        outcome = _PageOutcome()
        outcome.counts["pages_retrieved"] = 1
        retrieved = len(page.bars) + len(page.malformed)
        outcome.counts["records_retrieved"] = retrieved
        run.retrieved += retrieved
        raw_body = page.raw_event.raw_body or ""

        with session_scope(self._sessions) as session:
            events = MarketDataEventRepository(session)
            raw_id = events.add_raw(
                RawMarketDataEventRecord(
                    provider=job.provider,
                    feed=job.feed,
                    source=page.raw_event.source,
                    message_type=page.raw_event.message_type,
                    provider_symbol=run.provider_symbol,
                    ingestion_job_id=job.id,
                    page_number=page.page_number,
                    request_params=page.raw_event.request,
                    http_status=page.raw_event.http_status,
                    received_at=page.received_at,
                    payload_text=raw_body,
                    payload_sha256=hashlib.sha256(raw_body.encode("utf-8")).hexdigest(),
                )
            )

            def reject(issue: QualityIssue, bar: ProviderBar | None = None) -> None:
                outcome.counts["records_rejected"] += 1
                interval = None
                if bar is not None:
                    end = bar_interval_end(bar.event_time, bar.timeframe)
                    interval = (bar.event_time, end or bar.event_time)
                outcome.events.append(
                    self._event(job, issue, run, interval=interval, raw_id=raw_id)
                )

            for item in page.malformed:
                event_type = (
                    DataQualityEventType.MISSING_FIELD
                    if "missing required field" in item.reason
                    else DataQualityEventType.REJECTED_MESSAGE
                )
                reject(
                    QualityIssue(
                        event_type,
                        Severity.ERROR,
                        f"unparseable bar rejected: {item.reason}",
                        {"provider_symbol": item.provider_symbol, "item": _jsonable(item.item)},
                    )
                )

            accepted: list[ProviderBar] = []
            if page.currency is not None and page.currency != run.quote_currency:
                for bar in page.bars:
                    reject(
                        QualityIssue(
                            DataQualityEventType.CURRENCY_MISMATCH,
                            Severity.ERROR,
                            f"prices in {page.currency}, asset is quoted in {run.quote_currency}",
                        ),
                        bar,
                    )
            else:
                for bar in page.bars:
                    issue = check_request_consistency(
                        bar,
                        expected_symbol=run.provider_symbol,
                        expected_feed=job.feed,
                        range_start=job.start,
                        range_end=job.end,
                    )
                    if issue is None and not run.mapped_to_asset(bar.event_time):
                        issue = QualityIssue(
                            DataQualityEventType.UNEXPECTED_SYMBOL,
                            Severity.ERROR,
                            f"'{run.provider_symbol}' was not mapped to this asset at "
                            f"{bar.event_time.isoformat()}",
                        )
                    last = run.last_bar
                    if issue is None and last is not None and bar.event_time <= last.event_time:
                        if bar.event_time == last.event_time and same_observation(bar, last):
                            outcome.counts["records_duplicate"] += 1
                            outcome.events.append(
                                self._event(
                                    job,
                                    QualityIssue(
                                        DataQualityEventType.DUPLICATE_EVENT,
                                        Severity.INFO,
                                        "identical bar repeated within one response",
                                    ),
                                    run,
                                    interval=(bar.event_time, bar.event_time),
                                    raw_id=raw_id,
                                )
                            )
                            continue
                        issue = QualityIssue(
                            DataQualityEventType.OUT_OF_ORDER_EVENT,
                            Severity.ERROR,
                            "bar out of order or conflicting with an earlier bar of the same "
                            "response",
                            {"previous": last.event_time.isoformat(), **_values(bar)},
                        )
                    if issue is not None:
                        reject(issue, bar)
                        continue
                    accepted.append(bar)
                    run.last_bar = bar

            outcome.counts["records_validated"] = retrieved - outcome.counts["records_rejected"]
            self._store_bars(session, job, run, accepted, now, raw_id, outcome)
            events.add_quality_events(outcome.events)
            jobs = IngestionJobRepository(session)
            jobs.add_counts(job.id, run.provider_symbol, dict(outcome.counts), now)
            if accepted:
                jobs.extend_event_range(
                    job.id, run.provider_symbol, accepted[0].event_time, accepted[-1].event_time
                )
            if page.bars:
                run.last_event_at = max(bar.event_time for bar in page.bars)

    def _persist_observations(self, job: _Job, run: _SymbolRun, page: ObservationPage) -> None:
        """One page of historical trades or quotes, in one transaction."""
        now = self._clock.now()
        outcome = _PageOutcome()
        outcome.counts["pages_retrieved"] = 1
        retrieved = page.item_count
        outcome.counts["records_retrieved"] = retrieved
        run.retrieved += retrieved
        raw_body = page.raw_event.raw_body or ""
        observations: tuple[Any, ...] = page.trades or page.quotes
        with session_scope(self._sessions) as session:
            events = MarketDataEventRepository(session)
            raw_id = events.add_raw(
                RawMarketDataEventRecord(
                    provider=job.provider,
                    feed=job.feed,
                    source=page.raw_event.source,
                    message_type=page.raw_event.message_type,
                    provider_symbol=run.provider_symbol,
                    ingestion_job_id=job.id,
                    page_number=page.page_number,
                    request_params=page.raw_event.request,
                    http_status=page.raw_event.http_status,
                    received_at=page.received_at,
                    payload_text=raw_body,
                    payload_sha256=hashlib.sha256(raw_body.encode("utf-8")).hexdigest(),
                )
            )
            findings: Counter[tuple[DataQualityEventType, Severity, str]] = Counter()

            def reject(issue: QualityIssue) -> None:
                outcome.counts["records_rejected"] += 1
                findings[(issue.event_type, issue.severity, issue.message)] += 1

            for item in page.malformed:
                reject(
                    QualityIssue(
                        DataQualityEventType.REJECTED_MESSAGE,
                        Severity.ERROR,
                        f"unparseable {job.data_type.value.lower()} rejected: {item.reason}",
                    )
                )
            rows: list[dict[str, Any]] = []
            for observation in observations:
                if observation.provider_symbol != run.provider_symbol:
                    reject(
                        QualityIssue(
                            DataQualityEventType.UNEXPECTED_SYMBOL,
                            Severity.ERROR,
                            "observation for a symbol that was not requested",
                        )
                    )
                    continue
                if not job.start <= observation.event_time <= job.end:
                    reject(
                        QualityIssue(
                            DataQualityEventType.OUT_OF_REQUESTED_RANGE,
                            Severity.ERROR,
                            "observation outside the requested range",
                        )
                    )
                    continue
                if not run.mapped_to_asset(observation.event_time):
                    reject(
                        QualityIssue(
                            DataQualityEventType.UNEXPECTED_SYMBOL,
                            Severity.ERROR,
                            f"'{run.provider_symbol}' was not mapped to this asset then",
                        )
                    )
                    continue
                trade = job.data_type is DataType.TRADE
                assessment = assess_trade(observation) if trade else assess_quote(observation)
                for issue in assessment.issues:
                    findings[(issue.event_type, issue.severity, issue.message)] += 1
                if assessment.status is DataQualityStatus.QUARANTINED:
                    outcome.counts["records_quarantined"] += 1
                build = trade_row if trade else quote_row
                rows.append(
                    build(
                        observation,
                        run.asset_id,
                        provider=job.provider,
                        feed=job.feed,
                        status=assessment.status,
                        now=now,
                        raw_id=raw_id,
                    )
                )
            repo = LiveObservationRepository(session)
            insert = repo.insert_trades if job.data_type is DataType.TRADE else repo.insert_quotes
            new = insert(rows)
            outcome.counts["records_validated"] = len(rows)
            outcome.counts["records_stored"] = len(new)
            outcome.counts["records_duplicate"] = len(rows) - len(new)
            interval = (
                (observations[0].event_time, observations[-1].event_time)
                if observations
                else (job.start, job.end)
            )
            for (event_type, severity, message), count in findings.items():
                suffix = f" ({count} occurrences)" if count > 1 else ""
                outcome.events.append(
                    self._event(
                        job,
                        QualityIssue(event_type, severity, f"{message}{suffix}", {"count": count}),
                        run,
                        interval=interval,
                        raw_id=raw_id,
                    )
                )
            events.add_quality_events(outcome.events)
            jobs = IngestionJobRepository(session)
            jobs.add_counts(job.id, run.provider_symbol, dict(outcome.counts), now)
            if observations:
                jobs.extend_event_range(
                    job.id,
                    run.provider_symbol,
                    min(o.event_time for o in observations),
                    max(o.event_time for o in observations),
                )
                run.last_event_at = max(o.event_time for o in observations)

    def _record_coverage(
        self, session: Session, job: _Job, run: _SymbolRun, *, complete: bool
    ) -> None:
        """Completeness evidence from this symbol's retrieval (2D).

        Complete: the whole range up to what had settled when the retrieval began. Failed:
        only up to the newest observation received (responses are ascending in time).
        """
        if job.timeframe is not None:
            try:
                check_trackable(job.timeframe)
            except UnsupportedTimeframeError:
                return  # completeness is not tracked for week/month bars
        if run.started_at is None:
            return
        end = job.end
        if not complete:
            if run.last_event_at is None:
                return
            end = min(end, run.last_event_at)
        hours = self._provider.feed_access().session_hours
        interval = rest_coverage(
            job.data_type,
            job.timeframe,
            job.start,
            end,
            run.started_at,
            timedelta(seconds=self._settings.market_data_recovery_settle_seconds),
            hours,
        )
        if not complete and interval is not None:
            # The newest received observation itself is proven, not what follows it.
            interval = (interval[0], min(interval[1], end + ONE_MICROSECOND))
        if interval is None:
            return
        series = Series(
            asset_id=run.asset_id,
            provider=job.provider,
            feed=job.feed,
            data_type=job.data_type,
            timeframe=None if job.timeframe is None else str(job.timeframe),
            adjustment=job.adjustment if job.data_type is DataType.BAR else None,
        )
        CompletenessRepository(session).add_coverage(
            series,
            interval,
            CoverageEvidence.REST_RETRIEVAL,
            at=self._clock.now(),
            ingestion_job_id=job.id,
        )

    def _store_bars(
        self,
        session: Session,
        job: _Job,
        run: _SymbolRun,
        bars: Sequence[ProviderBar],
        now: datetime,
        raw_id: int,
        outcome: _PageOutcome,
    ) -> None:
        if not bars:
            return
        repo = MarketBarRepository(session)
        key = SeriesKey(
            run.asset_id, str(job.bar_timeframe), job.provider, job.feed, job.adjustment
        )
        repo.lock_series(key)
        latest = repo.latest_revisions(key, (bar.event_time for bar in bars))
        rows: list[dict[str, Any]] = []
        for bar in bars:
            assessment = assess_bar(bar)
            previous = latest.get(bar.event_time)
            if (
                previous is not None
                and same_observation(bar, previous)
                and previous.quality_status is assessment.status
            ):
                outcome.counts["records_duplicate"] += 1
                continue
            interval_end = bar_interval_end(bar.event_time, bar.timeframe) or bar.event_time
            for issue in assessment.issues:
                outcome.events.append(
                    self._event(
                        job,
                        issue,
                        run,
                        interval=(bar.event_time, interval_end),
                        raw_id=raw_id,
                        status=assessment.status,
                    )
                )
            revision = 0
            if previous is not None:
                revision = previous.revision + 1
                outcome.counts["records_revised"] += 1
                outcome.events.append(
                    self._event(
                        job,
                        QualityIssue(
                            DataQualityEventType.CORRECTION,
                            Severity.INFO
                            if previous.quality_status is DataQualityStatus.INCOMPLETE
                            else Severity.WARNING,
                            f"bar changed since revision {previous.revision}; stored as "
                            f"revision {revision}, earlier revision kept",
                            {"previous": _values(previous), "current": _values(bar)},
                        ),
                        run,
                        interval=(bar.event_time, interval_end),
                        raw_id=raw_id,
                        status=assessment.status,
                    )
                )
            if assessment.status is DataQualityStatus.QUARANTINED:
                outcome.counts["records_quarantined"] += 1
            rows.append(
                {
                    "asset_id": run.asset_id,
                    "provider": job.provider,
                    "feed": job.feed,
                    "provider_symbol": bar.provider_symbol,
                    "timeframe": str(bar.timeframe),
                    "adjustment": bar.adjustment,
                    "event_time": bar.event_time,
                    "revision": revision,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "vwap": bar.vwap,
                    "trade_count": bar.trade_count,
                    "is_complete": bar.is_complete,
                    "source": bar.source,
                    "quality_status": assessment.status,
                    "received_at": bar.received_at,
                    "available_at": bar.available_at,
                    "original_available_at": bar.original_available_at,
                    "processed_at": now,
                    "ingestion_job_id": job.id,
                    "raw_event_id": raw_id,
                }
            )
        outcome.counts["records_stored"] += repo.insert(rows)

    def _finish_job(self, job_id: uuid.UUID, crash: str | None = None) -> None:
        with session_scope(self._sessions) as session:
            repo = IngestionJobRepository(session)
            record = repo.get(job_id, lock=True)
            if record is None:
                return
            if record.status is not IngestionJobStatus.RUNNING:
                # Reclaimed as interrupted by another process: never overwrite that.
                logger.warning(
                    "historical.finish_skipped", job_id=str(job_id), status=record.status.value
                )
                return
            symbols = repo.symbols(job_id)
            finished = (IngestionSymbolStatus.COMPLETED, IngestionSymbolStatus.NO_DATA)
            failed = [s for s in symbols if s.status not in finished]
            if crash is not None:
                status = IngestionJobStatus.FAILED
                for symbol in failed:
                    repo.set_symbol_status(
                        job_id, symbol.provider_symbol, IngestionSymbolStatus.FAILED, crash
                    )
            elif not failed:
                status = IngestionJobStatus.COMPLETED
            elif len(failed) == len(symbols):
                status = IngestionJobStatus.FAILED
            else:
                status = IngestionJobStatus.PARTIALLY_COMPLETED
            errors = [f"{s.provider_symbol}: {s.error}" for s in failed if s.error]
            # Assign together: a finished status always carries its completion time.
            record.completed_at = self._clock.now()
            record.error = crash or ("; ".join(errors)[:2000] if errors else None)
            record.status = status

    # ------------------------------------------------------------ queries and recovery
    def get_job(self, job_id: uuid.UUID) -> DataIngestionJob:
        with session_scope(self._sessions) as session:
            repo = IngestionJobRepository(session)
            record = repo.get(job_id)
            if record is None:
                raise UnknownAssetError(f"ingestion job {job_id} does not exist")
            return job_snapshot(record, repo.symbols(job_id))

    def recover_interrupted_jobs(self) -> int:
        """Fail jobs whose heartbeat is stale (their process died); live jobs are kept."""
        now = self._clock.now()
        with session_scope(self._sessions) as session:
            return IngestionJobRepository(session).mark_interrupted(
                reason=INTERRUPTED_REASON, at=now, stale_before=now - STALE_AFTER
            )

    # ------------------------------------------------------------ helpers
    def _record_limitation(
        self,
        feed: str,
        timeframe: Timeframe | None,
        message: str,
        data_type: DataType = DataType.BAR,
    ) -> None:
        with session_scope(self._sessions) as session:
            session.add(
                DataQualityEventRecord(
                    event_type=DataQualityEventType.CAPABILITY_LIMITATION,
                    severity=Severity.WARNING,
                    provider=self._provider.provider_id,
                    feed=feed,
                    data_type=data_type,
                    timeframe=None if timeframe is None else str(timeframe),
                    detected_at=self._clock.now(),
                    message=redact_text(message),
                    details={},
                )
            )

    def _event(
        self,
        job: _Job,
        issue: QualityIssue,
        run: _SymbolRun,
        *,
        interval: tuple[datetime, datetime] | None = None,
        raw_id: int | None = None,
        status: DataQualityStatus | None = None,
    ) -> DataQualityEventRecord:
        return DataQualityEventRecord(
            event_type=issue.event_type,
            severity=issue.severity,
            provider=job.provider,
            feed=job.feed,
            asset_id=run.asset_id,
            provider_symbol=run.provider_symbol,
            data_type=job.data_type,
            timeframe=None if job.timeframe is None else str(job.timeframe),
            interval_start=interval[0] if interval else None,
            interval_end=interval[1] if interval else None,
            detected_at=self._clock.now(),
            resulting_status=status,
            message=issue.message,
            details=_jsonable(issue.details),
            ingestion_job_id=job.id,
            raw_event_id=raw_id,
        )


def _jsonable(value: Any) -> Any:
    """Make details JSON-safe (Decimals and datetimes become strings)."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _counters(record: IngestionJobRecord | IngestionJobSymbolRecord) -> IngestionCounters:
    return IngestionCounters(
        pages_retrieved=record.pages_retrieved,
        records_retrieved=record.records_retrieved,
        records_validated=record.records_validated,
        records_stored=record.records_stored,
        records_duplicate=record.records_duplicate,
        records_revised=record.records_revised,
        records_rejected=record.records_rejected,
        records_quarantined=record.records_quarantined,
    )


def job_snapshot(
    record: IngestionJobRecord, symbols: Sequence[IngestionJobSymbolRecord]
) -> DataIngestionJob:
    return DataIngestionJob(
        job_id=record.id,
        provider=record.provider,
        feed=record.feed,
        adjustment=record.adjustment,
        data_type=record.data_type,
        provider_symbols=tuple(record.requested_symbols),
        timeframe=record.timeframe,
        range_start=record.range_start,
        range_end=record.range_end,
        status=record.status,
        counters=_counters(record),
        symbols=tuple(
            IngestionSymbolResult(
                provider_symbol=s.provider_symbol,
                asset_id=s.asset_id,
                status=s.status,
                counters=_counters(s),
                first_event_at=s.first_event_at,
                last_event_at=s.last_event_at,
                error=s.error,
            )
            for s in symbols
        ),
        error=record.error,
        requested_by=record.requested_by,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


__all__ = [
    "HistoricalFetchRequest",
    "HistoricalIngestionService",
    "IngestionJobStateError",
    "UnknownAssetError",
    "job_snapshot",
]
