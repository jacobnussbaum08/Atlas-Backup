"""Background-worker status read model (Phase 9 dashboard). Read-only.

Every Atlas worker is a separate process (``python -m app.workers.<name>``). The API cannot
see those processes; it can only read what each worker recorded in PostgreSQL. This module
gathers those records into one report, and is explicit about how much each record can
prove:

* ``HEARTBEAT``: the worker renews a status row while it runs (market-data stream and
  recovery, news stream, paper broker). A heartbeat older than the worker's documented
  threshold means the process is probably not running, whatever state it last wrote.
* ``ACTIVITY``: the worker writes no heartbeat, only its output (strategy evaluations,
  decision runs, valuation checkpoints, discovery jobs). The newest output shows when it
  last did something; it cannot show that the process is alive now, so such a worker is
  never reported RUNNING (health ``NO_HEARTBEAT``), except a discovery job that is
  RUNNING with a current lease heartbeat.

Nothing here starts, stops or changes a worker. Error texts are redacted before they leave
the process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_engine.enums import DecisionMode
from app.core.security.redaction import redact_text
from app.database.models.ai import DecisionRunRecord
from app.database.models.execution import PaperBrokerStateRecord
from app.database.models.market_data import RecoveryStatusRecord, StreamStatusRecord
from app.database.models.news import NewsStreamStatusRecord
from app.database.models.orchestration import OrchestratorStatusRecord
from app.database.models.portfolio import PortfolioSnapshotRecord
from app.database.models.strategy import StrategyEvaluationRecord
from app.database.models.universe import DiscoveryJobRecord
from app.execution.monitor import MONITOR_ACTOR
from app.strategies.enums import EvaluationMode

# Thresholds for heartbeat-writing workers whose interval is not a setting. The paper
# execution worker beats every loop (idle sleep 1 s by default); a minute without one is not
# an idle loop. Discovery leases are renewed while a job runs.
PAPER_BROKER_STALE_AFTER = timedelta(seconds=60)
DISCOVERY_HEARTBEAT_STALE_AFTER = timedelta(minutes=5)
RECOVERY_HEARTBEAT_GRACE = timedelta(seconds=60)


class Observation(StrEnum):
    HEARTBEAT = "HEARTBEAT"
    ACTIVITY = "ACTIVITY"


class WorkerHealth(StrEnum):
    RUNNING = "RUNNING"  # fresh heartbeat, healthy recorded state
    DEGRADED = "DEGRADED"  # fresh heartbeat, but the worker reports a problem
    STALE = "STALE"  # heartbeat older than the threshold: probably not running
    STOPPED = "STOPPED"  # the worker recorded a clean stop
    FAILED = "FAILED"  # the worker recorded a permanent failure
    DISABLED = "DISABLED"  # switched off by configuration
    NOT_REPORTED = "NOT_REPORTED"  # no record at all in this database
    NO_HEARTBEAT = "NO_HEARTBEAT"  # writes output only; liveness cannot be observed


class WorkerStatus(BaseModel):
    worker: str  # Docker Compose service name
    title: str
    module: str  # python -m <module>
    purpose: str
    observation: Observation
    source: str  # table the report is read from
    health: WorkerHealth
    detail: str
    enabled_in_api_env: bool | None  # the API process's view of the worker's switch
    enabled_reported: bool | None  # what the worker itself recorded
    state: str | None  # the state the worker last recorded
    last_heartbeat_at: datetime | None
    heartbeat_age_seconds: float | None
    stale_after_seconds: float | None
    last_activity_at: datetime | None
    last_activity: str | None
    last_error: str | None
    last_error_at: datetime | None


class WorkersReport(BaseModel):
    checked_at: datetime
    summary: dict[str, int]
    workers: list[WorkerStatus]
    notes: list[str]


@dataclass(frozen=True)
class Switches:
    """Enable flags as the API process sees them (None: could not be read)."""

    market_data_stream: bool | None
    market_data_recovery: bool | None
    recovery_interval_seconds: float | None
    stream_stale_after_seconds: float | None
    news_stream: bool | None
    news_stale_after_seconds: float | None
    asset_discovery: bool | None
    strategy_worker: bool | None
    ai_decisions: bool | None
    ai_news_analysis: bool | None
    ai_conflict_explanations: bool | None
    orchestrator: bool | None = None
    orchestrator_tick_seconds: float | None = None


# The orchestrator beats at least every 30 s (and after every tick).
ORCHESTRATOR_STALE_AFTER = timedelta(seconds=90)


def orchestrator_stale_after(tick_seconds: float | None) -> timedelta:
    """Three missed heartbeats (the worker beats every min(tick, 30 s))."""
    if tick_seconds is None:
        return ORCHESTRATOR_STALE_AFTER
    return max(ORCHESTRATOR_STALE_AFTER, timedelta(seconds=3 * min(tick_seconds, 30.0)))


def _read(getter: Callable[[], Any]) -> Any:
    try:
        return getter()
    except (ValidationError, ValueError):
        return None


def load_switches() -> Switches:
    """Each module's settings, read independently: one invalid module does not hide the rest."""
    from app.ai_engine.config import get_ai_settings
    from app.asset_discovery.config import get_asset_discovery_settings
    from app.core.config.market_data import get_market_data_settings
    from app.news.config import get_news_settings
    from app.orchestration.config import get_orchestrator_settings
    from app.strategies.config import get_strategy_settings

    md = _read(get_market_data_settings)
    news = _read(get_news_settings)
    discovery = _read(get_asset_discovery_settings)
    strategy = _read(get_strategy_settings)
    ai = _read(get_ai_settings)
    orchestration = _read(get_orchestrator_settings)
    return Switches(
        market_data_stream=md.market_data_stream_enabled if md else None,
        market_data_recovery=md.market_data_recovery_enabled if md else None,
        recovery_interval_seconds=md.market_data_recovery_interval if md else None,
        stream_stale_after_seconds=md.market_data_stream_stale_after if md else None,
        news_stream=news.stream_enabled if news else None,
        news_stale_after_seconds=float(news.stream_stale_after) if news else None,
        asset_discovery=discovery.enabled if discovery else None,
        strategy_worker=strategy.worker_enabled if strategy else None,
        ai_decisions=ai.decisions_enabled if ai else None,
        ai_news_analysis=ai.news_analysis_enabled if ai else None,
        ai_conflict_explanations=ai.conflict_explanations_enabled if ai else None,
        orchestrator=orchestration.worker_enabled if orchestration else None,
        orchestrator_tick_seconds=orchestration.tick_seconds if orchestration else None,
    )


