"""Orchestrator settings (``ORCHESTRATOR_*``, Phases 10A-10B).

Operational knobs only: how often the orchestrator ticks, which stages it runs itself and
when its scheduled jobs are due. None of them can enable trading. Whether new paper entries
may be opened is a durable, audited operator control in PostgreSQL
(:mod:`app.orchestration.controls`), DISABLED until an operator enables it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)
from app.data_engine.calendar import MarketSession
from app.execution.enums import OrderType

ENV_PREFIX = "ORCHESTRATOR_"
ENTRY_ORDER_TYPES = (OrderType.LIMIT, OrderType.MARKET)


class OrchestratorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # The worker process (python -m app.workers.trading_orchestrator). Running it does not
    # enable trading: entries also need the operator control ENABLED.
    worker_enabled: bool = False
    # Name of the paper account the orchestrator manages; empty = the PRIMARY account.
    account_name: str = ""
    tick_seconds: float = Field(default=30.0, ge=1, le=600)
    # Stages the orchestrator runs itself, in order, on each tick. With these on, the
    # stand-alone strategy and AI workers are not needed (running them too is harmless:
    # evaluations and intents are idempotent).
    run_strategy_stage: bool = True
    run_decision_stage: bool = True
    # A LIVE decision run is requested only when strategy proposals arrived since the last
    # LIVE run, and not more often than this.
    min_decision_interval_seconds: float = Field(default=300.0, ge=10, le=86_400)
    # Market sessions in which new entry intents are taken in. Daily strategies decide
    # after the close; their LIMIT orders then rest until the next regular session.
    entry_sessions: Annotated[tuple[MarketSession, ...], NoDecode] = (
        MarketSession.REGULAR,
        MarketSession.AFTER_HOURS,
        MarketSession.CLOSED,
    )
    # Decisions older than this are not taken in (a restart does not trade old signals).
    intake_lookback_minutes: int = Field(default=120, ge=1, le=7 * 24 * 60)
    intake_batch: int = Field(default=50, ge=1, le=500)
    # Entry order: LIMIT at the reserved worst entry price (as the 8A backtester) or MARKET
    # (the simulator still never fills beyond the reserved worst entry price).
    entry_order_type: OrderType = OrderType.LIMIT
    # Outbox delivery of intents.
    intent_max_attempts: int = Field(default=5, ge=1, le=20)
    # Scheduled jobs.
    job_lease_seconds: int = Field(default=600, ge=30, le=7200)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    risk_monitor_seconds: int = Field(default=300, ge=30, le=86_400)
    reservation_reconcile_seconds: int = Field(default=900, ge=60, le=86_400)
    execution_reconcile_seconds: int = Field(default=1800, ge=60, le=86_400)
    accounting_reconcile_seconds: int = Field(default=3600, ge=60, le=86_400)
    worker_health_seconds: int = Field(default=300, ge=60, le=86_400)
    # Daily, after the regular close plus this delay (bars settled): market intelligence
    # and the evaluation of past decisions' outcomes.
    after_close_delay_seconds: int = Field(default=3900, ge=0, le=6 * 3600)
    run_market_intelligence: bool = True
    run_decision_outcomes: bool = True
    decision_outcome_lookback_days: int = Field(default=60, ge=1, le=730)

    # ---------------------------------------------------------------- Phase 10B
    # Entry gate: no new entry while the position monitor or the paper execution worker
    # is not running (their heartbeats are stale). Only tests switch this off.
    require_protection_workers: bool = True
    # Backstops: while the position monitor is down, the orchestrator runs its protective
    # pass; while the paper execution worker has crashed, one delivery + simulation pass.
    backstop_position_monitor: bool = True
    backstop_paper_execution: bool = True
    # Daily jobs missed during an outage are caught up within this many trading days.
    catch_up_days: int = Field(default=3, ge=0, le=10)
    # Operational checks.
    data_freshness_seconds: int = Field(default=300, ge=60, le=86_400)
    session_open_lead_seconds: int = Field(default=1800, ge=0, le=6 * 3600)
    session_open_window_seconds: int = Field(default=3600, ge=60, le=6 * 3600)
    session_close_review_delay_seconds: int = Field(default=900, ge=0, le=6 * 3600)
    run_session_checks: bool = True
    # 8B degradation monitoring of approved strategies and models, daily after the close.
    # It only warns unless an operator approved a SUSPEND rule (it never enables anything).
    run_degradation_monitor: bool = True

    @field_validator("entry_sessions", mode="before")
    @classmethod
    def _split_sessions(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(v.strip().upper() for v in value.split(",") if v.strip())
        return value

    @field_validator("entry_order_type")
    @classmethod
    def _entry_type(cls, value: OrderType) -> OrderType:
        if value not in ENTRY_ORDER_TYPES:
            raise ValueError("entry_order_type must be LIMIT or MARKET")
        return value


def check_environment(settings: OrchestratorSettings, environment: str) -> None:
    """Explicit configuration outside development and test (Phase 10B, spec 24): in the
    paper environment the worker must name its account and may not switch off the
    protection gate. Raises :class:`InvalidSettingsError` (names only, never values)."""
    if environment != "paper" or not settings.worker_enabled:
        return
    problems = []
    if not settings.account_name.strip():
        problems.append("ORCHESTRATOR_ACCOUNT_NAME must name the paper account explicitly")
    if not settings.require_protection_workers:
        problems.append("ORCHESTRATOR_REQUIRE_PROTECTION_WORKERS cannot be false")
    if problems:
        raise InvalidSettingsError(
            "Orchestrator settings (paper environment): " + "; ".join(problems)
        )


@lru_cache(maxsize=1)
def get_orchestrator_settings() -> OrchestratorSettings:
    """Load once. Unknown ``ORCHESTRATOR_*`` keys are typos; errors never include values."""
    known = {f"{ENV_PREFIX}{name}".upper() for name in OrchestratorSettings.model_fields}
    reject_unknown_keys((ENV_PREFIX,), known)
    try:
        return OrchestratorSettings()
    except ValidationError as exc:
        message = describe_validation_error(exc, "Orchestrator settings")
        raise InvalidSettingsError(message) from None


__all__ = ["ENV_PREFIX", "OrchestratorSettings", "check_environment", "get_orchestrator_settings"]
