"""Phase 8A: performance metrics (known vectors), the simulated ledger and configuration
validation. All inputs are hand-written TEST FIXTURES."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.accounting.templates import InstrumentInfo, TradeParams
from app.backtesting import metrics as m
from app.backtesting.config import BacktestRequest
from app.backtesting.engine import Snapshot, Trade
from app.backtesting.enums import MetricScope, MetricStatus, TradeStatus
from app.backtesting.ledger import BacktestTemplateContext, LedgerError, SimulatedLedger
from app.risk.enums import TradeDirection

D = Decimal
ASSET = uuid.uuid4()
T0 = datetime(2026, 10, 1, 21, tzinfo=UTC)


def snap(equity: str, costs: str = "0", day: int = 0) -> Snapshot:
    return Snapshot(
        session=date(2026, 1, 1) + timedelta(days=day),
        at=T0 + timedelta(days=day),
        equity=D(equity),
        cash=D(equity),
        contributed_capital=D(100),
        long_exposure=D(0),
        short_exposure=D(0),
        realized_pnl=D(0),
        unrealized_pnl=D(0),
        commissions=D(costs),
        fees=D(0),
        borrow_costs=D(0),
        open_positions=0,
        open_orders=0,
        reserved_exposure=D(0),
        peak_equity=D(100),
        drawdown=D(0),
        mark_status="COMPLETE",
        active_breakers=[],
    )


def trade(net: str, status: TradeStatus = TradeStatus.CLOSED, seq: int = 1) -> Trade:
    t = Trade(
        seq=seq,
        proposal_seq=seq,
        strategy_id="s",
        strategy_version=1,
        timeframe="1Day",
        asset_id=ASSET,
        symbol="FIX",
        asset_class="EQUITY",
        direction=TradeDirection.LONG,
        entry_order_seq=1,
        opened_at=T0,
        first_session=date(2026, 10, 1),
        stop_level=D(1),
        target_level=None,
        horizon_periods=5,
    )
    t.realized_gross = D(net)
    t.status = status
    t.last_session = date(2026, 10, 6)
    return t


def value(metric: m.Metric) -> Decimal:
    assert metric.value is not None
    return metric.value


def test_max_drawdown_from_a_running_peak() -> None:
    amount, pct = m.max_drawdown([D(110), D(99), D(120), D(90), D(130)], D(100))
    assert amount == D(30)
    assert pct == D(30) / D(120)


def test_daily_returns_start_from_the_initial_capital_and_gross_adds_costs_back() -> None:
    snaps = [snap("101", "1", 0), snap("99", "2", 1)]
    assert m.daily_returns(snaps, D(100)) == pytest.approx([0.01, 99 / 101 - 1])
    assert m.daily_returns(snaps, D(100), gross=True) == pytest.approx([0.02, 101 / 102 - 1])


def test_risk_adjusted_metrics_are_withheld_on_short_samples() -> None:
    short = m.risk_metrics([0.01, -0.01] * 30, MetricScope.PORTFOLIO, "p")
    assert all(x.value is None and x.status is MetricStatus.INSUFFICIENT_SAMPLE for x in short)


def test_sharpe_and_sortino_on_a_known_vector() -> None:
    returns = [0.002, -0.001] * 100
    out = {x.metric: x for x in m.risk_metrics(returns, MetricScope.PORTFOLIO, "p")}
    mean = sum(returns) / len(returns)
    sd = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    assert float(value(out["sharpe_ratio"])) == pytest.approx(mean / sd * math.sqrt(252), rel=1e-6)
    dd = math.sqrt(sum(min(r, 0) ** 2 for r in returns) / len(returns))
    assert float(value(out["sortino_ratio"])) == pytest.approx(mean / dd * math.sqrt(252), rel=1e-6)
    no_losses = {x.metric: x for x in m.risk_metrics([0.001] * 130, MetricScope.PORTFOLIO, "p")}
    assert no_losses["sortino_ratio"].status is MetricStatus.NOT_APPLICABLE


def test_trade_statistics_and_their_sample_flags() -> None:
    trades = [
        trade("100", seq=1),
        trade("-50", seq=2),
        trade("30", seq=3),
        trade("10", TradeStatus.OPEN_AT_END, seq=4),
    ]
    out = {x.metric: x for x in m.trade_metrics(trades, MetricScope.PORTFOLIO, "p")}
    assert out["trades_closed"].value == 3
    assert out["trades_open_at_end"].value == 1
    assert out["win_rate"].value == D(2) / 3 or abs(value(out["win_rate"]) - D(2) / 3) < D("1e-8")
    assert out["average_winner"].value == D("65.00")
    assert out["average_loser"].value == D("-50.00")
    assert out["profit_factor"].value == D("2.6")
    assert out["expectancy"].value == D("26.67")
    assert out["win_rate"].status is MetricStatus.INSUFFICIENT_SAMPLE
    assert out["average_holding_sessions"].value == D("3.00")
    winners_only = {x.metric: x for x in m.trade_metrics([trade("5")], MetricScope.ASSET, "x")}
    assert winners_only["profit_factor"].status is MetricStatus.NOT_APPLICABLE
    none = {x.metric: x for x in m.trade_metrics([], MetricScope.ASSET, "x")}
    assert none["win_rate"].status is MetricStatus.NOT_APPLICABLE


# ---------------------------------------------------------------------- ledger
def _ledger() -> SimulatedLedger:
    info = InstrumentInfo(ASSET, "FIX", "EQUITY", "USD", D(1), D(1), D(1), None)
    ledger = SimulatedLedger("USD", BacktestTemplateContext("USD", {ASSET: info}))
    ledger.post("opening_funding", {"amount": "10000", "currency": "USD"}, at=T0, source_ref="f")
    return ledger


def test_the_ledger_posts_6a_templates_and_reconciles() -> None:
    ledger = _ledger()
    ledger.post(
        "long_purchase",
        TradeParams(asset_id=ASSET, quantity=D(10), price=D(100), commission=D(1)),
        at=T0,
        source_ref="o1",
    )
    ledger.post(
        "long_sale",
        TradeParams(asset_id=ASSET, quantity=D(10), price=D(110), commission=D(1), fees=D("0.05")),
        at=T0,
        source_ref="o2",
    )
    result = ledger.reconcile()
    assert result.passed, result.failures()
    totals = ledger.figures()
    assert totals.realized_pnl == D(100)
    assert totals.equity == D("10000") + D(100) - D(2) - D("0.05")


def test_the_ledger_refuses_to_overdraw_cash() -> None:
    ledger = _ledger()
    with pytest.raises(LedgerError):
        ledger.post(
            "long_purchase",
            TradeParams(asset_id=ASSET, quantity=D(1000), price=D(100)),
            at=T0,
            source_ref="too-big",
        )
    assert ledger.sequence == 1  # nothing was posted


def test_reconciliation_detects_a_balance_that_differs_from_the_journal() -> None:
    ledger = _ledger()
    key = next(iter(ledger.state.balances))
    ledger.state.balances[key].amount += D("0.01")  # tampering
    result = ledger.reconcile()
    assert not result.passed
    assert {c["check"] for c in result.failures()} >= {"balances_replay"}


# ---------------------------------------------------------------------- configuration
BASE = {
    "name": "x",
    "start_date": "2026-10-01",
    "end_date": "2026-10-30",
    "universe": {"mode": "EXPLICIT", "symbols": ["aapl", " AAPL "]},
    "strategies": [{"strategy_id": "momentum"}],
}


def test_requests_are_validated_strictly() -> None:
    req = BacktestRequest.model_validate(BASE)
    assert req.universe.symbols == ("AAPL",)
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate({**BASE, "end_date": "2026-09-01"})
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate({**BASE, "universe": {"mode": "EXPLICIT"}})
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate({**BASE, "universe": {"mode": "POINT_IN_TIME"}})
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate({**BASE, "execution": {"stop_order_type": "TRAILING_STOP"}})
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate({**BASE, "live_trading": True})
    with pytest.raises(ValidationError):
        BacktestRequest.model_validate(
            {**BASE, "strategies": [{"strategy_id": "a"}, {"strategy_id": "a"}]}
        )
