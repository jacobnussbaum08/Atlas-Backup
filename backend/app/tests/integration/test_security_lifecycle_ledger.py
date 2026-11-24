"""X4.2 (D-246): the lifecycle tables (migration 0028) and ledger registration.

The ledger here is a FIXTURE document, not the evidence-built ledger.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.database.models import (
    SecurityEntityRecord,
    SecurityIdentityRecord,
    SecurityLifecycleEventRecord,
    SecurityLifecycleSetRecord,
)
from app.database.sessions import build_session_factory, session_scope
from app.research import lifecycle as lc
from app.tests.integration.conftest import alembic_config

pytestmark = pytest.mark.integration
TABLES = (
    "security_entities",
    "security_identities",
    "security_lifecycle_events",
    "security_lifecycle_sets",
)


def event(code: str, key: str, etype: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {k: None for k in lc.EVENT_FIELDS}
    base.update(
        code=code,
        revision=0,
        security_key=key,
        event_type=etype,
        effective_precision="UNKNOWN",
        confidence="HIGH",
        resolution_status="CONFIRMED",
        explanation="fixture",
        evidence=[{"artifact": "fixture", "sha256": "0" * 64, "locator": code}],
    )
    base.update(kw)
    base["evidence_sha256"] = "1" * 64
    return base


def ledger(tmp_path: Path) -> Path:
    events = [
        event(
            "A1",
            "CIK:0000000001|COMMON",
            "LISTING_OBSERVED",
            ticker="AAA",
            venue="NYSE",
            effective_precision="EXACT",
            effective_date="2017-06-08",
        ),
        event(
            "A2",
            "CIK:0000000001|COMMON",
            "EXCHANGE_TRANSFER",
            ticker="AAA",
            from_venue="NYSE",
            to_venue="NASDAQ",
            effective_precision="BOUNDED",
            not_before="2017-06-08",
            not_after="2017-12-16",
            filing_date="2017-09-19",
        ),
        event(
            "B1",
            "CIK:0000000002|ADS",
            "DELISTING",
            filing_date="2018-12-31",
            reason="CLASS_CONVERTED_BY_OPERATION_OF_LAW",
        ),
    ]
    doc = {
        "set_code": "SECURITY_LIFECYCLE_FIXTURE",
        "evidence_manifest_sha256": "2" * 64,
        "entities": [
            {
                "entity_key": f"CIK:000000000{i}",
                "cik": f"000000000{i}",
                "name": f"Fixture {i}",
                "name_source": "fixture",
                "evidence": [],
            }
            for i in (1, 2)
        ],
        "securities": [
            {
                "security_key": "CIK:0000000001|COMMON",
                "entity_key": "CIK:0000000001",
                "sec_class_id": None,
                "class_label": "COMMON",
                "class_description": "Common Stock",
                "evidence": [],
            },
            {
                "security_key": "CIK:0000000002|ADS",
                "entity_key": "CIK:0000000002",
                "sec_class_id": None,
                "class_label": "ADS",
                "class_description": None,
                "evidence": [],
            },
        ],
        "events": events,
        "content_sha256": lc.content_sha256(events),
    }
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_ledger_registers_once_with_its_recomputed_hash(fresh_engine: Engine, tmp_path: Path):
    factory = build_session_factory(fresh_engine)
    path = ledger(tmp_path)
    got = lc.register(factory, path)
    assert got["events"] == 3
    with factory() as s:
        st = s.scalars(select(SecurityLifecycleSetRecord)).one()
        assert st.content_sha256 == json.loads(path.read_text())["content_sha256"]
        assert len(st.members) == 3
        transfer = s.scalars(
            select(SecurityLifecycleEventRecord).where(SecurityLifecycleEventRecord.code == "A2")
        ).one()
        assert transfer.effective_date is None
        assert transfer.not_before == date(2017, 6, 8)
    with pytest.raises(ValueError, match="already registered"):
        lc.register(factory, path)
    tampered = json.loads(path.read_text())
    tampered["events"][0]["ticker"] = "ZZZ"
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="do not hash"):
        lc.register(factory, bad)


def test_the_tables_refuse_changes(fresh_engine: Engine, tmp_path: Path):
    factory = build_session_factory(fresh_engine)
    lc.register(factory, ledger(tmp_path))
    for table in TABLES:
        with pytest.raises(DBAPIError), fresh_engine.begin() as conn:
            conn.execute(text(f"UPDATE {table} SET created_by = 'x'"))
        with pytest.raises(DBAPIError), fresh_engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {table}"))
        with pytest.raises(DBAPIError), fresh_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {table} CASCADE"))


@pytest.mark.parametrize(
    "bad",
    [
        {"effective_precision": "EXACT"},  # a precise date that is not there
        {"effective_precision": "UNKNOWN", "not_before": date(2018, 1, 1)},  # unknown, yet bounded
        {"effective_precision": "BOUNDED"},  # bounded by nothing
        {
            "effective_precision": "BOUNDED",
            "not_before": date(2018, 2, 1),
            "not_after": date(2018, 1, 1),
        },  # out of order
        {"effective_precision": "UNKNOWN", "effective_date": date(2018, 1, 1)},  # a date, not exact
        {"event_type": "EXCHANGE_TRANSFER", "from_venue": "NYSE", "to_venue": "NYSE"},
        {"event_type": "TICKER_CHANGE", "from_ticker": "AAA", "to_ticker": None},
        {"resolution_status": "MAYBE"},
    ],
)
def test_the_database_enforces_temporal_and_status_rules(fresh_engine: Engine, bad: dict[str, Any]):
    factory = build_session_factory(fresh_engine)
    with session_scope(factory) as s:
        s.add(
            SecurityEntityRecord(
                entity_key="CIK:0000000009",
                cik="0000000009",
                name="F",
                name_source="fixture",
                evidence=[],
                created_by="test",
            )
        )
        s.flush()
        s.add(
            SecurityIdentityRecord(
                security_key="S",
                entity_key="CIK:0000000009",
                class_label="COMMON",
                evidence=[],
                created_by="test",
            )
        )
    values: dict[str, Any] = {
        "code": "X",
        "security_key": "S",
        "event_type": "DELISTING",
        "effective_precision": "UNKNOWN",
        "confidence": "HIGH",
        "resolution_status": "CONFIRMED",
        "explanation": "fixture",
        "evidence": [],
        "evidence_sha256": "0" * 64,
        "created_by": "test",
    }
    values.update(bad)
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(SecurityLifecycleEventRecord(**values))


def test_an_event_needs_a_known_security(fresh_engine: Engine):
    factory = build_session_factory(fresh_engine)
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(
            SecurityLifecycleEventRecord(
                code="X",
                security_key="nowhere",
                event_type="DELISTING",
                effective_precision="UNKNOWN",
                confidence="HIGH",
                resolution_status="CONFIRMED",
                explanation="fixture",
                evidence=[],
                evidence_sha256="0" * 64,
                created_by="test",
            )
        )


def test_the_downgrade_refuses_a_populated_ledger_and_round_trips_an_empty_one(
    fresh_engine: Engine, tmp_path: Path
):
    url = fresh_engine.url
    fresh_engine.dispose()
    command.downgrade(alembic_config(url), "0027")  # empty: the tables go
    assert not set(TABLES) & set(inspect(fresh_engine).get_table_names())
    command.upgrade(alembic_config(url), "head")
    assert set(TABLES) <= set(inspect(fresh_engine).get_table_names())
    lc.register(build_session_factory(fresh_engine), ledger(tmp_path))
    fresh_engine.dispose()
    with pytest.raises(RuntimeError, match="refusing to drop it"):
        command.downgrade(alembic_config(url), "0027")
    with fresh_engine.connect() as conn:
        assert conn.scalar(text("select version_num from alembic_version")) == "0028"
        assert conn.scalar(text("select count(*) from security_lifecycle_events")) == 3
