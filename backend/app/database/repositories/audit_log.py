"""Audit log persistence. The repository only appends; it never updates or deletes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import current_context
from app.core.security.redaction import redact_mapping
from app.database.models.audit_log import ActorType, AuditLog


class AuditLogRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        actor_type: ActorType,
        actor_id: str,
        action: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        reason: str | None = None,
        previous_state: dict[str, Any] | None = None,
        new_state: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Stage an audit record in the caller's transaction (committed with it)."""
        request_id = current_context().get("request_id")
        entry = AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            reason=reason,
            request_id=str(request_id) if request_id is not None else None,
            previous_state=redact_mapping(previous_state) if previous_state is not None else None,
            new_state=redact_mapping(new_state) if new_state is not None else None,
            details=redact_mapping(details or {}),
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def list_for_entity(self, entity_type: str, entity_id: str) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.id)
        )
        return self._session.scalars(stmt).all()
