"""Market intelligence runs: scope, history, point-in-time inputs, persistence (Phase 3B).

A run is created RUNNING (a partial unique index allows one per provider and feed), then
executed:

* **LIVE**: optionally re-retrieve the daily history window of the benchmark and the
  qualified universe (one request per instrument, so an adjusted series is consistent across
  the window, D-063); then compute at ``as_of`` = now with basis RECORDED: only bars Atlas
  had stored by ``as_of``, completed sessions only, members as of ``as_of``.
* **RECONSTRUCTION**: past decision times (each session's close + ``settle_seconds``),
  basis RECONSTRUCTED: bars retrieved up to the run's start whose session had completed by
  the decision time. Membership is still what Atlas recorded by the decision time, so
  periods before Phase 3A have no universe (no participation), never today's universe.

Every decision time is persisted in one transaction, after checking under a row lock that
the run is still RUNNING: a run another process reclaimed (its heartbeat went stale)
commits nothing. Results are append-only; recomputing a decision time adds rows.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery import membership
from app.asset_discovery.enums import UniverseLayer
from app.asset_discovery.rules import create_version, latest_version
from app.asset_discovery.sessions import CALENDARS, ExchangeCalendar, completed_sessions
from app.core.clocks import Clock
from app.core.config import MarketDataSettings
from app.core.exceptions import AtlasError
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.capabilities import FeedCoverage
from app.data_engine.errors import MarketDataError
from app.data_engine.historical import (
    MAX_SYMBOLS_PER_JOB,
    HistoricalFetchRequest,
    HistoricalIngestionService,
)
from app.data_engine.providers.base import MarketDataProvider
from app.data_engine.timeframes import DAY_1
from app.database.models import (
    ActorType,
    Asset,
    AssetStatus,
    CrossAssetRelationshipRecord,
    IntelligenceConfigRecord,
    IntelligenceRunRecord,
    MarketFeatureSnapshotRecord,
    MarketSummaryRecord,
    RegimeClassificationRecord,
)
from app.database.repositories import universe as reads
from app.database.sessions import session_scope
from app.feature_engine.features import FEATURE_SET_VERSION, Bar, FeatureQuality
from app.market_intelligence.classification import Classification
from app.market_intelligence.config import (
    DEFAULT_CONFIGS,
    IntelligenceConfig,
    MarketIntelligenceSettings,
)
from app.market_intelligence.correlation import ALGORITHM as CROSS_ASSET_ALGORITHM
from app.market_intelligence.engine import (
    DAILY,
    AssetInput,
    IntelligenceResult,
    compute,
    session_midnight,
)
from app.market_intelligence.enums import (
    KnowledgeBasis,
    RelationKind,
    RunKind,
    RunStatus,
    Scope,
)
from app.market_intelligence.participation import ALGORITHM as PARTICIPATION_ALGORITHM

logger = get_logger(__name__)

HEARTBEAT_SECONDS = 30.0
STALE_AFTER = timedelta(minutes=5)
CALENDAR = "XNYS"  # every supported US venue follows the NYSE schedule (D-059)
INTERRUPTED = "interrupted: the process running this run stopped before it finished"


@dataclass(frozen=True)
class UniverseScope:
    """Qualified-universe members at a decision time (watchlist first, then by symbol)
    and the order in which their history is prepared (benchmark first)."""

    members: list[uuid.UUID]
    watchlist: list[uuid.UUID]
    prep_order: list[uuid.UUID]


class RunInProgressError(AtlasError):
    code = "intelligence_run_in_progress"
    http_status = 409


class RunReclaimedError(AtlasError):
    code = "intelligence_run_reclaimed"
    http_status = 409


# ------------------------------------------------------------------ number formatting
def number(value: float | None) -> str | None:
    """A float as a decimal string with 10 significant digits; None for missing or
    non-finite values (never 0)."""
    if value is None or not math.isfinite(value):
        return None
    return format(Decimal(repr(value)), ".10g") if value != 0 else "0"


def decimal(value: float | None, places: int = 8) -> Decimal | None:
    if value is None or not math.isfinite(value):
        return None
    return Decimal(repr(value)).quantize(Decimal(1).scaleb(-places))


def jsonable(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float):
        return number(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def resolve_config(
    session: Session, code: str
) -> tuple[IntelligenceConfigRecord, IntelligenceConfig]:
    record = latest_version(session, IntelligenceConfigRecord, code)
    if record is None:
        if code not in DEFAULT_CONFIGS:
            raise LookupError(f"market intelligence configuration '{code}' does not exist")
        description, config = DEFAULT_CONFIGS[code]
        record = create_version(
            session,
            IntelligenceConfigRecord,
            code=code,
            config=config,
            description=description,
            created_by="market_intelligence",
            actor_type=ActorType.SYSTEM,
            reason="initial default configuration",
        )
    return record, IntelligenceConfig.model_validate(record.config)


class MarketIntelligenceService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: MarketDataProvider,
        market_settings: MarketDataSettings,
        settings: MarketIntelligenceSettings,
        clock: Clock,
        *,
        ingestion: HistoricalIngestionService | None = None,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._market = market_settings
        self.settings = settings
        self._clock = clock
        self._ingestion = ingestion
        self.heartbeat_seconds = HEARTBEAT_SECONDS
        self.calendar: ExchangeCalendar = CALENDARS[CALENDAR]

    # ------------------------------------------------------------ run lifecycle
    def create_run(
        self, kind: RunKind, *, requested_by: str, params: dict[str, Any] | None = None
    ) -> uuid.UUID:
        """Create a RUNNING run (after failing stale orphans); RunInProgressError if a live
        run of this provider and feed is already RUNNING."""
        now = self._clock.now()
        provider, feed = self._provider.provider_id, self._provider.feed
        with session_scope(self._sessions) as session:
            reclaimed = session.execute(
                update(IntelligenceRunRecord)
                .where(
                    IntelligenceRunRecord.provider == provider,
                    IntelligenceRunRecord.feed == feed,
                    IntelligenceRunRecord.status == RunStatus.RUNNING,
                    IntelligenceRunRecord.heartbeat_at < now - STALE_AFTER,
                )
                .values(status=RunStatus.FAILED, error=INTERRUPTED, completed_at=now)
            )
            stale = int(getattr(reclaimed, "rowcount", 0) or 0)
            if stale:
                logger.warning("intelligence.runs_reclaimed", count=stale)
            config_record, _ = resolve_config(session, self.settings.config)
            run = IntelligenceRunRecord(
                id=uuid.uuid4(),
                kind=kind,
                status=RunStatus.RUNNING,
                basis=KnowledgeBasis.RECORDED
                if kind is RunKind.LIVE
                else KnowledgeBasis.RECONSTRUCTED,
                provider=provider,
                feed=feed,
                adjustment=self.settings.price_adjustment,
                config_id=config_record.id,
                as_of_start=now,
                as_of_end=now,
                requested_by=requested_by,
                params=params or {},
                started_at=now,
                heartbeat_at=now,
            )
            try:
                with session.begin_nested():
                    session.add(run)
                    session.flush()
            except IntegrityError:
                raise RunInProgressError(
                    f"a market intelligence run for {provider}/{feed} is already running"
                ) from None
            return run.id

    async def execute(self, run_id: uuid.UUID) -> IntelligenceRunRecord:
        """Execute a RUNNING run to its end (COMPLETED, PARTIALLY_COMPLETED or FAILED)."""
        run = self.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            raise RunReclaimedError(f"run {run_id} is {run.status.value}, not RUNNING")
        lost = asyncio.Event()
        work = asyncio.ensure_future(self._execute(run))
        beat = asyncio.create_task(self._heartbeat(run_id, work, lost))
        try:
            status, summary = await work
        except asyncio.CancelledError:
            if not lost.is_set():
                raise
            logger.error("intelligence.run_reclaimed", run_id=str(run_id))
            return self.get_run(run_id)
        except Exception as exc:
            logger.exception("intelligence.run_failed", run_id=str(run_id))
            message = (
                exc.message
                if isinstance(exc, AtlasError | MarketDataError)
                else (f"{type(exc).__name__}: {redact_text(str(exc))}")
            )
            self._finish(run_id, RunStatus.FAILED, {}, message[:2000])
            if isinstance(exc, SQLAlchemyError | AtlasError | MarketDataError | LookupError):
                return self.get_run(run_id)
            raise
        finally:
            beat.cancel()
        self._finish(run_id, status, summary, summary.get("problem"))
        return self.get_run(run_id)

    async def run(
        self, kind: RunKind, *, requested_by: str, params: dict[str, Any] | None = None
    ) -> IntelligenceRunRecord:
        return await self.execute(self.create_run(kind, requested_by=requested_by, params=params))

    def get_run(self, run_id: uuid.UUID) -> IntelligenceRunRecord:
        with session_scope(self._sessions) as session:
            run = session.get(IntelligenceRunRecord, run_id)
            if run is None:
                raise LookupError(f"market intelligence run {run_id} does not exist")
            session.expunge(run)
            return run

    async def _heartbeat(
        self, run_id: uuid.UUID, work: asyncio.Future[Any], lost: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                alive = await asyncio.to_thread(self._touch, run_id)
            except SQLAlchemyError:
                logger.warning("intelligence.heartbeat_failed", run_id=str(run_id))
                continue
            if not alive:
                lost.set()
                work.cancel()
                return

    def _touch(self, run_id: uuid.UUID) -> bool:
        with session_scope(self._sessions) as session:
            result = session.execute(
                update(IntelligenceRunRecord)
                .where(
                    IntelligenceRunRecord.id == run_id,
                    IntelligenceRunRecord.status == RunStatus.RUNNING,
                )
                .values(heartbeat_at=self._clock.now())
            )
            return bool(getattr(result, "rowcount", 0))

    def _finish(
        self, run_id: uuid.UUID, status: RunStatus, summary: dict[str, Any], error: str | None
    ) -> None:
        with session_scope(self._sessions) as session:
            run = session.get(IntelligenceRunRecord, run_id, with_for_update=True)
            if run is None or run.status is not RunStatus.RUNNING:
                return  # reclaimed meanwhile: its FAILED status stands
            run.summary = jsonable(summary)
            run.error = error
            run.completed_at = self._clock.now()
            run.status = status

    @staticmethod
    def _owned(session: Session, run_id: uuid.UUID) -> IntelligenceRunRecord:
        run = session.get(IntelligenceRunRecord, run_id, with_for_update=True)
        if run is None or run.status is not RunStatus.RUNNING:
            raise RunReclaimedError(f"run {run_id} is no longer RUNNING; results discarded")
        return run

    # ------------------------------------------------------------ execution
    async def _execute(self, run: IntelligenceRunRecord) -> tuple[RunStatus, dict[str, Any]]:
        with session_scope(self._sessions) as session:
            _, config = resolve_config(session, self.settings.config)
            benchmark = self._benchmark(session)
        if run.kind is RunKind.LIVE:
            return await self._execute_live(run, config, benchmark)
        return await self._execute_reconstruction(run, config, benchmark)

    async def _execute_live(
        self, run: IntelligenceRunRecord, config: IntelligenceConfig, benchmark: uuid.UUID | None
    ) -> tuple[RunStatus, dict[str, Any]]:
        start = self._clock.now()
        with session_scope(self._sessions) as session:
            scope = self._scope(session, start, benchmark)
        preparation = await self._prepare(scope.prep_order, self.settings.history_lookback_days)
        as_of = self._clock.now()
        self._set_window(run.id, as_of, as_of)
        result = await asyncio.to_thread(
            self._compute_and_store,
            run.id,
            config,
            benchmark,
            as_of,
            as_of,
            KnowledgeBasis.RECORDED,
        )
        summary = self._summary(result, preparation)
        summary["decision_times"] = 1
        return self._status(result, preparation, summary), summary

    async def _execute_reconstruction(
        self, run: IntelligenceRunRecord, config: IntelligenceConfig, benchmark: uuid.UUID | None
    ) -> tuple[RunStatus, dict[str, Any]]:
        count = min(int(run.params.get("sessions", 20)), self.settings.reconstruction_sessions)
        settle = timedelta(seconds=self.settings.settle_seconds)
        start = self._clock.now()
        decisions = [
            s.regular_close + settle
            for s in completed_sessions(self.calendar, start, count, settle=settle)
        ]
        with session_scope(self._sessions) as session:
            assets: set[uuid.UUID] = set()
            for at in decisions:
                assets |= set(membership.members_at(session, UniverseLayer.QUALIFIED_UNIVERSE, at))
            order = ([benchmark] if benchmark else []) + sorted(assets - {benchmark}, key=str)
        span_days = math.ceil(count * 7 / 5) + 7
        preparation = await self._prepare(order, self.settings.history_lookback_days + span_days)
        known_at = self._clock.now()  # every bar retrieved up to here may be used
        if decisions:
            self._set_window(run.id, decisions[0], decisions[-1])
        results: list[IntelligenceResult] = []
        for at in decisions:
            results.append(
                await asyncio.to_thread(
                    self._compute_and_store,
                    run.id,
                    config,
                    benchmark,
                    at,
                    known_at,
                    KnowledgeBasis.RECONSTRUCTED,
                )
            )
        summary = self._summary(results[-1], preparation) if results else {}
        summary |= {
            "decision_times": len(decisions),
            "first_decision": decisions[0].isoformat() if decisions else None,
            "last_decision": decisions[-1].isoformat() if decisions else None,
            "known_at": known_at.isoformat(),
            "basis_note": "RECONSTRUCTED: bars retrieved later whose sessions had completed by "
            "each decision time; membership as Atlas recorded it then.",
        }
        status = self._status(results[-1], preparation, summary) if results else RunStatus.FAILED
        return status, summary

    # ------------------------------------------------------------ scope and history
    def _benchmark(self, session: Session) -> uuid.UUID | None:
        return session.scalar(
            select(Asset.id)
            .where(
                Asset.symbol == self.settings.benchmark_symbol, Asset.status == AssetStatus.ACTIVE
            )
            .limit(1)
        )

    def _symbols(self, session: Session, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        wanted = list(set(ids))
        if not wanted:
            return {}
        rows = session.execute(select(Asset.id, Asset.symbol).where(Asset.id.in_(wanted)))
        return {row.id: row.symbol for row in rows}

    def _scope(self, session: Session, at: datetime, benchmark: uuid.UUID | None) -> UniverseScope:
        """Members at ``at`` (as recorded by then), watchlist first; the benchmark first in
        the history order."""
        members = membership.members_at(session, UniverseLayer.QUALIFIED_UNIVERSE, at)
        watch = membership.members_at(session, UniverseLayer.ACTIVE_WATCHLIST, at)
        symbols = self._symbols(session, members)
        by_symbol = sorted(members, key=lambda a: (symbols.get(a, ""), str(a)))
        watchlist = [a for a in by_symbol if a in watch]
        ordered = watchlist + [a for a in by_symbol if a not in watch]
        prep = ([benchmark] if benchmark else []) + [a for a in ordered if a != benchmark]
        return UniverseScope(ordered, watchlist, prep)

    async def _prepare(self, assets: Sequence[uuid.UUID], lookback_days: int) -> dict[str, Any]:
        """Re-retrieve the whole history window of ``assets`` (bounded), one request each."""
        limit = self.settings.history_prep_max_assets
        if not self.settings.prepare_history or self._ingestion is None or limit == 0:
            return {"enabled": False}
        chosen = list(assets)[:limit]
        now = self._clock.now()
        last = completed_sessions(self.calendar, now, 1)[-1]
        start = session_midnight(self.calendar, (now - timedelta(days=lookback_days)).date())
        end = session_midnight(self.calendar, last.day)
        jobs: list[dict[str, Any]] = []
        for offset in range(0, len(chosen), MAX_SYMBOLS_PER_JOB):
            group = chosen[offset : offset + MAX_SYMBOLS_PER_JOB]
            try:
                created = await asyncio.to_thread(
                    self._ingestion.create_job,
                    HistoricalFetchRequest(
                        asset_ids=tuple(group),
                        timeframe=DAY_1,
                        start=start,
                        end=end,
                        adjustment=self.settings.price_adjustment,
                    ),
                    "market_intelligence",
                )
                finished = await self._ingestion.run_job(created.job_id)
                jobs.append(
                    {
                        "job_id": str(finished.job_id),
                        "status": finished.status.value,
                        "assets": len(group),
                    }
                )
            except MarketDataError as exc:
                jobs.append({"error": exc.code, "message": exc.message, "assets": len(group)})
                if exc.code in ("provider_authentication_failed", "credentials_not_configured"):
                    raise
        return {
            "enabled": True,
            "requested": len(chosen),
            "deferred": max(0, len(assets) - len(chosen)),
            "lookback_days": lookback_days,
            "adjustment": self.settings.price_adjustment.value,
            "jobs": jobs,
        }

    # ------------------------------------------------------------ one decision time
    def _sessions_needed(self, config: IntelligenceConfig) -> int:
        daily = config.spec(DAILY)
        need = daily.window + daily.change_lookback + 1
        need = max(need, config.correlation.window + config.correlation.prior_window + 1)
        for spec in config.timeframes:
            if spec.timeframe != DAILY:
                need = max(need, (spec.window + spec.change_lookback + 2) * 5 + 10)
        return need

    def load(
        self,
        session: Session,
        asset_ids: Sequence[uuid.UUID],
        *,
        as_of: datetime,
        known_at: datetime,
        sessions: Sequence[Any],
    ) -> dict[uuid.UUID, AssetInput]:
        """Completed VALID daily bars of the newest revision known at ``known_at``, for the
        sessions completed by ``as_of``."""
        symbols = self._symbols(session, asset_ids)
        rows = reads.daily_bars(
            session,
            asset_ids,
            provider=self._provider.provider_id,
            feed=self._provider.feed,
            adjustment=self.settings.price_adjustment,
            sessions=list(sessions),
            calendar=self.calendar,
            as_of=as_of,
            known_at=known_at,
        )
        bars: dict[uuid.UUID, dict[Any, Bar]] = {a: {} for a in asset_ids}
        for row in rows:
            if row.quality_status != "VALID" or row.available_at is None:
                continue  # quarantined or incomplete bars are missing data, never inputs
            bars[row.asset_id][row.session] = Bar(
                event_time=row.event_time,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                available_at=row.available_at,
            )
        return {a: AssetInput(a, symbols.get(a, "?"), bars[a]) for a in asset_ids}

    def _compute_and_store(
        self,
        run_id: uuid.UUID,
        config: IntelligenceConfig,
        benchmark: uuid.UUID | None,
        as_of: datetime,
        known_at: datetime,
        basis: KnowledgeBasis,
    ) -> IntelligenceResult:
        settle = timedelta(seconds=self.settings.settle_seconds)
        sessions = completed_sessions(self.calendar, as_of, self._sessions_needed(config), settle)
        with session_scope(self._sessions) as session:
            scope = self._scope(session, as_of, benchmark)
            ids = list(dict.fromkeys(([benchmark] if benchmark else []) + scope.members))
            inputs = self.load(session, ids, as_of=as_of, known_at=known_at, sessions=sessions)
        coverage = self._provider.feed_access().coverage
        result = compute(
            as_of=as_of,
            calendar=self.calendar,
            sessions=sessions,
            inputs=inputs,
            benchmark_id=benchmark,
            members=scope.members,
            correlation_assets=scope.members,
            config=config,
            feed=self._provider.feed,
            single_venue=coverage is FeedCoverage.SINGLE_VENUE,
        )
        with session_scope(self._sessions) as session:
            run = self._owned(session, run_id)
            self._persist(session, run, result, basis, config)
        return result

    def _set_window(self, run_id: uuid.UUID, start: datetime, end: datetime) -> None:
        with session_scope(self._sessions) as session:
            run = self._owned(session, run_id)
            run.as_of_start, run.as_of_end = start, end

    def _persist(
        self,
        session: Session,
        run: IntelligenceRunRecord,
        result: IntelligenceResult,
        basis: KnowledgeBasis,
        config: IntelligenceConfig,
    ) -> None:
        now = self._clock.now()
        common = {"run_id": run.id, "as_of": result.as_of, "basis": basis, "available_at": now}
        snapshots, classifications = [], []
        for item in result.assets:
            f = item.features
            snapshots.append(
                {
                    **common,
                    "asset_id": item.asset_id,
                    "timeframe": item.timeframe,
                    "feature_set_version": FEATURE_SET_VERSION,
                    "provider": run.provider,
                    "feed": run.feed,
                    "adjustment": run.adjustment,
                    "quality": f.quality,
                    "feature_values": {k: number(v) for k, v in f.values.items()},
                    "window_bars": f.expected_bars
                    if f.quality is FeatureQuality.INSUFFICIENT_DATA
                    else f.bars_used + f.missing_bars,
                    "bars_used": f.bars_used,
                    "expected_bars": f.expected_bars,
                    "missing_bars": f.missing_bars,
                    "first_event_at": f.first_event_at,
                    "last_event_at": f.last_event_at,
                    "inputs_available_at": f.inputs_available_at,
                    "notes": list(f.notes),
                }
            )
            for c in (item.trend, item.volatility):
                classifications.append(
                    self._classification_row(
                        common,
                        run,
                        Scope.ASSET,
                        item.asset_id,
                        item.timeframe,
                        c,
                        f.inputs_available_at,
                    )
                )
        for timeframe, items in result.market.items():
            bench = result.asset(result.benchmark_id, timeframe) if result.benchmark_id else None
            available = None if bench is None else bench.features.inputs_available_at
            for c in items:
                classifications.append(
                    self._classification_row(
                        common, run, Scope.MARKET, None, timeframe, c, available
                    )
                )
        part = result.participation
        cross = result.cross_asset
        summary = {
            **common,
            "benchmark_asset_id": result.benchmark_id,
            "algorithm": PARTICIPATION_ALGORITHM,
            "participation_status": "INSUFFICIENT_DATA" if part is None else part.status,
            "members_total": result.members_total,
            "members_evaluated": 0 if part is None else part.members_evaluated,
            "participation": {}
            if part is None
            else jsonable({**part.metrics, "reasons": part.reasons}),
            "cross_asset": {}
            if cross is None
            else jsonable(
                {
                    "algorithm": CROSS_ASSET_ALGORITHM,
                    "window_start": cross.window_start,
                    "window_end": cross.window_end,
                    "average_correlation": cross.average_correlation,
                    "pairs_computed": sum(1 for p in cross.pairs if p.correlation is not None),
                    "pairs_insufficient": sum(1 for p in cross.pairs if p.correlation is None),
                    "absorption_ratio": cross.absorption_ratio,
                    "absorption_assets": cross.absorption_assets,
                    "absorption_observations": cross.absorption_observations,
                    "notes": cross.notes,
                }
            ),
            "limitations": [] if part is None else part.limitations,
        }
        relations = []
        if cross is not None:
            window = {
                "algorithm": CROSS_ASSET_ALGORITHM,
                "window_start": cross.window_start,
                "window_end": cross.window_end,
            }
            for p in cross.pairs:
                relations.append(
                    {
                        **common,
                        **window,
                        "kind": RelationKind.PAIR,
                        "asset_a": p.asset_a,
                        "asset_b": p.asset_b,
                        "window": config.correlation.window,
                        "status": p.status,
                        "observations": p.observations,
                        "correlation": decimal(p.correlation),
                        "ci_low": decimal(p.ci_low),
                        "ci_high": decimal(p.ci_high),
                        "significant": p.significant,
                        "prior_observations": p.prior_observations,
                        "prior_correlation": decimal(p.prior_correlation),
                        "change": decimal(p.change),
                        "beta": None,
                        "relative_log_return": None,
                    }
                )
            for b in cross.benchmark:
                assert result.benchmark_id is not None  # noqa: S101  # rows exist only with one
                relations.append(
                    {
                        **common,
                        **window,
                        "kind": RelationKind.BENCHMARK,
                        "asset_a": b.asset_id,
                        "asset_b": result.benchmark_id,
                        "window": config.correlation.window,
                        "status": b.status,
                        "observations": b.observations,
                        "correlation": decimal(b.correlation),
                        "ci_low": None,
                        "ci_high": None,
                        "significant": None,
                        "prior_observations": 0,
                        "prior_correlation": None,
                        "change": None,
                        "beta": decimal(b.beta),
                        "relative_log_return": decimal(b.relative_log_return),
                    }
                )
        for model, rows in (
            (MarketFeatureSnapshotRecord, snapshots),
            (RegimeClassificationRecord, classifications),
            (MarketSummaryRecord, [summary]),
            (CrossAssetRelationshipRecord, relations),
        ):
            for offset in range(0, len(rows), 1000):
                if rows[offset : offset + 1000]:
                    session.execute(insert(model), rows[offset : offset + 1000])

    @staticmethod
    def _classification_row(
        common: dict[str, Any],
        run: IntelligenceRunRecord,
        scope: Scope,
        asset_id: uuid.UUID | None,
        timeframe: str,
        c: Classification,
        inputs_available_at: datetime | None,
    ) -> dict[str, Any]:
        return {
            **common,
            "scope": scope,
            "asset_id": asset_id,
            "timeframe": timeframe,
            "dimension": c.dimension,
            "algorithm": c.algorithm,
            "config_id": run.config_id,
            "label": c.label,
            "confidence": decimal(c.confidence, 6),
            "probabilities": {k: number(v) for k, v in c.probabilities.items()},
            "quality": c.quality,
            "evidence": jsonable(c.evidence),
            "reasons": list(c.reasons),
            "inputs_available_at": inputs_available_at,
        }

    # ------------------------------------------------------------ outcome
    @staticmethod
    def _summary(result: IntelligenceResult, preparation: dict[str, Any]) -> dict[str, Any]:
        quality: Counter[str] = Counter(
            f"{a.timeframe}:{a.features.quality.value}" for a in result.assets
        )
        market = {
            timeframe: {c.dimension.value: c.label for c in items}
            for timeframe, items in result.market.items()
        }
        return {
            "as_of": result.as_of.isoformat(),
            "benchmark_asset_id": None if result.benchmark_id is None else str(result.benchmark_id),
            "instruments": len({a.asset_id for a in result.assets}),
            "members": result.members_total,
            "feature_quality": dict(quality),
            "market": market,
            "participation": None if result.participation is None else result.participation.status,
            "history_preparation": preparation,
        }

    @staticmethod
    def _status(
        result: IntelligenceResult, preparation: dict[str, Any], summary: dict[str, Any]
    ) -> RunStatus:
        problems = []
        failed = [
            j for j in preparation.get("jobs", []) if "error" in j or j.get("status") != "COMPLETED"
        ]
        if failed:
            problems.append(f"{len(failed)} history retrieval job(s) did not complete")
        if result.benchmark_id is None:
            problems.append("benchmark not registered: no market-level classification")
        else:
            bench = result.asset(result.benchmark_id, DAILY)
            if bench is None or bench.features.quality is FeatureQuality.INSUFFICIENT_DATA:
                problems.append("benchmark daily history insufficient")
        if problems:
            summary["problem"] = "; ".join(problems)
            return RunStatus.PARTIALLY_COMPLETED
        return RunStatus.COMPLETED


__all__ = [
    "MarketIntelligenceService",
    "RunInProgressError",
    "RunReclaimedError",
    "decimal",
    "jsonable",
    "number",
    "resolve_config",
]