def _error(text: str | None) -> str | None:
    return redact_text(text)[:500] if text else None


def _age(now: datetime, at: datetime | None) -> float | None:
    return round((now - at).total_seconds(), 1) if at is not None else None


def heartbeat_health(
    *,
    now: datetime,
    heartbeat_at: datetime | None,
    stale_after: timedelta | None,
    enabled: bool | None,
    state: str | None,
) -> tuple[WorkerHealth, str]:
    """Health of a heartbeat-writing worker from its recorded row (pure; unit-tested).

    A heartbeat older than ``stale_after`` overrides every recorded running state: the row
    describes the last moment the worker was alive, not now.
    """
    if heartbeat_at is None:
        return WorkerHealth.NOT_REPORTED, "No status has been recorded in this database."
    recorded = (state or "").upper()
    if enabled is False or recorded == "DISABLED":
        return WorkerHealth.DISABLED, "The worker recorded that it is disabled by configuration."
    if recorded == "FAILED":
        return WorkerHealth.FAILED, "The worker recorded a permanent failure and is not retrying."
    if recorded == "STOPPED":
        return WorkerHealth.STOPPED, "The worker recorded a clean stop."
    age = now - heartbeat_at
    if stale_after is not None and age > stale_after:
        return (
            WorkerHealth.STALE,
            f"No heartbeat for {int(age.total_seconds())} s (threshold "
            f"{int(stale_after.total_seconds())} s): the process is probably not running. "
            f"Its last recorded state was {recorded or 'unknown'}.",
        )
    if recorded in {"DEGRADED", "RECONNECTING", "PAUSED", "FAILING"}:
        return WorkerHealth.DEGRADED, f"Heartbeat current; the worker reports {recorded}."
    return WorkerHealth.RUNNING, f"Heartbeat current; recorded state {recorded or 'unknown'}."


