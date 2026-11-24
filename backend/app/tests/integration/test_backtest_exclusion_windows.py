"""D-242: date-bounded corporate-action exclusions in the replay.

Bars, securities, universes and exclusions are TEST FIXTURES, not market data. FIXB's
fixture series halves on CONTAM, both RAW and split-adjusted: an unrecorded corporate
action the replay would read as a 50% loss.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.asset_discovery.sessions import SessionDay
from app.backtesting.data import sessions_between
from app.backtesting.engine import BacktestError
from app.backtesting.service import BacktestRequestError
from app.database.models import (
    BacktestFillRecord,
    BacktestRunEventRecord,
    BacktestTradeRecord,
    ResearchDataReservationRecord,
    ResearchExclusionRecord,
    ResearchPreregistrationRecord,
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
    ResearchUniverseRecord,
)
from app.database.sessions import session_scope
from app.research import store
from app.tests.integration.backtest_support import (
    RUN_END,
    RUN_START,
    Candle,
    make_bench,
    make_traded_asset,
    rows,
    seed_series,
    trend,
)
from app.tests.integration.test_backtest_live_pipeline import WIDE, completed

pytestmark = pytest.mark.integration

CODE = "RESEARCH_FIXTURE_V1"
RUN_DAYS = [s.day for s in sessions_between(RUN_START, RUN_END)]
CONTAM = RUN_DAYS[10]
BEFORE_CONTAM = RUN_DAYS[9]
FAR_PAST = date(2020, 3, 2)  # long before any history the fixture run reads


def halved(i: int, day: SessionDay) -> Candle:
    """MOCK: FIXB's trend, at half price from CONTAM on (an unrecorded corporate action)."""
    c = trend(i)
    if day.day < CONTAM:
        return c
    h = Decimal(2)
    return Candle(c.open / h, c.high / h, c.low / h, c.close / h)


def seed_universe(
    factory: Any, assets: dict[str, uuid.UUID], starts: dict[str, date] | None = None
) -> None:
    """FIXTURE universe: each asset a member from ``starts[symbol]`` (RUN_START) to RUN_END."""
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
                asset_id=asset_id,
                retrieved_at=now,
            )
            session.add(sec)
            session.flush()
            start = (starts or {}).get(symbol, RUN_START)
            session.add(
                ResearchUniverseCandidateRecord(
                    universe_code=CODE,
                    formation_session=start - timedelta(days=1),
                    symbol=symbol,
                    security_id=sec.id,
                    asset_id=asset_id,
                    sessions_observed=60,
                    eligible=True,
                    rank=1,
                    member=True,
                    effective_from=start,
                    effective_to=RUN_END,
                )
            )


def exclude_window(
    factory: Any,
    asset_id: uuid.UUID,
    symbol: str,
    day: date,
    *,
    code: str = "X2_FIXTURE",
    through: date | None = None,
) -> None:
    """FIXTURE WINDOW exclusion row (D-242)."""
    with session_scope(factory) as session:
        session.add(
            ResearchExclusionRecord(
                code=code,
                asset_id=asset_id,
                symbol=symbol,
                universe_code=CODE,
                scope="WINDOW",
                event_session=day,
                contaminated_from=day,
                contaminated_to=day,
                blocked_through=through,
                reason="fixture unrecorded corporate action",
                preregistration_code="X2_FIXTURE_PREREG",
                document_sha256="4" * 64,
                created_by="test",
            )
        )


def research_request(bench: Any, **extra: Any) -> Any:
    return bench.request(
        universe={"mode": "RESEARCH", "research_universe": CODE},
        risk=WIDE,
        correlation_source="replay-bars-v1",
        **extra,
    )


def db_now(bench: Any) -> datetime:
    """The database's clock, which stamps ``created_at`` (the host's may run ahead)."""
    with bench.session() as session:
        found = session.scalar(text("SELECT now()"))
    assert isinstance(found, datetime)
    return found


def known(at: datetime) -> dict[str, Any]:
    return {"data": {"data_known_at": at.isoformat()}}


def trades_of(bench: Any, run_id: uuid.UUID, symbol: str) -> list[Any]:
    return [t for t in rows(bench, BacktestTradeRecord, run_id) if t.symbol == symbol]


def fills_of(bench: Any, run_id: uuid.UUID, symbol: str) -> list[Any]:
    return [f for f in rows(bench, BacktestFillRecord, run_id) if f.symbol == symbol]


def two_assets(bench: Any, b_candles: Any = halved) -> tuple[uuid.UUID, uuid.UUID]:
    a = make_traded_asset(bench.factory, "FIXA")
    b = make_traded_asset(bench.factory, "FIXB")
    seed_series(bench.factory, a, lambda i, _d: trend(i))
    seed_series(bench.factory, b, b_candles)
    return a, b


