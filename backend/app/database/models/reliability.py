"""Phase 10B reliability and operator tables (migration 0021). Paper trading only.

* ``worker_heartbeats``: one row per (worker, instance), renewed while the process runs. The
  position monitor writes it (before 10B its liveness could not be observed at all); the
  dashboard and the orchestrator's protection gate judge it by age.
* ``operator_credentials``: named operators for the API. Only the SHA-256 digest of each
  token is stored; the token is shown once when it is issued. Never deleted; a revoked
  credential stays as the record of who could act and when.
* ``alert_acknowledgements``: an operator acknowledged a ``system_health_events`` alert.
  Append-only; acknowledging never changes or hides the alert itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, str_enum
from app.orchestration.enums import WorkerState

OPERATOR_NAME = r"^[A-Za-z0-9_.@-]{2,64}$"


class WorkerHeartbeatRecord(Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (Index(None, "worker", "heartbeat_at"),)

    worker: Mapped[str] = mapped_column(String(64), primary_key=True)  # Compose service name
    instance_id: Mapped[str] = mapped_column(String(96), primary_key=True)  # host:pid:uuid
    state: Mapped[WorkerState] = mapped_column(str_enum(WorkerState, "worker_heartbeat_state"))
    interval_seconds: Mapped[Decimal | None]
    started_at: Mapped[datetime]
    heartbeat_at: Mapped[datetime]
    stopped_at: Mapped[datetime | None]
    passes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OperatorCredentialRecord(Base):
    __tablename__ = "operator_credentials"
    __table_args__ = (
        UniqueConstraint("name"),
        UniqueConstraint("token_sha256"),
        CheckConstraint(f"name ~ '{OPERATOR_NAME}'", name="name_valid"),
        CheckConstraint("token_sha256 ~ '^[0-9a-f]{64}$'", name="digest_valid"),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)", name="revocation_has_operator"
        ),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_given"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(64))  # the actor is "operator:<name>"
    token_sha256: Mapped[str] = mapped_column(String(64))
    # Least privilege: only credentials with this flag may use the trading controls
    # (pause, disable, emergency stop, kill switch). None of them can enable trading.
    can_control_trading: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    revoked_by: Mapped[str | None] = mapped_column(String(128))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None]


class AlertAcknowledgementRecord(Base):
    __tablename__ = "alert_acknowledgements"
    __table_args__ = (
        Index(None, "health_event_id"),
        CheckConstraint("actor ~ '^operator:[A-Za-z0-9_.@-]+$'", name="operator_only"),
        CheckConstraint("length(btrim(note)) >= 3", name="note_given"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    health_event_id: Mapped[int] = mapped_column(
        ForeignKey("system_health_events.id", ondelete="RESTRICT")
    )
    actor: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text)
    acknowledged_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "OPERATOR_NAME",
    "AlertAcknowledgementRecord",
    "OperatorCredentialRecord",
    "WorkerHeartbeatRecord",
]
