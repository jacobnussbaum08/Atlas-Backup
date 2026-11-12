"""Phase 10A orchestration tables (migration 0020). Paper trading only.

* ``trading_control_events``: the operator's trading mode per paper account, append-only and
  numbered. No row means DISABLED.
* ``trade_intents``: the inbox of prioritized AI decisions. One row per (account, decision)
  (UNIQUE), written with its outbox event in one transaction. Its status moves once, from
  RECEIVED to a final outcome, and never changes after (``atlas_trade_intent_guard``).
* ``orchestration_jobs``: scheduled jobs, one row per (kind, slot) (UNIQUE), claimed under a
  lease with a heartbeat; an expired lease is a stuck job and is reclaimed. Never deleted.
* ``orchestrator_status``: the orchestrator's heartbeat-updated state (one row per name).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, str_enum
from app.orchestration.enums import (
    ControlAction,
    IntentStatus,
    JobKind,
    JobStatus,
    OrchestratorState,
    TradingMode,
)

_ACCOUNT_FK = "portfolio_accounts.id"


class TradingControlEventRecord(Base):
    __tablename__ = "trading_control_events"
    __table_args__ = (
        UniqueConstraint("portfolio_account_id", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("actor ~ '^operator:[A-Za-z0-9_.@-]+$'", name="operator_only"),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_given"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(_ACCOUNT_FK, ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    action: Mapped[ControlAction] = mapped_column(str_enum(ControlAction, "trading_control_action"))
    from_mode: Mapped[TradingMode] = mapped_column(str_enum(TradingMode, "trading_mode_from"))
    to_mode: Mapped[TradingMode] = mapped_column(str_enum(TradingMode, "trading_mode_to"))
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    occurred_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TradeIntentRecord(Base):
    __tablename__ = "trade_intents"
    __table_args__ = (
        UniqueConstraint("portfolio_account_id", "ai_decision_id"),
        Index(None, "status", "received_at"),
        Index(None, "portfolio_account_id", "received_at"),
        CheckConstraint("(status = 'RECEIVED') = (decided_at IS NULL)", name="decided_iff_final"),
        CheckConstraint(
            "status <> 'ORDER_CREATED' OR (order_id IS NOT NULL AND reservation_id IS NOT NULL)",
            name="order_created_has_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(_ACCOUNT_FK, ondelete="RESTRICT")
    )
    ai_decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="RESTRICT")
    )
    decision_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("decision_runs.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[str | None] = mapped_column(String(8))
    decision_expires_at: Mapped[datetime]
    status: Mapped[IntentStatus] = mapped_column(str_enum(IntentStatus, "trade_intent_status"))
    trading_mode: Mapped[TradingMode | None] = mapped_column(
        str_enum(TradingMode, "trade_intent_mode")
    )
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_reservations.id", ondelete="RESTRICT")
    )
    reservation_decision: Mapped[str | None] = mapped_column(String(16))
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    execution_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("execution_requests.id", ondelete="RESTRICT")
    )
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrchestrationJobRecord(Base):
    __tablename__ = "orchestration_jobs"
    __table_args__ = (
        UniqueConstraint("kind", "slot"),
        Index(None, "status", "next_attempt_at"),
        Index(None, "kind", "created_at"),
        CheckConstraint("attempts >= 0", name="attempts_valid"),
        CheckConstraint(
            "status <> 'RUNNING' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="running_has_lease",
        ),
        CheckConstraint(
            "status NOT IN ('SUCCEEDED', 'DEAD') OR finished_at IS NOT NULL",
            name="final_has_finish",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[JobKind] = mapped_column(str_enum(JobKind, "orchestration_job_kind"))
    slot: Mapped[str] = mapped_column(String(64))  # e.g. an interval start or a trading day
    portfolio_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(_ACCOUNT_FK, ondelete="RESTRICT")
    )
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus, "orchestration_job_status"))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(96))
    lease_expires_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    reclaimed: Mapped[int] = mapped_column(Integer, server_default="0")  # stuck-lease recoveries
    next_attempt_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OrchestratorStatusRecord(Base):
    __tablename__ = "orchestrator_status"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    worker_id: Mapped[str | None] = mapped_column(String(96))
    portfolio_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(_ACCOUNT_FK, ondelete="RESTRICT")
    )
    state: Mapped[OrchestratorState] = mapped_column(
        str_enum(OrchestratorState, "orchestrator_state")
    )
    trading_mode: Mapped[TradingMode | None] = mapped_column(
        str_enum(TradingMode, "orchestrator_trading_mode")
    )
    heartbeat_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    stopped_at: Mapped[datetime | None]
    last_tick_at: Mapped[datetime | None]
    last_tick_ms: Mapped[int | None] = mapped_column(Integer)
    counters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


__all__ = [
    "OrchestrationJobRecord",
    "OrchestratorStatusRecord",
    "TradeIntentRecord",
    "TradingControlEventRecord",
]
