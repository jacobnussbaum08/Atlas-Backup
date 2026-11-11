"""Phase 8A: the backtester end to end against PostgreSQL, on DETERMINISTIC FIXTURE bars.

Covers chronological replay, strategy integration through the shared decision step, risk
sizing and limits, execution (limit entries, stops, targets, gaps, partial fills, costs),
accounting reconciliation, look-ahead and survivorship safeguards, corporate actions,
reproducibility and the immutability of stored results. The strategies are the 4B TEST
FIXTURE strategies (not trading strategies) unless a test says otherwise.
"""

from __future__ import annotations

import itertools
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text, update
from sqlalchemy.exc import DBAPIError

from app.asset_discovery.enums import MembershipAction, UniverseLayer
from app.backtesting.data import PointInTimeBars, sessions_between
from app.backtesting.enums import RunStatus
from app.database.models import (
    Asset,
    AssetStatus,
    BacktestBenchmarkResultRecord,
    BacktestEquitySnapshotRecord,
    BacktestEvaluationCountRecord,
    BacktestEventCursorRecord,
    BacktestFillRecord,
    BacktestJournalEntryRecord,
    BacktestMetricRecord,
    BacktestOrderRecord,
    BacktestProposalRecord,
    BacktestRejectionRecord,
    BacktestRunErrorRecord,
    BacktestRunEventRecord,
    BacktestRunRecord,
    BacktestTradeRecord,
)
from app.database.sessions import session_scope
from app.strategies.registry import default_registry
from app.tests.integration.backtest_support import (
    RUN_END,
    RUN_START,
    SETTLE,
    Candle,
    D,
    add_membership,
    make_bench,
    make_traded_asset,
    rows,
    run_record,
    seed_series,
    trend,
)
from app.tests.integration.strategy_support import AlwaysShortStrategy, ProposingStrategy

pytestmark = pytest.mark.integration

WIDE = {
    "hard_limits": {
        "max_sector_pct": "1.00",
        "max_open_positions": 30,
        "max_single_asset_pct": "1.00",
        "max_correlated_exposure_pct": "1.00",
    }
}


def completed(bench: Any, run_id: uuid.UUID) -> Any:
    run = run_record(bench, run_id)
    errors = rows(bench, BacktestRunErrorRecord, run_id)
    assert run.status is RunStatus.COMPLETED, (run.error_message, [e.message for e in errors])
    return run


def shock(day_of_october: int, kind: str) -> Any:
    """MOCK path: the uptrend of ``trend``, with one session changed on the given date."""

    def candles(i: int, day: Any) -> Candle:
        c = trend(i)
        if day.day != date(2026, 10, day_of_october):
            return c
        prev = trend(i - 1).close
        if kind == "gap_down":
            o = (prev * D("0.88")).quantize(D("0.01"))
            return Candle(o, o + D("0.5"), o - D(1), o)
        if kind == "spike":
            return Candle(prev, prev * D("1.08"), prev - D("0.2"), prev * D("1.07"))
        if kind == "both":
            return Candle(prev, prev * D("1.08"), prev * D("0.90"), prev)
        raise AssertionError(kind)

    return candles