def test_a_position_is_closed_before_the_contamination_and_never_held_over_it(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine)
    a, b = two_assets(bench)
    seed_universe(bench.factory, {"FIXA": a, "FIXB": b})
    before = db_now(bench)
    exclude_window(bench.factory, b, "FIXB", CONTAM)

    # Control: known before the registration, the replay holds FIXB over CONTAM and
    # books the artificial halving (the defect X2 removes). Stored runs reproduce.
    control = completed(bench, bench.run(research_request(bench, **known(before))))
    assert "exclusions" not in control.config
    held_over = [f for f in fills_of(bench, control.id, "FIXB") if f.session >= CONTAM]
    open_over = [
        t
        for t in trades_of(bench, control.id, "FIXB")
        if t.closed_at is None or t.closed_at.date() >= CONTAM
    ]
    assert held_over or open_over, "the fixture must hold FIXB over CONTAM without X2"

    run = completed(bench, bench.run(research_request(bench)))
    spec = run.config["exclusions"]
    assert spec["policy"] == "exit-before-contamination-block-read-window-v1"
    assert set(spec["sets"]) == {"X2_FIXTURE"}
    (window,) = spec["windows"]
    assert window["asset_id"] == str(b)
    assert window["contaminated_from"] == CONTAM.isoformat()
    assert window["blocked_from"] == BEFORE_CONTAM.isoformat()
    assert date.fromisoformat(window["blocked_through"]) >= RUN_END  # read window > run

    # No FIXB fill on or after the blocked session except the exclusion exit, which is
    # priced on BEFORE_CONTAM's close; nothing is open over CONTAM.
    fills = fills_of(bench, run.id, "FIXB")
    assert all(f.session <= BEFORE_CONTAM for f in fills)
    trades = trades_of(bench, run.id, "FIXB")
    assert trades
    assert all(t.status == "CLOSED" for t in trades)
    assert all(t.closed_at.date() <= CONTAM for t in trades)  # processed by CONTAM at latest
    excluded = [t for t in trades if t.exit_reason == "CA_EXCLUSION"]
    assert excluded, [(t.exit_reason, t.closed_at) for t in trades]
    close_before = trend(len(sessions_between(date(2026, 5, 1), BEFORE_CONTAM)) - 1).close
    for t in excluded:
        assert "date-bounded exclusion X2_FIXTURE" in " ".join(t.details["notes"])
        # Exited near the last clean close, nowhere near the halved price.
        assert t.exit_price is not None
        assert abs(t.exit_price / close_before - 1) < Decimal("0.01")
    events = [
        e
        for e in rows(bench, BacktestRunEventRecord, run.id)
        if e.symbol == "FIXB" and "date-bounded exclusion" in e.message
    ]
    assert events
    assert events[0].kind == "UNIVERSE_REMOVED"

    # The unaffected asset keeps trading through and after CONTAM.
    assert any(f.session > CONTAM for f in fills_of(bench, run.id, "FIXA"))


def test_a_window_outside_the_runs_reads_changes_no_result(fresh_engine: Engine) -> None:
    """Unaffected history stays in use: an exclusion long before anything the run reads
    leaves every trade as it was; only the configuration records the set."""
    bench = make_bench(fresh_engine)
    a, b = two_assets(bench, lambda i, _d: trend(i))
    seed_universe(bench.factory, {"FIXA": a, "FIXB": b})
    before = db_now(bench)
    exclude_window(bench.factory, b, "FIXB", FAR_PAST)
    control = completed(bench, bench.run(research_request(bench, **known(before))))
    run = completed(bench, bench.run(research_request(bench)))
    assert run.config["exclusions"]["windows"] == []
    assert set(run.config["exclusions"]["sets"]) == {"X2_FIXTURE"}

    def outcome(run_id: uuid.UUID) -> list[tuple[Any, ...]]:
        return [
            (t.symbol, t.opened_at, t.closed_at, t.entry_quantity, t.net_pnl, t.exit_reason)
            for t in sorted(rows(bench, BacktestTradeRecord, run_id), key=lambda t: t.seq)
        ]

    assert outcome(run.id) == outcome(control.id)
    assert any(t[0] == "FIXB" for t in outcome(run.id))


def test_an_event_before_the_membership_span_blocks_the_span_it_contaminates(
    fresh_engine: Engine,
) -> None:
    """Membership transition: FIXB joins the universe at RUN_DAYS[12], after its
    contaminated session RUN_DAYS[3]. No position can span the event, but every decision
    whose read window contains it is blocked, so FIXB is not traded on joining."""
    bench = make_bench(fresh_engine)
    event, joins = RUN_DAYS[3], RUN_DAYS[12]
    a, b = two_assets(bench, lambda i, _d: trend(i))
    seed_universe(bench.factory, {"FIXA": a, "FIXB": b}, starts={"FIXB": joins})
    before = db_now(bench)
    exclude_window(bench.factory, b, "FIXB", event)
    control = completed(bench, bench.run(research_request(bench, **known(before))))
    assert any(f.session >= joins for f in fills_of(bench, control.id, "FIXB"))
    assert not any(f.session < joins for f in fills_of(bench, control.id, "FIXB"))
    run = completed(bench, bench.run(research_request(bench)))
    assert fills_of(bench, run.id, "FIXB") == []
    assert any(f.session > joins for f in fills_of(bench, run.id, "FIXA"))