def _activity_health(enabled: bool | None) -> tuple[WorkerHealth, str]:
    if enabled is False:
        return (
            WorkerHealth.DISABLED,
            "Disabled in the API's environment (the worker exits at start when its own "
            "environment matches).",
        )
    return (
        WorkerHealth.NO_HEARTBEAT,
        "This worker records no heartbeat, so whether its process is running cannot be "
        "observed. The newest output it recorded is shown.",
    )


def _stream(session: Session, now: datetime, sw: Switches) -> WorkerStatus:
    row = session.scalars(
        select(StreamStatusRecord).order_by(StreamStatusRecord.heartbeat_at.desc()).limit(1)
    ).first()
    stale = (
        timedelta(seconds=sw.stream_stale_after_seconds)
        if sw.stream_stale_after_seconds is not None
        else None
    )
    health, detail = heartbeat_health(
        now=now,
        heartbeat_at=row.heartbeat_at if row else None,
        stale_after=stale,
        enabled=row.enabled if row else None,
        state=row.connection_state if row else None,
    )
    return WorkerStatus(
        worker="stream-worker",
        title="Market-data stream",
        module="app.workers.market_data_stream",
        purpose="Live market-data WebSocket: trades, quotes and bars persisted as received.",
        observation=Observation.HEARTBEAT,
        source="market_data_stream_status",
        health=health,
        detail=detail,
        enabled_in_api_env=sw.market_data_stream,
        enabled_reported=row.enabled if row else None,
        state=row.connection_state if row else None,
        last_heartbeat_at=row.heartbeat_at if row else None,
        heartbeat_age_seconds=_age(now, row.heartbeat_at) if row else None,
        stale_after_seconds=stale.total_seconds() if stale else None,
        last_activity_at=row.last_persisted_at if row else None,
        last_activity=(
            f"{row.provider}/{row.feed}: last persisted event"
            if row and row.last_persisted_at
            else None
        ),
        last_error=_error(row.last_error) if row else None,
        last_error_at=row.last_error_at if row else None,
    )


def _recovery(session: Session, now: datetime, sw: Switches) -> WorkerStatus:
    row = session.scalars(
        select(RecoveryStatusRecord).order_by(RecoveryStatusRecord.heartbeat_at.desc()).limit(1)
    ).first()
    stale = (
        timedelta(seconds=sw.recovery_interval_seconds) + RECOVERY_HEARTBEAT_GRACE
        if sw.recovery_interval_seconds is not None
        else None
    )
    health, detail = heartbeat_health(
        now=now,
        heartbeat_at=row.heartbeat_at if row else None,
        stale_after=stale,
        enabled=row.enabled if row else None,
        state=row.state if row else None,
    )
    return WorkerStatus(
        worker="recovery-worker",
        title="Market-data recovery",
        module="app.workers.market_data_recovery",
        purpose="Detects gaps in stored market data and backfills them from provider history.",
        observation=Observation.HEARTBEAT,
        source="market_data_recovery_status",
        health=health,
        detail=detail,
        enabled_in_api_env=sw.market_data_recovery,
        enabled_reported=row.enabled if row else None,
        state=row.state if row else None,
        last_heartbeat_at=row.heartbeat_at if row else None,
        heartbeat_age_seconds=_age(now, row.heartbeat_at) if row else None,
        stale_after_seconds=stale.total_seconds() if stale else None,
        last_activity_at=row.last_successful_backfill_at if row else None,
        last_activity="last successful backfill"
        if row and row.last_successful_backfill_at
        else None,
        last_error=_error(row.last_error) if row else None,
        last_error_at=row.last_error_at if row else None,
    )