# ---------------------------------------------------------------------- replay
def test_events_replay_chronologically_and_nothing_uses_a_later_observation(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    run_id = bench.run(bench.request())
    run = completed(bench, run_id)
    assert run.summary["reconciliation"]["passed"] is True
    sessions = sessions_between(RUN_START, RUN_END)
    cursors = rows(bench, BacktestEventCursorRecord, run_id, BacktestEventCursorRecord.seq)
    assert [c.session for c in cursors] == [s.day for s in sessions]
    assert all(a.simulated_at < b.simulated_at for a, b in itertools.pairwise(cursors))
    assert run.sessions_done == run.sessions_total == len(sessions)
    snaps = rows(bench, BacktestEquitySnapshotRecord, run_id, BacktestEquitySnapshotRecord.session)
    assert [s.session for s in snaps] == [s.day for s in sessions]
    proposals = rows(bench, BacktestProposalRecord, run_id, BacktestProposalRecord.seq)
    by_day = {s.day: s for s in sessions}
    for p in proposals:
        # A decision is taken at its session's close + settle, never earlier, and every
        # piece of evidence was available by then.
        assert p.decided_at == by_day[p.session].regular_close + SETTLE
        for ev in p.details["evidence"]:
            assert datetime.fromisoformat(ev["available_at"]) <= p.decided_at
    fills = rows(bench, BacktestFillRecord, run_id, BacktestFillRecord.seq)
    orders = {o.seq: o for o in rows(bench, BacktestOrderRecord, run_id)}
    proposal_by_seq = {p.seq: p for p in proposals}
    assert fills
    for f in fills:
        order = orders[f.order_seq]
        assert f.filled_at <= f.known_at
        assert f.filled_at > order.arrival_at  # filled in a session after the order arrived
        if order.role == "ENTRY":
            # Never in the bar that produced the signal.
            assert f.session > proposal_by_seq[order.proposal_seq].session


def test_future_observations_cannot_change_earlier_decisions(fresh_engine: Engine) -> None:
    """Two databases identical up to 14 October; in one, every later price collapses. The
    replays must agree on everything decided or filled by 14 October's cutoff."""
    from sqlalchemy import create_engine

    results = []
    fresh_engine.dispose()
    for collapse in (False, True):
        url = fresh_engine.url.set(database=f"{fresh_engine.url.database}_{int(collapse)}")
        admin = create_engine(
            fresh_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        with admin.connect() as conn:
            conn.execute(
                text(f'CREATE DATABASE "{url.database}" TEMPLATE "{fresh_engine.url.database}"')
            )
        engine = create_engine(url, connect_args={"options": "-c timezone=UTC"})
        try:
            bench = make_bench(engine)
            asset = make_traded_asset(bench.factory, "FIXA")

            def candles(i: int, day: Any, collapse: bool = collapse) -> Candle:
                c = trend(i)
                if collapse and day.day > date(2026, 10, 14):
                    return Candle(c.open / 3, c.high / 3, c.low / 3, c.close / 3, D(5))
                return c

            seed_series(bench.factory, asset, candles)
            run_id = bench.run(bench.request())
            completed(bench, run_id)
            cutoff = datetime(2026, 10, 14, 21, tzinfo=UTC)  # 14 Oct close (EDT) + 1 h
            proposals = [
                (p.session, p.direction, p.reference_entry, p.risk_result, p.permitted_quantity)
                for p in rows(bench, BacktestProposalRecord, run_id, BacktestProposalRecord.seq)
                if p.decided_at <= cutoff
            ]
            fills = [
                (f.session, f.side, f.quantity, f.price)
                for f in rows(bench, BacktestFillRecord, run_id, BacktestFillRecord.seq)
                if f.known_at <= cutoff
            ]
            equity = [
                (s.session, s.equity)
                for s in rows(bench, BacktestEquitySnapshotRecord, run_id)
                if s.at <= cutoff
            ]
            results.append((proposals, fills, equity))
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            admin.dispose()
    assert results[0][0]
    assert results[0][1]
    assert results[0] == results[1]


def test_every_bar_served_to_a_feature_was_completed_by_the_decision(
    fresh_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    served: list[tuple[datetime | None, datetime]] = []
    original = PointInTimeBars.__call__

    def spy(self: PointInTimeBars, session: Any, asset_id: Any, sessions: Any, **kw: Any) -> Any:
        out = original(self, session, asset_id, sessions, **kw)
        served.extend((b.available_at, kw["data_cutoff"]) for b in out)
        return out

    monkeypatch.setattr(PointInTimeBars, "__call__", spy)
    completed(bench, bench.run(bench.request()))
    assert served
    assert all(a is not None and a <= cutoff for a, cutoff in served)


# ---------------------------------------------------------------------- universe
def test_point_in_time_universe_membership_is_respected_without_survivorship(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine)
    early = make_traded_asset(bench.factory, "FIXE")  # a member until 14 October
    late = make_traded_asset(bench.factory, "FIXL")  # a member from 15 October
    gone = make_traded_asset(bench.factory, "FIXG")  # a member throughout; delisted since
    for a in (early, late, gone):
        seed_series(bench.factory, a, lambda i, _d: trend(i))
    with session_scope(bench.factory) as session:
        session.execute(update(Asset).where(Asset.id == gone).values(status=AssetStatus.INACTIVE))
    layer = UniverseLayer.QUALIFIED_UNIVERSE
    add_membership(
        bench.factory,
        early,
        layer,
        [
            (MembershipAction.ADDED, datetime(2026, 9, 1, tzinfo=UTC)),
            (MembershipAction.REMOVED, datetime(2026, 10, 15, tzinfo=UTC)),
        ],
    )
    add_membership(
        bench.factory,
        late,
        layer,
        [
            (MembershipAction.ADDED, datetime(2026, 10, 15, tzinfo=UTC)),
        ],
    )
    add_membership(
        bench.factory,
        gone,
        layer,
        [
            (MembershipAction.ADDED, datetime(2026, 9, 1, tzinfo=UTC)),
        ],
    )
    request = bench.request(
        universe={"mode": "POINT_IN_TIME", "layer": "QUALIFIED_UNIVERSE"}, risk=WIDE
    )
    run_id = bench.run(request)
    completed(bench, run_id)
    proposals = rows(bench, BacktestProposalRecord, run_id)
    sessions = {
        s: sorted({p.session for p in proposals if p.symbol == s}) for s in ("FIXE", "FIXL", "FIXG")
    }
    boundary = date(2026, 10, 15)
    assert sessions["FIXE"]
    assert max(sessions["FIXE"]) < boundary
    assert sessions["FIXL"]
    assert min(sessions["FIXL"]) >= boundary
    # An asset delisted after the run is not dropped from the periods it belonged to.
    assert sessions["FIXG"][0] == RUN_START
    assert sessions["FIXG"][-1] == date(2026, 10, 28)
    assert any(t.symbol == "FIXG" for t in rows(bench, BacktestTradeRecord, run_id))
    kinds = Counter(e.kind for e in rows(bench, BacktestRunEventRecord, run_id))
    assert kinds["UNIVERSE_ADDED"] >= 3
    assert kinds["UNIVERSE_REMOVED"] == 1


# ---------------------------------------------------------------------- risk
def test_risk_rules_size_positions_and_enforce_limits(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    for symbol in ("FIXA", "FIXB", "FIXC"):
        asset = make_traded_asset(bench.factory, symbol)
        seed_series(bench.factory, asset, lambda i, _d: trend(i))
    request = bench.request(
        universe={"mode": "EXPLICIT", "symbols": ["FIXA", "FIXB", "FIXC"]},
        risk={"hard_limits": {"max_sector_pct": "1.00", "max_open_positions": 2}},
    )
    run_id = bench.run(request)
    run = completed(bench, run_id)
    assert run.config["risk"]["hard_limits"]["max_open_positions"] == 2
    assert run.risk_source.endswith("+BACKTEST_OVERRIDES")
    snaps = rows(bench, BacktestEquitySnapshotRecord, run_id)
    assert max(s.open_positions for s in snaps) <= 2
    rejections = Counter(r.code for r in rows(bench, BacktestRejectionRecord, run_id))
    assert rejections["MAX_OPEN_POSITIONS"] > 0
    proposals = rows(bench, BacktestProposalRecord, run_id)
    approved = [p for p in proposals if p.risk_result == "PASS_FOR_RESERVATION"]
    assert approved
    for p in approved:
        # Sized by the 6B rules: the potential loss at the stop never exceeds the budget.
        assert Decimal(p.details["potential_loss"]) <= Decimal(p.details["risk_budget"])
        # 0.5% of the equity at the decision (equity stayed within 1% of 100,000 here).
        assert Decimal(p.details["risk_budget"]) <= D("505.00")
        assert p.permitted_quantity == Decimal(p.details["reservation"]["quantity"])


# ---------------------------------------------------------------------- execution
def test_stops_targets_gaps_and_stop_first(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    paths = {"FIXS": "gap_down", "FIXT": "spike", "FIXB": "both"}
    for symbol, kind in paths.items():
        asset = make_traded_asset(bench.factory, symbol)
        seed_series(bench.factory, asset, shock(13, kind))
    run_id = bench.run(
        bench.request(universe={"mode": "EXPLICIT", "symbols": list(paths)}, risk=WIDE)
    )
    completed(bench, run_id)
    trades = rows(bench, BacktestTradeRecord, run_id)
    fills = {f.order_seq: f for f in rows(bench, BacktestFillRecord, run_id)}
    shock_day = date(2026, 10, 13)

    def exits(symbol: str) -> list[tuple[str | None, Any]]:
        out = []
        for t in trades:
            if t.symbol != symbol or t.closed_at is None:
                continue
            out.append((t.exit_reason, t.details["last_session"]))
        return out

    gap_stops = [t for t in trades if t.symbol == "FIXS" and t.exit_reason == "STOP_LOSS"]
    assert gap_stops
    for t in gap_stops:
        stop_fill = fills[t.details["stop_order_seq"]]
        assert stop_fill.session.isoformat() == "2026-10-13"
        assert stop_fill.details["gap"] is True
        assert stop_fill.price <= Decimal(t.details["stop_level"])  # never better than the stop
    assert ("TAKE_PROFIT", shock_day.isoformat()) in exits("FIXT")
    both = exits("FIXB")
    assert ("STOP_LOSS", shock_day.isoformat()) in both
    assert ("TAKE_PROFIT", shock_day.isoformat()) not in both  # stop first


def test_partial_fills_accumulate_and_release_the_unfilled_reservation(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXP")

    def thin(i: int, day: Any) -> Candle:
        c = trend(i)
        if day.day >= date(2026, 10, 2):
            return Candle(c.open, c.high, c.low, c.close, D(600))  # 5% = 30 shares a session
        return c

    seed_series(bench.factory, asset, thin)
    run_id = bench.run(bench.request(universe={"mode": "EXPLICIT", "symbols": ["FIXP"]}, risk=WIDE))
    completed(bench, run_id)
    fills = rows(bench, BacktestFillRecord, run_id)
    orders = rows(bench, BacktestOrderRecord, run_id)
    entries = [o for o in orders if o.role == "ENTRY" and o.filled_quantity > 0]
    partial = [o for o in entries if o.filled_quantity < o.quantity]
    assert partial, "thin sessions must leave entries partly filled"
    for o in entries:
        mine = [f for f in fills if f.order_seq == o.seq]
        assert sum((f.quantity for f in mine), D(0)) == o.filled_quantity
        assert all(f.quantity <= D(30) for f in mine if f.session >= date(2026, 10, 2))
    proposals = {p.seq: p for p in rows(bench, BacktestProposalRecord, run_id)}
    assert any(proposals[o.proposal_seq].outcome == "PARTIALLY_FILLED" for o in partial)


def test_costs_are_charged_and_reconcile_with_the_metrics(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    run_id = bench.run(bench.request())
    completed(bench, run_id)
    fills = rows(bench, BacktestFillRecord, run_id)
    metrics = {(m.scope, m.metric): m for m in rows(bench, BacktestMetricRecord, run_id)}
    commissions = sum((f.commission for f in fills), D(0))
    fees = sum((f.fees for f in fills), D(0))
    assert commissions > 0
    assert fees > 0
    assert metrics[("PORTFOLIO", "commissions")].value == commissions
    assert metrics[("PORTFOLIO", "fees")].value == fees
    final = rows(bench, BacktestEquitySnapshotRecord, run_id, BacktestEquitySnapshotRecord.session)
    last = final[-1]
    assert last.gross_equity == last.equity + last.total_costs
    net = metrics[("PORTFOLIO", "net_pnl")].value
    gross = metrics[("PORTFOLIO", "gross_pnl")].value
    assert gross - net == metrics[("PORTFOLIO", "total_costs")].value
    trades = rows(bench, BacktestTradeRecord, run_id)
    # Trade-level net P/L adds up to the account's P/L (no dividends or other income).
    assert abs(sum((t.net_pnl for t in trades), D(0)) - net) <= D("0.05")


def test_shorts_pay_borrow_and_post_short_entries(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine, (AlwaysShortStrategy(),))
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i, step=-0.1))
    shorts = {"strategies": [{"strategy_id": "fixture_short"}], "directions": ["SHORT"]}
    # The fixture provider reports no short margin requirement: 6B rejects by default.
    strict = bench.run(bench.request(**shorts, risk=WIDE))
    completed(bench, strict)
    codes = Counter(r.code for r in rows(bench, BacktestRejectionRecord, strict))
    assert codes["MARGIN_DATA_MISSING"] > 0
    assert not rows(bench, BacktestTradeRecord, strict)
    # With the configured conservative minimum (an explicit, recorded override) shorts trade.
    relaxed = {
        "hard_limits": {**WIDE["hard_limits"], "missing_margin_policy": "USE_CONFIGURED_MINIMUM"}
    }
    run_id = bench.run(bench.request(**shorts, risk=relaxed))
    completed(bench, run_id)
    trades = rows(bench, BacktestTradeRecord, run_id)
    assert trades
    assert all(t.direction == "SHORT" for t in trades)
    journal = rows(bench, BacktestJournalEntryRecord, run_id)
    templates = Counter(j.template for j in journal)
    assert templates["short_open"] > 0
    assert templates["financing_cost"] > 0
    assert sum((Decimal(t.details["borrow_costs"]) for t in trades), D(0)) > 0
    metrics = {
        (m.scope, m.scope_key, m.metric): m for m in rows(bench, BacktestMetricRecord, run_id)
    }
    assert ("DIRECTION", "SHORT", "trades_closed") in metrics


def test_a_split_while_holding_is_carried_without_a_fictitious_loss(fresh_engine: Engine) -> None:
    """FIXTURE 2-for-1 split effective 14 October: RAW halves, the provider's SPLIT series
    is continuous. Positions are carried at the last pre-split close."""
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXX")
    split_day = date(2026, 10, 14)

    def raw(i: int, day: Any) -> Candle:
        c = trend(i)
        if day.day < split_day:
            return Candle(c.open * 2, c.high * 2, c.low * 2, c.close * 2, D(500_000))
        return c

    seed_series(
        bench.factory,
        asset,
        raw,
        split_factor=lambda day: D(2) if day.day < split_day else D(1),
    )
    run_id = bench.run(bench.request(universe={"mode": "EXPLICIT", "symbols": ["FIXX"]}, risk=WIDE))
    completed(bench, run_id)
    events = [
        e for e in rows(bench, BacktestRunEventRecord, run_id) if e.kind == "CORPORATE_ACTION"
    ]
    assert len(events) == 1
    assert events[0].details["ratio"] == "2"
    assert events[0].details["open_trades"] > 0
    carried = [
        t for t in rows(bench, BacktestTradeRecord, run_id) if t.details["split_factor"] == "2"
    ]
    assert carried
    snaps = {s.session: s for s in rows(bench, BacktestEquitySnapshotRecord, run_id)}
    before, after = snaps[date(2026, 10, 13)].equity, snaps[split_day].equity
    assert abs(after - before) / before < D("0.01"), (before, after)
    # Proposals before the split are at the pre-split level, after it at the new level.
    proposals = rows(bench, BacktestProposalRecord, run_id)
    pre = [p.reference_entry for p in proposals if p.session < split_day]
    post = [p.reference_entry for p in proposals if p.session >= split_day]
    assert min(pre) > D(200)
    assert max(post) < D(150)


# ---------------------------------------------------------------------- strategies
def test_several_strategies_and_timeframes_run_together(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine, (ProposingStrategy(), AlwaysShortStrategy()))
    for symbol in ("FIXA", "FIXB"):
        asset = make_traded_asset(bench.factory, symbol)
        seed_series(bench.factory, asset, lambda i, _d: trend(i))
    run_id = bench.run(
        bench.request(
            universe={"mode": "EXPLICIT", "symbols": ["FIXA", "FIXB"]},
            strategies=[{"strategy_id": "fixture_proposer"}, {"strategy_id": "fixture_short"}],
            directions=["LONG", "SHORT"],
            timeframes=["1Day", "1Week"],
            risk=WIDE,
        )
    )
    completed(bench, run_id)
    counts = rows(bench, BacktestEvaluationCountRecord, run_id)
    assert {(c.strategy_id, c.timeframe) for c in counts} == {
        ("fixture_proposer", "1Day"),
        ("fixture_proposer", "1Week"),
        ("fixture_short", "1Day"),
        ("fixture_short", "1Week"),
    }
    proposals = rows(bench, BacktestProposalRecord, run_id)
    assert {p.timeframe for p in proposals} == {"1Day", "1Week"}
    assert {p.symbol for p in proposals} == {"FIXA", "FIXB"}
    weekly = [p for p in proposals if p.timeframe == "1Week"]
    # Weekly decisions only at the last session of an ISO week.
    assert all(p.session.weekday() == 4 for p in weekly)
    codes = Counter(r.code for r in rows(bench, BacktestRejectionRecord, run_id))
    assert codes["OPPOSITE_POSITION_HELD"] + codes["OPPOSITE_RESERVATION_ACTIVE"] > 0
    metrics = {(m.scope, m.scope_key) for m in rows(bench, BacktestMetricRecord, run_id)}
    assert ("STRATEGY", "fixture_proposer") in metrics
    assert ("STRATEGY", "fixture_short") in metrics


def test_all_six_strategies_are_evaluated_through_their_own_interfaces(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine, tuple(default_registry()))
    for symbol in ("FIXA", "SPY"):
        asset = make_traded_asset(bench.factory, symbol)
        seed_series(bench.factory, asset, lambda i, _d: trend(i))
    six = [
        "momentum",
        "trend_following",
        "mean_reversion",
        "breakout",
        "volatility",
        "event_driven",
    ]
    run_id = bench.run(
        bench.request(
            strategies=[{"strategy_id": s} for s in six],
            directions=["LONG", "SHORT"],
            timeframes=None,
            benchmark_symbol="SPY",
        )
    )
    run = completed(bench, run_id)
    assert {s["strategy_id"] for s in run.config["strategies"]} == set(six)
    counts = rows(bench, BacktestEvaluationCountRecord, run_id)
    assert {c.strategy_id for c in counts} == set(six)
    assert not [c for c in counts if c.state == "FAILED"]
    assert sum(c.count for c in counts if c.state not in ("INELIGIBLE_ASSET",)) > 0
    assert not rows(bench, BacktestRunErrorRecord, run_id)


# ---------------------------------------------------------------------- benchmark
def test_benchmark_and_buy_and_hold_cover_the_same_sessions(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    spy = make_traded_asset(bench.factory, "SPY")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    seed_series(bench.factory, spy, lambda i, _d: trend(i, start=400, step=1))
    run_id = bench.run(bench.request(benchmark_symbol="SPY"))
    completed(bench, run_id)
    results = {b.kind: b for b in rows(bench, BacktestBenchmarkResultRecord, run_id)}
    sessions = sessions_between(RUN_START, RUN_END)
    first = sessions_between(date(2026, 5, 1), RUN_START)
    i0 = len(first) - 1  # index of RUN_START in the fixture series
    i1 = i0 + len(sessions) - 1
    entry = trend(i0, start=400, step=1).open
    expected = (trend(i1, start=400, step=1).close / entry - 1).quantize(D("0.00000001"))
    spy_result = results["BENCHMARK"]
    assert spy_result.available
    assert spy_result.total_return == expected
    series = spy_result.details["series"]
    assert [s[0] for s in series] == [s.day.isoformat() for s in sessions]
    final = rows(bench, BacktestEquitySnapshotRecord, run_id, BacktestEquitySnapshotRecord.session)[
        -1
    ]
    portfolio_return = final.equity / D(100000) - 1
    assert spy_result.excess_return == (portfolio_return - expected).quantize(D("0.00000001"))
    assert results["BUY_AND_HOLD"].available


# ---------------------------------------------------------------------- reproducibility
def test_a_completed_run_is_reproducible_from_its_stored_configuration(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    original = bench.run(bench.request())
    first = completed(bench, original)
    # Data ingested later (a new revision after the knowledge time) cannot change a replay.
    seed_series(
        bench.factory,
        asset,
        lambda i, _d: Candle(D(1), D(2), D("0.5"), D(1)),
        ingested_at=datetime.now(UTC),
        revision=1,
    )
    copy = bench.service.reproduce(original, actor="operator:test")
    second = completed(bench, copy)
    assert second.reproduction_of == original
    assert second.reproduction_matches is True
    assert second.result_sha256 == first.result_sha256
    assert second.dataset_sha256 == first.dataset_sha256


def test_stored_results_are_immutable(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    run_id = bench.run(bench.request())
    completed(bench, run_id)
    statements: list[Any] = [
        update(BacktestRunRecord).where(BacktestRunRecord.id == run_id).values(name="changed"),
        update(BacktestTradeRecord)
        .where(BacktestTradeRecord.run_id == run_id)
        .values(net_pnl=D(1)),
        text("DELETE FROM backtest_fills WHERE run_id = :r"),
        text(
            "INSERT INTO backtest_run_events (run_id, seq, at, kind, message, details) "
            "VALUES (:r, 99999, now(), 'X', 'late', '{}')"
        ),
        text("DELETE FROM backtest_runs WHERE id = :r"),
    ]
    for statement in statements:
        with pytest.raises(DBAPIError), session_scope(bench.factory) as session:
            session.execute(statement, {"r": run_id})