def test_a_reserved_explicit_run_reading_a_window_is_refused(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    a, b = two_assets(bench)
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
    exclude_window(bench.factory, b, "FIXB", CONTAM)
    exclude_window(bench.factory, a, "FIXA", FAR_PAST, code="X2_FIXTURE_OLD")
    both = bench.request(
        risk=WIDE,
        preregistration="PREREG_OK",
        universe={"mode": "EXPLICIT", "symbols": ["FIXA", "FIXB"]},
    )
    with pytest.raises(BacktestRequestError, match=r"contaminated corporate-action.*FIXB \("):
        bench.service.create(both, actor="operator:test")
    # FIXA's window is long before anything the run reads: it resolves, untouched.
    with bench.session() as session:
        config = bench.service.resolve(
            session, bench.request(risk=WIDE, preregistration="PREREG_OK")
        )
    assert [x.symbol for x in config.assets] == ["FIXA"]
    assert config.exclusions is not None
    assert config.exclusions.windows == ()
    assert set(config.exclusions.sets) == {"X2_FIXTURE", "X2_FIXTURE_OLD"}


def test_the_replay_refuses_exclusion_sets_other_than_the_registered_ones(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine)
    a, b = two_assets(bench)
    seed_universe(bench.factory, {"FIXA": a, "FIXB": b})
    exclude_window(bench.factory, b, "FIXB", CONTAM)
    with bench.session() as session:
        config = bench.service.resolve(session, research_request(bench))
        assert config.exclusions is not None
        tampered = config.model_copy(
            update={
                "exclusions": config.exclusions.model_copy(
                    update={"sets": {"X2_FIXTURE": "0" * 64}}
                )
            }
        )
        with pytest.raises(BacktestError, match="exclusion sets"):
            bench.service.replay(session, tampered)


def test_set_hashes_are_pinned_by_knowledge_time(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    a, b = two_assets(bench)
    exclude_window(bench.factory, b, "FIXB", CONTAM)
    first = db_now(bench)
    exclude_window(bench.factory, a, "FIXA", CONTAM, code="X3_FIXTURE")
    with bench.session() as session:
        then = store.exclusion_sets(session, known_at=first)
        now = store.exclusion_sets(session, known_at=datetime.now(UTC))
        windows = store.exclusion_windows(session, known_at=datetime.now(UTC))
        assets = store.exclusions(session, known_at=datetime.now(UTC))
    assert set(then) == {"X2_FIXTURE"}
    assert set(now) == {"X2_FIXTURE", "X3_FIXTURE"}
    assert then["X2_FIXTURE"] == now["X2_FIXTURE"]  # adding a set changes no other set
    assert set(windows) == {a, b}
    assert assets == {}  # WINDOW rows never remove an asset wholesale


def test_window_rows_are_checked_and_append_only(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    exclude_window(bench.factory, a, "FIXA", CONTAM)
    # A second event of the same asset in the same set (UA's case) is allowed...
    exclude_window(bench.factory, a, "FIXA", RUN_DAYS[2])
    # ...the same event twice is not.
    with pytest.raises(IntegrityError):
        exclude_window(bench.factory, a, "FIXA", CONTAM)
    base = {
        "code": "X_BAD",
        "asset_id": a,
        "symbol": "FIXA",
        "universe_code": CODE,
        "event_session": CONTAM,
        "reason": "fixture",
        "preregistration_code": "P",
        "document_sha256": "5" * 64,
        "created_by": "test",
    }
    bad = (
        {"scope": "WINDOW"},  # no contaminated sessions
        {"scope": "ASSET", "contaminated_from": CONTAM, "contaminated_to": CONTAM},
        {"scope": "WINDOW", "contaminated_from": RUN_DAYS[11], "contaminated_to": RUN_DAYS[12]},
        {"scope": "WINDOW", "contaminated_from": CONTAM, "contaminated_to": RUN_DAYS[9]},
        {
            "scope": "WINDOW",
            "contaminated_from": CONTAM,
            "contaminated_to": CONTAM,
            "blocked_through": RUN_DAYS[9],
        },
        {"scope": "OTHER"},
    )
    for extra in bad:
        with pytest.raises(IntegrityError), session_scope(bench.factory) as session:
            session.add(ResearchExclusionRecord(**base, **extra))
    for statement in (
        "UPDATE research_exclusions SET blocked_through = NULL",
        "DELETE FROM research_exclusions",
        "TRUNCATE research_exclusions",
    ):
        with pytest.raises(DBAPIError), session_scope(bench.factory) as session:
            session.execute(text(statement))