def _news(session: Session, now: datetime, sw: Switches) -> WorkerStatus:
    row = session.scalars(
        select(NewsStreamStatusRecord).order_by(NewsStreamStatusRecord.heartbeat_at.desc()).limit(1)
    ).first()
    stale = (
        timedelta(seconds=sw.news_stale_after_seconds)
        if sw.news_stale_after_seconds is not None
        else None
    )
    health, detail = heartbeat_health(
        now=now,
        heartbeat_at=row.heartbeat_at if row else None,
        stale_after=stale,
        enabled=row.enabled if row else None,
        state=row.connection_state if row else None,
    )
    return WorkerStatus(
        worker="news-worker",
        title="News stream",
        module="app.workers.news_stream",
        purpose="Real-time news connection with REST recovery after each reconnection.",
        observation=Observation.HEARTBEAT,
        source="news_stream_status",
        health=health,
        detail=detail,
        enabled_in_api_env=sw.news_stream,
        enabled_reported=row.enabled if row else None,
        state=row.connection_state if row else None,
        last_heartbeat_at=row.heartbeat_at if row else None,
        heartbeat_age_seconds=_age(now, row.heartbeat_at) if row else None,
        stale_after_seconds=stale.total_seconds() if stale else None,
        last_activity_at=row.last_persisted_at if row else None,
        last_activity="last persisted article" if row and row.last_persisted_at else None,
        last_error=_error(row.last_error) if row else None,
        last_error_at=row.last_error_at if row else None,
    )


def _paper_broker(session: Session, now: datetime) -> WorkerStatus:
    row = session.scalars(
        select(PaperBrokerStateRecord)
        .order_by(PaperBrokerStateRecord.last_heartbeat_at.desc().nulls_last())
        .limit(1)
    ).first()
    beat = row.last_heartbeat_at if row else None
    if row is not None and beat is None:
        beat = row.updated_at  # registered but never beat: treat the registration as the last sign
    health, detail = heartbeat_health(
        now=now,
        heartbeat_at=beat,
        stale_after=PAPER_BROKER_STALE_AFTER,
        enabled=None,
        state=row.status.value if row else None,
    )
    return WorkerStatus(
        worker="paper-execution-worker",
        title="Paper execution (simulated broker)",
        module="app.workers.paper_execution",
        purpose="Delivers simulated paper orders to the isolated paper broker and simulates "
        "fills from stored market data. No brokerage connection exists.",
        observation=Observation.HEARTBEAT,
        source="paper_broker_state",
        health=health,
        detail=detail,
        enabled_in_api_env=None,
        enabled_reported=None,
        state=row.status.value if row else None,
        last_heartbeat_at=row.last_heartbeat_at if row else None,
        heartbeat_age_seconds=_age(now, row.last_heartbeat_at) if row else None,
        stale_after_seconds=PAPER_BROKER_STALE_AFTER.total_seconds(),
        last_activity_at=row.last_recovery_at if row else None,
        last_activity="last start-up recovery" if row and row.last_recovery_at else None,
        last_error=None,
        last_error_at=None,
    )


def _discovery(session: Session, now: datetime, sw: Switches) -> WorkerStatus:
    job = session.scalars(
        select(DiscoveryJobRecord).order_by(DiscoveryJobRecord.created_at.desc()).limit(1)
    ).first()
    health, detail = _activity_health(sw.asset_discovery)
    if job is not None and job.status.value == "RUNNING" and job.heartbeat_at is not None:
        health, detail = heartbeat_health(
            now=now,
            heartbeat_at=job.heartbeat_at,
            stale_after=DISCOVERY_HEARTBEAT_STALE_AFTER,
            enabled=None,
            state="RUNNING",
        )
        detail = f"A {job.kind.value} job is running. {detail}"
    elif job is None and health is WorkerHealth.NO_HEARTBEAT:
        health, detail = WorkerHealth.NOT_REPORTED, "No discovery job has been recorded."
    last = job.completed_at or job.started_at or job.created_at if job else None
    return WorkerStatus(
        worker="discovery-worker",
        title="Asset discovery",
        module="app.workers.asset_discovery",
        purpose="Scheduled directory discovery, qualification and watchlist scoring.",
        observation=Observation.ACTIVITY,
        source="discovery_jobs",
        health=health,
        detail=detail,
        enabled_in_api_env=sw.asset_discovery,
        enabled_reported=None,
        state=job.status.value if job else None,
        last_heartbeat_at=job.heartbeat_at if job else None,
        heartbeat_age_seconds=_age(now, job.heartbeat_at) if job else None,
        stale_after_seconds=DISCOVERY_HEARTBEAT_STALE_AFTER.total_seconds(),
        last_activity_at=last,
        last_activity=(
            f"{job.kind.value} job {job.status.value} ({job.trigger.value})" if job else None
        ),
        last_error=_error(job.error) if job else None,
        last_error_at=job.completed_at if job and job.error else None,
    )


