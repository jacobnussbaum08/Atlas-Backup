"""X4.3.1 (D-248): registering the ledger V2 beside V1 (migration 0028), from FIXTURE stores.

The fixture filings, listings and bars are hand-written test data, not evidence.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select

from app.database.models import SecurityLifecycleEventRecord, SecurityLifecycleSetRecord
from app.database.sessions import build_session_factory
from app.research import ledger_v2 as l2
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.tests.unit.test_entity_resolution import stores  # noqa: F401 (pytest fixture)

pytestmark = pytest.mark.integration


def test_v2_registers_beside_v1_and_supersedes_it(fresh_engine: Engine, stores, tmp_path):  # noqa: F811
    s, s43 = stores
    factory = build_session_factory(fresh_engine)
    ledger = lc.build(s)
    v1_path = tmp_path / "v1.json"
    lc.write(ledger, v1_path)
    lc.register(factory, v1_path)
    graph = rs.build(s, s43, ledger, ledger_sha256="fixture")
    doc = l2.build(s, s43, ledger, graph, ledger_sha256="fixture", graph_sha256="fixture")
    v2_path = tmp_path / "v2.json.gz"
    l2.write(doc, v2_path)
    got = l2.register(factory, v2_path)
    assert got["events"] == len(doc["events"])
    assert got["content_sha256"] == doc["content_sha256"]
    unchanged = sum(1 for d in doc["v1_events"].values() if d["disposition"] == "CARRIED")
    assert got["added_events"] == len(doc["events"]) - unchanged
    with factory() as session:
        sets = {r.code: r for r in session.scalars(select(SecurityLifecycleSetRecord))}
        assert set(sets) == {lc.SET_CODE, l2.SET_CODE}
        assert sets[l2.SET_CODE].rule["supersedes"]["set_code"] == lc.SET_CODE
        assert len(sets[l2.SET_CODE].members) == len(doc["events"])
        # V1's rows are all still there: nothing is deleted, revisions sit beside them.
        total = session.scalar(select(func.count()).select_from(SecurityLifecycleEventRecord))
        assert total == len(ledger["events"]) + got["added_events"]
    with pytest.raises(ValueError, match="already registered"):
        l2.register(factory, v2_path)
