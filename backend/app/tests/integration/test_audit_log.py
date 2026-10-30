from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.logging import bind_context, clear_context
from app.core.security.redaction import REDACTED
from app.database.models import ActorType
from app.database.repositories import AuditLogRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def entry(session):
    bind_context(request_id="req-audit-1")
    try:
        return AuditLogRepository(session).append(
            actor_type=ActorType.USER,
            actor_id="operator",
            action="risk_config.changed",
            entity_type="risk_configuration",
            entity_id="v1",
            reason="initial configuration",
            previous_state=None,
            new_state={"risk_per_trade": "0.005", "api_key": "should-not-persist"},
            details={"note": "connect via postgresql://u:pw@h/d"},
        )
    finally:
        clear_context()


def test_append_persists_with_context_and_redaction(session, entry):
    (stored,) = AuditLogRepository(session).list_for_entity("risk_configuration", "v1")

    assert stored.id == entry.id
    assert stored.request_id == "req-audit-1"
    assert stored.actor_type is ActorType.USER
    assert stored.new_state == {"risk_per_trade": "0.005", "api_key": REDACTED}
    assert stored.details == {"note": f"connect via postgresql://u:{REDACTED}@h/d"}
    session.refresh(stored)
    assert stored.occurred_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_logs SET reason = 'tampered' WHERE id = :id",
        "DELETE FROM audit_logs WHERE id = :id",
        "TRUNCATE audit_logs",
    ],
    ids=["update", "delete", "truncate"],
)
def test_audit_log_is_append_only(session, entry, statement):
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(text(statement), {"id": entry.id})


def test_audit_ids_are_generated_by_the_database(session, entry):
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "INSERT INTO audit_logs (id, actor_type, actor_id, action) "
                "VALUES (999999, 'SYSTEM', 'x', 'y')"
            )
        )