def _strategy(session: Session, now: datetime, sw: Switches) -> WorkerStatus:
    row = session.scalars(
        select(StrategyEvaluationRecord)
        .where(StrategyEvaluationRecord.mode == EvaluationMode.LIVE)
        .order_by(StrategyEvaluationRecord.started_at.desc())
        .limit(1)
    ).first()
    health, detail = _activity_health(sw.strategy_worker)
    if row is None and health is WorkerHealth.NO_HEARTBEAT:
        health, detail = WorkerHealth.NOT_REPORTED, "No LIVE strategy evaluation has been recorded."
    return WorkerStatus(
        worker="strategy-worker",
        title="Strategy evaluation",
        module="app.workers.strategy_evaluation",
        purpose="Evaluates enabled strategies on new data and records proposals (no orders).",
        observation=Observation.ACTIVITY,
        source="strategy_evaluations (mode LIVE)",
        health=health,
        detail=detail,
        enabled_in_api_env=sw.strategy_worker,
        enabled_reported=None,
        state=row.status.value if row else None,
        last_heartbeat_at=None,
        heartbeat_age_seconds=None,
        stale_after_seconds=None,
        last_activity_at=(row.finished_at or row.started_at) if row else None,
        last_activity=(
            f"{row.strategy_id} evaluation {row.status.value}, {row.proposals_count} proposal(s)"
            if row
            else None
        ),
        last_error=None,
        last_error_at=None,
    )


def _ai(session: Session, now: datetime, sw: Switches) -> WorkerStatus:
    row = session.scalars(
        select(DecisionRunRecord)
        .where(DecisionRunRecord.mode == DecisionMode.LIVE)
        .order_by(DecisionRunRecord.started_at.desc())
        .limit(1)
    ).first()
    flags = [sw.ai_decisions, sw.ai_news_analysis, sw.ai_conflict_explanations]
    enabled = None if any(f is None for f in flags) else any(bool(f) for f in flags)
    health, detail = _activity_health(enabled)
    if row is None and health is WorkerHealth.NO_HEARTBEAT:
        health, detail = WorkerHealth.NOT_REPORTED, "No LIVE decision run has been recorded."
    return WorkerStatus(
        worker="ai-worker",
        title="AI decision engine",
        module="app.workers.ai_engine",
        purpose="Decision coordinator and asynchronous LLM analysis. Ranks proposals; submits "
        "nothing.",
        observation=Observation.ACTIVITY,
        source="decision_runs (mode LIVE)",
        health=health,
        detail=detail,
        enabled_in_api_env=enabled,
        enabled_reported=None,
        state=row.status.value if row else None,
        last_heartbeat_at=None,
        heartbeat_age_seconds=None,
        stale_after_seconds=None,
        last_activity_at=(row.finished_at or row.started_at) if row else None,
        last_activity=(
            f"decision run {row.status.value}, {row.proposals_considered} proposal(s) considered"
            if row
            else None
        ),
        last_error=None,
        last_error_at=None,
    )


