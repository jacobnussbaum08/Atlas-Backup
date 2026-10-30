"""Append-only audit log (spec sections 14.7, 21, 24).

Rows are immutable: a database trigger (created in migration 0001) rejects
UPDATE, DELETE and TRUNCATE, so the guarantee holds even for code that bypasses
the repository layer.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, str_enum


class ActorType(StrEnum):
    USER = "USER"
    SYSTEM = "SYSTEM"
    SUBSYSTEM = "SUBSYSTEM"


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index(None, "occurred_at"),
        Index(None, "entity_type", "entity_id"),
        Index(None, "action"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor_type: Mapped[ActorType] = mapped_column(str_enum(ActorType, "audit_actor_type"))
    actor_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))
    previous_state: Mapped[dict[str, Any] | None]
    new_state: Mapped[dict[str, Any] | None]
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
