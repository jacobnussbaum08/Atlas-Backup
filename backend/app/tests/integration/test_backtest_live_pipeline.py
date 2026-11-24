"""D-234: the optional live-pipeline stages of the backtest (PostgreSQL).

Bars, quotes-derived spread rows and strategies are TEST FIXTURES (see
``backtest_support``), not market data."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine

from app.asset_discovery.enums import MetricStatus
from app.backtesting.data import sessions_between
from app.backtesting.enums import RunStatus
from app.backtesting.spreads import (
    CLOSE_WINDOW,
    METRIC,
    OPEN_WINDOW,
    SampleWindow,
    window_for,
)
from app.database.models import (
    AssetMetricValueRecord,
    BacktestFillRecord,
    BacktestProposalRecord,
    BacktestRejectionRecord,
    BacktestRunErrorRecord,
    BacktestRunEventRecord,
)
from app.database.sessions import session_scope
from app.tests.integration.backtest_support import (
    RUN_END,
    RUN_START,
    Candle,
    D,
    make_bench,
    make_traded_asset,
    rows,
    run_record,
    seed_series,
    trend,
)

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


def seed_spreads(
    factory: Any,
    asset_id: uuid.UUID,
    feed: str,
    bps: Decimal,
    kind: SampleWindow = CLOSE_WINDOW,
) -> None:
    """FIXTURE spread measurements, one per session window, around a 100.00 mid."""
    half = bps / 2 / 10000 * 100
    with session_scope(factory) as session:
        for day in sessions_between(RUN_START, RUN_END):
            window = window_for(day.day, kind)
            assert window is not None
            start, end = window
            session.add(
                AssetMetricValueRecord(
                    asset_id=asset_id,
                    metric=METRIC,
                    window=kind.label,
                    timeframe="quote",
                    provider="alpaca",
                    feed=feed,
                    feature_version=kind.feature_version,
                    as_of=end,
                    source_start=start,
                    source_end=end,
                    observations=10,
                    expected_observations=0,
                    value=bps,
                    status=MetricStatus.OK.value,
                    details={
                        "bid": str(D(100) - half),
                        "ask": str(D(100) + half),
                        "quote_at": start.isoformat(),
                    },
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )


def pipeline_summary(bench: Any, run_id: uuid.UUID) -> dict[str, Any]:
    events = [
        e for e in rows(bench, BacktestRunEventRecord, run_id) if e.kind == "PIPELINE_SUMMARY"
    ]
    assert len(events) == 1
    return dict(events[0].details)


def test_without_a_pipeline_the_stored_configuration_is_unchanged(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    run_id = bench.run(bench.request(risk=WIDE, execution_conventions="daily-v1", dividends=False))
    run = completed(bench, run_id)
    # Earlier configurations keep their hashes: no "pipeline" key is written.
    assert "pipeline" not in run.config
    # Engine 8A.2 (D-237) records only its conventions; no pipeline stage ran.
    summaries = [
        e for e in rows(bench, BacktestRunEventRecord, run_id) if e.kind == "PIPELINE_SUMMARY"
    ]
    assert [dict(e.details) for e in summaries] == [{"execution_conventions": "daily-v1"}]


def test_measured_spreads_reach_the_risk_check_and_the_fills(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    wide = make_traded_asset(bench.factory, "FIXA")
    tight = make_traded_asset(bench.factory, "FIXB")
    for asset in (wide, tight):
        seed_series(bench.factory, asset, lambda i, _d: trend(i))
    seed_spreads(bench.factory, wide, "iex", D(80))  # wider than the 50 bps limit
    seed_spreads(bench.factory, tight, "iex", D(2))
    seed_spreads(bench.factory, wide, "sip", D(1))  # another feed: never read here
    request = bench.request(
        universe={"mode": "EXPLICIT", "symbols": ["FIXA", "FIXB"]},
        risk=WIDE,
        pipeline={"measured_spreads": {"feed": "iex"}},
    )
    run_id = bench.run(request)
    run = completed(bench, run_id)
    assert run.config["pipeline"]["measured_spreads"]["feed"] == "iex"
    # D-235 / D-236: configurations without the E1 or E7 options keep their hashes.
    assert "opening_entry" not in run.config["pipeline"]
    assert "execution_fidelity" not in run.config["pipeline"]
    proposals = rows(bench, BacktestProposalRecord, run_id)
    by_symbol: dict[str, Counter[str]] = {"FIXA": Counter(), "FIXB": Counter()}
    for p in proposals:
        by_symbol[p.symbol][p.risk_result] += 1
    assert by_symbol["FIXA"]
    assert set(by_symbol["FIXA"]) == {"REJECTED"}
    assert by_symbol["FIXB"]["PASS_FOR_RESERVATION"] > 0
    codes = Counter(
        r.code for r in rows(bench, BacktestRejectionRecord, run_id) if r.symbol == "FIXA"
    )
    assert codes["SPREAD_TOO_WIDE"] == by_symbol["FIXA"]["REJECTED"]
    summary = pipeline_summary(bench, run_id)["spreads"]
    assert summary["feed"] == "iex"
    assert summary["lookups"]["measured"] > 0
    assert summary["fills"].get("measured", 0) > 0


def storm(i: int, day: Any) -> Candle:
    """MOCK benchmark: calm small oscillation, then +-6 % daily swings from 2026-09-15."""
    base = D(str(round(400 + 0.1 * i, 4)))
    if day.day < date(2026, 9, 15):
        close = base + (D("0.4") if i % 2 else D("-0.4"))
    else:
        close = base * (D("1.06") if i % 2 else D("0.94"))
    return Candle(close, close * D("1.01"), close * D("0.99"), close)


def test_the_coordinator_stands_between_strategies_and_risk(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i), first=date(2025, 6, 2))
    spy = make_traded_asset(bench.factory, "SPY")
    seed_series(bench.factory, spy, storm, first=date(2025, 6, 2))
    plain = bench.run(bench.request(risk=WIDE, benchmark_symbol="SPY"))
    completed(bench, plain)
    assert rows(bench, BacktestProposalRecord, plain)  # without it, risk sees proposals

    run_id = bench.run(
        bench.request(
            risk=WIDE,
            benchmark_symbol="SPY",
            pipeline={"coordinator": True, "live_entry_expiry": True},
        )
    )
    completed(bench, run_id)
    summary = pipeline_summary(bench, run_id)["coordinator"]
    assert summary["coordinator_runs"] > 0
    assert "ML predictions: no approved model in the replay" in summary["omitted_inputs"]
    # The market volatility regime is computed point in time from the benchmark's bars.
    assert summary["market_volatility_on_decision_days"].get("EXTREME", 0) > 0
    declined = [r for r in rows(bench, BacktestRejectionRecord, run_id) if r.stage == "COORDINATOR"]
    assert declined
    # The fixture's reward equals its risk: after assumed costs the coordinator rejects it,
    # so nothing reaches the risk engine.
    assert {r.code for r in declined} <= {
        "POOR_REWARD_RISK",
        "UNFAVORABLE_MARKET_CONDITIONS",
        "INSUFFICIENT_EVIDENCE",
        "HIGH_UNCERTAINTY",
        "UNFAVORABLE_REGIME",
    }
    assert any("market volatility EXTREME" in r.message for r in declined)
    assert rows(bench, BacktestProposalRecord, run_id) == []


def test_opening_entries_fill_at_the_open_and_risk_sizes_with_the_worst_price(
    fresh_engine: Engine,
) -> None:
    """E1 (D-235): with ``opening_entry`` the proposal reaches risk with the worst entry
    price, and an entry whose next session opens within it fills at that open plus the
    measured opening half spread."""
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXB")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    seed_spreads(bench.factory, asset, "iex", D(2))
    seed_spreads(bench.factory, asset, "iex", D(6), OPEN_WINDOW)
    request = bench.request(
        universe={"mode": "EXPLICIT", "symbols": ["FIXB"]},
        risk=WIDE,
        pipeline={
            "measured_spreads": {"feed": "iex"},
            "opening_entry": {"tolerance_pct": "0.015"},
        },
    )
    run_id = bench.run(request)
    run = completed(bench, run_id)
    assert run.config["pipeline"]["opening_entry"]["tolerance_pct"] == "0.015"
    approved = [
        p
        for p in rows(bench, BacktestProposalRecord, run_id)
        if p.risk_result == "PASS_FOR_RESERVATION"
    ]
    assert approved
    for p in approved:
        sign = 1 if p.direction == "LONG" else -1
        worst = D(p.details["levels"]["e1_worst_entry"])
        assert worst == (p.reference_entry * (1 + sign * D("0.015"))).quantize(D("0.0000000001"))
    entries = [f for f in rows(bench, BacktestFillRecord, run_id) if f.role == "ENTRY"]
    assert entries
    opened = [
        f for f in entries if any(a.startswith("opening entry") for a in f.details["assumptions"])
    ]
    assert opened
    assert all("measured opening" in " ".join(f.details["assumptions"]) for f in opened)
    summary = pipeline_summary(bench, run_id)["opening_entry"]
    assert summary["first_session"].get("open_fill_measured", 0) == len(opened)
    assert summary["open_spread_lookups"]["measured"] >= len(opened)


def gappy(i: int, _day: Any) -> Candle:
    """MOCK candles: an uptrend in which every third session opens 1.5 below the trend and
    never trades back up to the prior close (a limit at that close is never traded)."""
    base = D(str(round(100 + 0.2 * i, 4)))
    if i % 3 == 0:
        return Candle(base - D("1.50"), base - D("1.20"), base - D("1.80"), base - D("1.30"))
    return Candle(base - D("0.10"), base + D("0.60"), base - D("0.60"), base)


def test_corrected_execution_keeps_every_fill_inside_its_session(fresh_engine: Engine) -> None:
    """E7 (D-236): the baseline fills a buy limit at its limit although the session opened
    below it and never traded there; ``execution_fidelity`` fills it at the open, and every
    corrected fill lies inside its session's range. The baseline is unchanged."""
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXB")
    days = seed_series(bench.factory, asset, gappy)
    candles = {d.day: gappy(i, d) for i, d in enumerate(days)}
    seed_spreads(bench.factory, asset, "iex", D(2))
    seed_spreads(bench.factory, asset, "iex", D(6), OPEN_WINDOW)
    base = {"measured_spreads": {"feed": "iex"}}
    baseline = bench.run(  # the daily-v1 conventions (D-237: no longer the default)
        bench.request(
            universe={"mode": "EXPLICIT", "symbols": ["FIXB"]},
            risk=WIDE,
            pipeline=base,
            execution_conventions="daily-v1",
        )
    )
    corrected = bench.run(
        bench.request(
            universe={"mode": "EXPLICIT", "symbols": ["FIXB"]},
            risk=WIDE,
            pipeline={**base, "execution_fidelity": {}},
        )
    )
    assert "execution_fidelity" not in completed(bench, baseline).config["pipeline"]
    assert completed(bench, corrected).config["pipeline"]["execution_fidelity"] == {
        "limit_marketable_at_open": True,
        "stop_fill_at_trigger": True,
        "market_orders_at_open": True,
        "open_spreads": True,
    }

    def outside(run_id: uuid.UUID) -> list[Any]:
        return [
            f
            for f in rows(bench, BacktestFillRecord, run_id)
            if not candles[f.session].low <= f.price <= candles[f.session].high
        ]

    assert [f.role for f in outside(baseline)].count("ENTRY") > 0  # the defect is exercised
    assert outside(corrected) == []
    # Engine 8A.2's default (daily-v2) applies the same corrections without the option.
    default = bench.run(
        bench.request(universe={"mode": "EXPLICIT", "symbols": ["FIXB"]}, risk=WIDE, pipeline=base)
    )
    assert completed(bench, default).config["execution_conventions"] == "daily-v2"
    assert outside(default) == []
    assert [
        (f.role, f.price) for f in rows(bench, BacktestFillRecord, default, BacktestFillRecord.seq)
    ] == [
        (f.role, f.price)
        for f in rows(bench, BacktestFillRecord, corrected, BacktestFillRecord.seq)
    ]
    summary = pipeline_summary(bench, corrected)["execution_fidelity"]
    assert summary["corrected_fills"].get("E7 limit marketable at the open", 0) > 0
    assert summary["corrected_fills"].get("open_spread_measured", 0) > 0