def _position_monitor(session: Session, now: datetime) -> WorkerStatus:
    row = session.scalars(
        select(PortfolioSnapshotRecord)
        .where(PortfolioSnapshotRecord.created_by == MONITOR_ACTOR)
        .order_by(PortfolioSnapshotRecord.created_at.desc())
        .limit(1)
    ).first()
    health, detail = _activity_health(None)
    if row is None:
        health = WorkerHealth.NOT_REPORTED
        detail = (
            "No valuation checkpoint by the position monitor has been recorded. It records "
            "no heartbeat, so whether it runs cannot be observed."
        )
    return WorkerStatus(
        worker="position-monitor-worker",
        title="Position monitor",
        module="app.workers.position_monitor",
        purpose="Marks simulated paper positions, checks protective levels and submits "
        "simulated exits.",
        observation=Observation.ACTIVITY,
        source="portfolio_snapshots (created by the monitor)",
        health=health,
        detail=detail,
        enabled_in_api_env=None,
        enabled_reported=None,
        state=row.valuation_status.value if row else None,
        last_heartbeat_at=None,
        heartbeat_age_seconds=None,
        stale_after_seconds=None,
        last_activity_at=row.created_at if row else None,
        last_activity="valuation checkpoint" if row else None,
        last_error=None,
        last_error_at=None,
    )


def _orchestrator(session: Session, now: datetime, sw: Switches) -> WorkerStatus:
    row = session.scalars(
        select(OrchestratorStatusRecord).order_by(OrchestratorStatusRecord.heartbeat_at.desc())
    ).first()
    stale = orchestrator_stale_after(sw.orchestrator_tick_seconds)
    health, detail = heartbeat_health(
        now=now,
        heartbeat_at=row.heartbeat_at if row else None,
        stale_after=stale,
        enabled=None,
        state=row.state.value if row else None,
    )
    if row is None and sw.orchestrator is False:
        health, detail = _activity_health(False)
    mode = row.trading_mode.value if row and row.trading_mode else None
    return WorkerStatus(
        worker="orchestrator-worker",
        title="Trading orchestrator",
        module="app.workers.trading_orchestrator",
        purpose="Sequences strategies, AI decisions, risk reservations and simulated paper "
        "entry orders under the operator's trading control; runs scheduled risk monitoring "
        "and reconciliations.",
        observation=Observation.HEARTBEAT,
        source="orchestrator_status",
        health=health,
        detail=detail,
        enabled_in_api_env=sw.orchestrator,
        enabled_reported=None,
        state=row.state.value if row else None,
        last_heartbeat_at=row.heartbeat_at if row else None,
        heartbeat_age_seconds=_age(now, row.heartbeat_at) if row else None,
        stale_after_seconds=stale.total_seconds(),
        last_activity_at=row.last_tick_at if row else None,
        last_activity=f"tick (trading mode {mode})" if row and row.last_tick_at else None,
        last_error=_error(row.last_error) if row else None,
        last_error_at=row.last_error_at if row else None,
    )


NOTES = [
    "Workers are separate processes. The API reports what each recorded in PostgreSQL; it "
    "cannot see the processes themselves.",
    "HEARTBEAT workers renew a status row while running; an old heartbeat means the process "
    "is probably not running, whatever state it last wrote.",
    "ACTIVITY workers record only their output. NO_HEARTBEAT means liveness cannot be "
    "observed, not that the worker is down.",
    "enabled_in_api_env is the API process's configuration; a worker started with its own "
    "environment may differ (enabled_reported is what the worker itself recorded).",
]


def workers_report(session: Session, now: datetime, switches: Switches) -> WorkersReport:
    workers = [
        _stream(session, now, switches),
        _recovery(session, now, switches),
        _news(session, now, switches),
        _discovery(session, now, switches),
        _strategy(session, now, switches),
        _ai(session, now, switches),
        _paper_broker(session, now),
        _position_monitor(session, now),
        _orchestrator(session, now, switches),
    ]
    summary: dict[str, int] = {}
    for w in workers:
        summary[w.health.value] = summary.get(w.health.value, 0) + 1
    return WorkersReport(checked_at=now, summary=summary, workers=workers, notes=NOTES)


__all__ = [
    "Observation",
    "Switches",
    "WorkerHealth",
    "WorkerStatus",
    "WorkersReport",
    "heartbeat_health",
    "load_switches",
    "orchestrator_stale_after",
    "workers_report",
]
