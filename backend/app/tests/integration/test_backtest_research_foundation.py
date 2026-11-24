"""D-239: research universes, delisting exits, replay correlations and data reservations.

Bars, securities, universes and reservations are TEST FIXTURES, not market data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from app.backtesting.data import sessions_between
from app.backtesting.service import BacktestRequestError
from app.database.models import (
    BacktestFillRecord,
    BacktestTradeRecord,
    ResearchDataReservationRecord,
    ResearchExclusionRecord,
    ResearchPreregistrationRecord,
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
    ResearchUniverseRecord,
)
from app.database.sessions import session_scope
from app.tests.integration.backtest_support import (
    RUN_END,
    RUN_START,
    make_bench,
    make_traded_asset,
    rows,
    seed_series,
    trend,
)
from app.tests.integration.test_backtest_live_pipeline import WIDE, completed

pytestmark = pytest.mark.integration

CODE = "RESEARCH_FIXTURE_V1"
DELISTED_ON = sessions_between(RUN_START, RUN_END)[10].day


def seed_research(factory: Any, assets: dict[str, uuid.UUID], delisted: str) -> None:
    """FIXTURE universe: every asset a member over the run; ``delisted`` stops trading."""
    now = datetime.now(UTC)
    with session_scope(factory) as session:
        session.add(
            ResearchUniverseRecord(
                code=CODE,
                version=1,
                description="fixture",
                rule={"fixture": True},
                rule_sha256="0" * 64,
                created_by="test",
            )
        )
        for symbol, asset_id in sorted(assets.items()):
            sec = ResearchSecurityRecord(
                provider="alpaca",
                symbol=symbol,
                revision=0,
                provider_status="active",
                security_type="STOCK",
                type_basis="fixture",
                scan_start=RUN_START,
                scan_end=RUN_END,
                first_bar=RUN_START,
                last_bar=RUN_END,
                delisted_on=DELISTED_ON if symbol == delisted else None,
                delisting_basis="fixture" if symbol == delisted else None,
                asset_id=asset_id,
                retrieved_at=now,
            )
            session.add(sec)
            session.flush()
            session.add(
                ResearchUniverseCandidateRecord(
                    universe_code=CODE,
                    formation_session=RUN_START - timedelta(days=1),
                    symbol=symbol,
                    security_id=sec.id,
                    asset_id=asset_id,
                    sessions_observed=60,
                    eligible=True,
                    rank=1,
                    member=True,
                    effective_from=RUN_START,
                    effective_to=RUN_END,
                )
            )


def research_request(bench: Any, **extra: Any) -> Any:
    return bench.request(
        universe={"mode": "RESEARCH", "research_universe": CODE},
        risk=WIDE,
        correlation_source="replay-bars-v1",
        **extra,
    )


def test_a_research_universe_run_exits_delisted_positions_at_the_last_trade(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    b = make_traded_asset(bench.factory, "FIXB")
    seed_series(bench.factory, a, lambda i, _d: trend(i))
    # FIXB stops trading after DELISTED_ON (no bar afterwards).
    seed_series(bench.factory, b, lambda i, d: trend(i) if d.day <= DELISTED_ON else None)
    seed_research(bench.factory, {"FIXA": a, "FIXB": b}, delisted="FIXB")
    run_id = bench.run(research_request(bench))
    run = completed(bench, run_id)
    assert run.config["universe"]["research_universe"] == CODE
    assert run.config["delisting_policy"] == "exit-at-last-trade-v1"
    assert run.config["correlation_source"] == "replay-bars-v1"
    trades = [t for t in rows(bench, BacktestTradeRecord, run_id) if t.symbol == "FIXB"]
    assert trades, "the fixture must hold FIXB at some point"
    # Nothing in FIXB stays open past its last trade, and nothing fills after it except the
    # delisting exit, which is priced on the last trading session.
    for t in trades:
        assert t.status == "CLOSED"
    fills = [f for f in rows(bench, BacktestFillRecord, run_id) if f.symbol == "FIXB"]
    assert all(f.session <= DELISTED_ON for f in fills)
    delisted = [t for t in trades if t.exit_reason == "DELISTED"]
    assert delisted, [(t.exit_reason, t.status) for t in trades]
    for t in delisted:
        assert "delisted" in " ".join(t.details["notes"])


def test_runs_without_research_options_keep_their_configuration_shape(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, a, lambda i, _d: trend(i))
    run = completed(bench, bench.run(bench.request(risk=WIDE)))
    for key in ("delisting_policy", "correlation_source", "preregistration"):
        assert key not in run.config
    assert "research_universe" not in run.config["universe"]


def test_reserved_data_needs_a_covering_preregistration(
    fresh_engine: Engine, tmp_path: Path
) -> None:
    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, a, lambda i, _d: trend(i))
    with session_scope(bench.factory) as session:
        session.add(
            ResearchDataReservationRecord(
                code="V1_FIXTURE",
                scope="PERIOD",
                description="fixture reservation",
                start_date=RUN_START,
                end_date=RUN_END,
                symbols=[],
                created_by="test",
            )
        )
        session.add(
            ResearchDataReservationRecord(
                code="EXPLORE_FIXTURE",
                scope="EXPLORATION_ONLY",
                description="never blocks",
                start_date=date(2000, 1, 1),
                end_date=date(2100, 1, 1),
                symbols=["FIXA"],
                created_by="test",
            )
        )
        session.add(
            ResearchPreregistrationRecord(
                code="PREREG_OTHER",
                document_path="x",
                document_sha256="1" * 64,
                reservation_codes=["SOMETHING_ELSE"],
                created_by="test",
            )
        )
        session.add(
            ResearchPreregistrationRecord(
                code="PREREG_OK",
                document_path="x",
                document_sha256="2" * 64,
                reservation_codes=["V1_FIXTURE"],
                created_by="test",
            )
        )
    with pytest.raises(BacktestRequestError, match="reserved"):
        bench.service.create(bench.request(risk=WIDE), actor="operator:test")
    with pytest.raises(BacktestRequestError, match="does not cover"):
        bench.service.create(
            bench.request(risk=WIDE, preregistration="PREREG_OTHER"), actor="operator:test"
        )
    with pytest.raises(BacktestRequestError, match="not registered"):
        bench.service.create(
            bench.request(risk=WIDE, preregistration="NOPE"), actor="operator:test"
        )
    run = completed(bench, bench.run(bench.request(risk=WIDE, preregistration="PREREG_OK")))
    assert run.config["preregistration"] == {"code": "PREREG_OK", "document_sha256": "2" * 64}


def test_a_later_identity_record_decides_the_delisting(fresh_engine: Engine) -> None:
    """FIXTURE: an asset carries master revisions 0-3 without a delisting, then its
    point-in-time identity record (revision 0 of its own chain) with one: the replay's
    lookup must take the newer record, not the higher revision number (D-239, MBLY/TMH)."""
    from app.research import store

    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    now = datetime.now(UTC)
    common = {
        "provider_status": "active",
        "security_type": "STOCK",
        "type_basis": "fixture",
        "scan_start": RUN_START,
        "scan_end": RUN_END,
        "asset_id": a,
        "retrieved_at": now,
    }
    with session_scope(bench.factory) as session:
        for rev in range(4):
            session.add(
                ResearchSecurityRecord(provider="alpaca", symbol="FIXA", revision=rev, **common)
            )
    with session_scope(bench.factory) as session:
        session.add(
            ResearchSecurityRecord(
                provider="alpaca-asof",
                symbol="FIXA",
                revision=0,
                delisted_on=DELISTED_ON,
                delisting_basis="fixture",
                **common,
            )
        )
    with session_scope(bench.factory) as session:
        found = store.delistings(session, [a], known_at=datetime.now(UTC))
        before = store.delistings(session, [a], known_at=now - timedelta(days=1))
    assert found == {a: DELISTED_ON}
    assert before == {}  # nothing was known before the records existed


def exclude(factory: Any, asset_id: uuid.UUID, symbol: str) -> None:
    """FIXTURE exclusion row (D-240)."""
    with session_scope(factory) as session:
        session.add(
            ResearchExclusionRecord(
                code="X_FIXTURE",
                asset_id=asset_id,
                symbol=symbol,
                universe_code=CODE,
                scope="ASSET",
                event_session=DELISTED_ON,
                reason="fixture phantom split",
                preregistration_code="X_FIXTURE_PREREG",
                document_sha256="3" * 64,
                created_by="test",
            )
        )


def test_an_excluded_asset_leaves_the_research_universe_from_its_registration(
    fresh_engine: Engine,
) -> None:
    """D-240: the replay's member lookup drops an excluded asset for runs known after the
    exclusion, keeps it for runs known before (stored runs reproduce), and the resolved
    research run never lists it, so its bars are never loaded, decided on or held."""
    from app.research import store

    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    b = make_traded_asset(bench.factory, "FIXB")
    seed_series(bench.factory, a, lambda i, _d: trend(i))
    seed_series(bench.factory, b, lambda i, _d: trend(i))
    seed_research(bench.factory, {"FIXA": a, "FIXB": b}, delisted="")
    before = datetime.now(UTC)
    exclude(bench.factory, b, "FIXB")
    now = datetime.now(UTC)
    with bench.session() as session:
        assert set(store.universe_members(session, CODE, known_at=now)) == {a}
        assert set(store.universe_members(session, CODE, known_at=before)) == {a, b}
        formed = store.universe_members(session, CODE, known_at=now, apply_exclusions=False)
        assert set(formed) == {a, b}
        assert store.exclusions(session, known_at=now) == {b: ["X_FIXTURE"]}
        config = bench.service.resolve(session, research_request(bench))
    assert [x.symbol for x in config.assets] == ["FIXA"]


def test_a_reserved_run_naming_an_excluded_asset_is_refused(fresh_engine: Engine) -> None:
    """D-240: an explicit run on reserved data is refused, not narrowed, when it names an
    excluded asset; the same run without it resolves."""
    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    b = make_traded_asset(bench.factory, "FIXB")
    seed_series(bench.factory, a, lambda i, _d: trend(i))
    seed_series(bench.factory, b, lambda i, _d: trend(i))
    with session_scope(bench.factory) as session:
        session.add(
            ResearchDataReservationRecord(
                code="V1_FIXTURE",
                scope="PERIOD",
                description="fixture reservation",
                start_date=RUN_START,
                end_date=RUN_END,
                symbols=[],
                created_by="test",
            )
        )
        session.add(
            ResearchPreregistrationRecord(
                code="PREREG_OK",
                document_path="x",
                document_sha256="2" * 64,
                reservation_codes=["V1_FIXTURE"],
                created_by="test",
            )
        )
    exclude(bench.factory, b, "FIXB")
    both = bench.request(
        risk=WIDE,
        preregistration="PREREG_OK",
        universe={"mode": "EXPLICIT", "symbols": ["FIXA", "FIXB"]},
    )
    with pytest.raises(
        BacktestRequestError, match=r"excluded from research use: FIXB \(X_FIXTURE\)"
    ):
        bench.service.create(both, actor="operator:test")
    with bench.session() as session:
        config = bench.service.resolve(
            session, bench.request(risk=WIDE, preregistration="PREREG_OK")
        )
    assert [x.symbol for x in config.assets] == ["FIXA"]


def test_exclusions_are_append_only(fresh_engine: Engine) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    exclude(bench.factory, a, "FIXA")
    for statement in (
        "UPDATE research_exclusions SET reason = 'x'",
        "DELETE FROM research_exclusions",
        "TRUNCATE research_exclusions",
    ):
        with pytest.raises(DBAPIError), session_scope(bench.factory) as session:
            session.execute(text(statement))
