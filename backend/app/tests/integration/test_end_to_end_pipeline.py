"""The whole paper-trading pipeline with the REAL engines, end to end, on PostgreSQL.

    stored bars -> strategy evaluation worker (the six real families, real feature pipeline)
      -> AI decision coordinator (real policy, LIVE run) -> orchestrator intake -> 6C atomic
      reservation (the risk engine sizes and approves) -> 7A simulated paper order
      -> 7B execution simulator (fills from stored quotes) -> 6A double-entry ledger
      -> position monitor (take-profit / stop-loss exits) -> reconciliations

Earlier phases tested each engine on its own and the orchestrator with stubbed research
stages; nothing ran a real strategy proposal all the way to a closed position. That gap hid
a defect (strategy targets are computed levels that are rarely on the tick grid, and the
take-profit order built from them was refused on every monitor pass, D-229), so this file
is the regression test for the complete chain.

TEST FIXTURE material only: CONTROLLED_TEST accounts with simulated capital, invented
instruments, DETERMINISTIC FIXTURE daily bars (``tests/fixtures/strategy_series``) and MOCK
quotes. Nothing is market data and nothing measures performance. Every order is a simulated
paper order of the isolated paper broker; nothing reaches a brokerage. No ML model and no
stored news exist here, so the coordinator decides on the components that are available
(the others are recorded UNAVAILABLE), exactly as a fresh installation would.

The fixed clock is a Tuesday after the close at least a week ahead of the wall clock: the
engines stamp ``created_at`` with the database clock, and those rows must precede the
decision time as they do in production.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.ai_engine.config import AISettings
from app.ai_engine.enums import DecisionMode
from app.ai_engine.runtime import build_ai_context
from app.asset_discovery import membership
from app.asset_discovery.enums import MembershipAction, UniverseLayer
from app.asset_discovery.sessions import CALENDARS, SessionDay, trading_day
from app.database.models import (
    AIDecisionRecord,
    StrategyEvaluationRecord,
    StrategyProposalRecord,
)
from app.database.models.ai import DecisionRunRecord
from app.database.models.execution import (
    OrderFillRecord,
    OrderRecord,
    PaperPositionRecord,
    PositionEventRecord,
)
from app.database.models.reservations import (
    RiskReservationApprovalRecord,
    RiskReservationRecord,
)
from app.database.sessions import session_scope
from app.execution.enums import (
    ExitReason,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionEventType,
    PositionStatus,
)
from app.execution.monitor import PositionMonitor
from app.execution.reconciliation import ExecutionReconciler
from app.execution.simulation import ExecutionSimulator
from app.orchestration.enums import IntentStatus
from app.orchestration.runtime import OrchestratorRuntime
from app.risk import governance
from app.risk.enums import ReservationStatus, TradeDirection
from app.strategies import catalog
from app.strategies.config import StrategySettings
from app.strategies.enums import Direction, EvaluationStatus
from app.strategies.registry import default_registry
from app.strategies.runtime import build_strategy_context
from app.tests.fixtures.strategy_series import Candle, from_closes, mirror, trend_up
from app.tests.integration.orchestration_support import OPERATOR, Bench, make_bench
from app.tests.integration.simulation_support import Sim
from app.tests.integration.strategy_support import seed_candles
from app.workers.strategy_evaluation import StrategyEvaluationWorker

pytestmark = pytest.mark.integration
CAL = CALENDARS["XNYS"]
D = Decimal
TICK = D("0.01")


def scenario_now() -> datetime:
    """A Tuesday 90 minutes after a regular (not early) close, at least a week ahead of
    the wall clock, whose next day is a trading day too."""
    day = (datetime.now(UTC) + timedelta(days=7)).date()
    while True:
        day += timedelta(days=1)
        if day.weekday() != 1:
            continue
        today = trading_day(CAL, day)
        tomorrow = trading_day(CAL, day + timedelta(days=1))
        if today is not None and tomorrow is not None and not today.early_close:
            return today.regular_close + timedelta(minutes=90)


def flat(count: int = 260, level: float = 50.0) -> list[Candle]:
    """DETERMINISTIC FIXTURE: a featureless series no strategy has a setup in."""
    return from_closes([level + (0.01 if i % 2 else -0.01) for i in range(count)])


@dataclass
class Pipeline:
    """The real research stages on the orchestration bench (TEST FIXTURE data)."""

    bench: Bench
    sim: Sim
    worker: StrategyEvaluationWorker
    research: dict[str, Any]
    now: datetime

    @property
    def factory(self) -> Any:
        return self.bench.factory

    def asset(self, symbol: str, candles: list[Candle]) -> uuid.UUID:
        """An invented instrument in the qualified universe with FIXTURE daily bars, split
        adjusted (what strategies read) and unadjusted (what the risk engine prices from)."""
        bench = self.bench
        asset: uuid.UUID = bench.ex.risk.books.asset(symbol)  # instrument + lot size 1
        seed_candles(self.factory, asset, candles, as_of=self.now)
        with session_scope(self.factory) as session:
            session.execute(
                text(
                    "INSERT INTO market_bars (asset_id, provider, feed, provider_symbol,"
                    " timeframe, adjustment, event_time, revision, open, high, low, close,"
                    " volume, vwap, trade_count, is_complete, source, observation_kind,"
                    " quality_status, received_at, available_at, original_available_at,"
                    " processed_at, ingested_at)"
                    " SELECT asset_id, provider, feed, provider_symbol, timeframe, 'RAW',"
                    " event_time, revision, open, high, low, close, volume, vwap, trade_count,"
                    " is_complete, source, observation_kind, quality_status, received_at,"
                    " available_at, original_available_at, processed_at, ingested_at"
                    " FROM market_bars WHERE asset_id = :asset AND adjustment = 'SPLIT'"
                ),
                {"asset": asset},
            )
            at = self.now - timedelta(days=30)
            membership.transition(
                session,
                asset_id=asset,
                layer=UniverseLayer.QUALIFIED_UNIVERSE,
                action=MembershipAction.ADDED,
                effective_at=at,
                recorded_at=at,
                reason_code="TEST",
                reason="TEST FIXTURE membership",
            )
        bench.ex.risk.adv(asset, "50000000", as_of=self.now - timedelta(hours=1))
        return asset

    def orchestrator(self, worker_id: str = "orchestrator:e2e") -> OrchestratorRuntime:
        return self.bench.orchestrator(
            worker_id=worker_id,
            research=self.research,
            run_strategy_stage=True,
            run_decision_stage=True,
        )

    def tick(self, runtime: OrchestratorRuntime) -> Any:
        return asyncio.run(runtime.orchestrator.tick())

    def next_session(self) -> SessionDay:
        day = self.now.date()
        while True:
            day += timedelta(days=1)
            found = trading_day(CAL, day)
            if found is not None:
                return found

    def rows(self, model: Any, *where: Any) -> list[Any]:
        with session_scope(self.factory) as session:
            found = list(session.scalars(select(model).where(*where)).all())
            session.expunge_all()
            return found

    def count(self, model: Any, *where: Any) -> int:
        with session_scope(self.factory) as session:
            return int(session.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    def reconcile(self) -> None:
        """The ledger (6A), the reservations (6C) and the execution records (7A) agree."""
        bench = self.bench
        bench.ex.dispatch()
        assert bench.ex.risk.reconcile(bench.account) == "PASSED"
        reservations = bench.ex.rv.reconciler.run(bench.account, actor="test")
        assert not reservations.unexplained, reservations.unexplained
        result = ExecutionReconciler(self.factory, bench.clock).run(bench.account)
        assert result.passed, result.findings


@pytest.fixture
def pipeline(fresh_engine) -> Pipeline:
    now = scenario_now()
    bench = make_bench(fresh_engine, name="e2e-pipeline", now=now)
    registry = default_registry()  # the six real strategy families
    settings = StrategySettings(
        _env_file=None, worker_enabled=True, evaluation_timeout_seconds=30, lease_seconds=120
    )
    context = build_strategy_context(
        bench.factory,
        bench.clock,
        provider="alpaca",
        feed="iex",
        settings=settings,
        registry=registry,
        worker_id="e2e-strategy",
    )
    worker = StrategyEvaluationWorker(context, bench.clock)
    worker.sync()
    with session_scope(bench.factory) as session:
        for strategy in registry:
            catalog.set_status(
                session, strategy, enabled=True, reason="end-to-end test", actor=OPERATOR
            )
    ai = build_ai_context(
        bench.factory, bench.clock, settings=AISettings(_env_file=None), worker_id="e2e-ai"
    )

    def decide() -> Any:
        return ai.coordinator.run(
            ai.policy, mode=DecisionMode.LIVE, trigger="orchestrator", actor="e2e-ai"
        )

    ex = bench.ex
    sim = Sim(
        ex,
        ExecutionSimulator(ex.service, bench.clock),
        PositionMonitor(ex.service, bench.clock, accounting=ex.rv.service._accounting),
    )
    return Pipeline(bench, sim, worker, {"strategy_cycle": worker.cycle, "decide": decide}, now)


def open_position(p: Pipeline, asset: uuid.UUID) -> tuple[OrderRecord, PaperPositionRecord]:
    """After the decision tick: the next session opens, the entry is delivered to the paper
    broker and fills in two parts from MOCK quotes (40 displayed, then the rest)."""
    [order] = p.bench.orders()
    opened = p.next_session().regular_open
    p.sim.at(opened + timedelta(minutes=1))
    p.sim.cycle(1)  # the paper broker receives the order; nothing to fill it yet
    assert p.bench.ex.order(order.id).status is OrderStatus.OPEN
    p.sim.later(10)
    p.sim.quote(asset, "100.40", "100.45", ask_size="40")
    p.sim.cycle(1)
    partial = p.bench.ex.order(order.id)
    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert D(0) < partial.filled_quantity < partial.quantity
    p.sim.later(10)
    p.sim.quote(asset, "100.42", "100.47", ask_size="5000")
    p.sim.cycle(2)
    filled = p.bench.ex.order(order.id)
    assert filled.status is OrderStatus.FILLED
    assert filled.filled_quantity == filled.quantity
    position = p.sim.position(order.id)
    assert position is not None
    assert position.status is PositionStatus.OPEN
    assert position.open_quantity == filled.quantity
    return filled, position


def test_real_strategy_to_closed_position_with_take_profit(pipeline: Pipeline) -> None:
    p, bench = pipeline, pipeline.bench
    winner = p.asset("E2EW", trend_up())
    quiet = p.asset("E2EQ", flat())
    bench.enable()
    runtime = p.orchestrator()

    # ---- after the close: strategies, coordinator, intake, reservation, order ----------
    report = p.tick(runtime)
    assert report.errors == []
    assert report.strategy is not None
    assert report.strategy["assets"] == 2
    assert report.decision_run
    assert (report.intents_taken, report.intents_delivered) == (1, 1)

    evaluations = p.rows(StrategyEvaluationRecord)
    assert {e.strategy_id for e in evaluations} == {s.strategy_id for s in default_registry()}
    assert all(e.status is not EvaluationStatus.FAILED for e in evaluations)
    # No trade is a valid outcome: the featureless asset gets no proposal from anyone.
    [proposal] = p.rows(StrategyProposalRecord)
    assert (proposal.asset_id, proposal.strategy_id) == (winner, "trend_following")
    assert proposal.invalidation_level < proposal.reference_entry < proposal.target_reference
    assert not p.rows(StrategyProposalRecord, StrategyProposalRecord.asset_id == quiet)
    # The fixture's target is a computed level off the 0.01 grid, like real targets.
    assert proposal.target_reference % TICK != 0

    [run] = p.rows(DecisionRunRecord)
    assert (run.mode, run.status) == ("LIVE", "COMPLETED")
    [decision] = p.rows(AIDecisionRecord)
    assert decision.outcome == "PRIORITIZE_FOR_REVIEW"
    assert decision.asset_id == winner
    assert proposal.id in decision.proposal_ids

    [intent] = bench.intents()
    assert intent.status is IntentStatus.ORDER_CREATED
    assert intent.ai_decision_id == decision.id
    [reservation] = bench.reservations()
    assert reservation.status is ReservationStatus.APPROVED
    [order] = bench.orders()
    assert (order.purpose, order.order_type) == (OrderPurpose.ENTRY, OrderType.LIMIT)
    with session_scope(p.factory) as session:
        approval = session.scalar(
            select(RiskReservationApprovalRecord).where(
                RiskReservationApprovalRecord.reservation_id == reservation.id
            )
        )
        assert approval is not None
        # Size and price are the risk engine's: 0.5% of 100,000 at risk between the worst
        # entry and the strategy's stop, whole shares, the limit on the tick grid.
        assert order.quantity == approval.approved_quantity
        assert order.limit_price == approval.worst_entry_price
        assert order.limit_price % TICK == 0
        assert approval.stop == proposal.invalidation_level
        risk = (approval.worst_entry_price - approval.stop) * approval.approved_quantity
        assert D(0) < risk <= D("500.00")
        assert order.quantity == order.quantity.to_integral_value()
    assert order.expires_at == p.next_session().regular_close

    # ---- a restart and repeated ticks change nothing -----------------------------------
    p.tick(runtime)
    p.tick(p.orchestrator(worker_id="orchestrator:e2e-restarted"))
    assert len(p.rows(StrategyProposalRecord)) == 1
    assert len(bench.intents()) == len(bench.reservations()) == len(bench.orders()) == 1

    # ---- the next session: partial fills, position, protection ------------------------
    entry, position = open_position(p, winner)
    entry_fills = bench.ex.fills(entry.id)
    assert len(entry_fills) >= 2  # 40 displayed first, then the remainder
    assert sum((f.quantity for f in entry_fills), D(0)) == entry.quantity
    assert entry.limit_price is not None
    assert all(f.price <= entry.limit_price for f in entry_fills)  # never beyond the limit
    assert position.stop_price == proposal.invalidation_level
    assert position.target_price == proposal.target_reference
    assert position.strategy_proposal_id == proposal.id
    assert position.ai_decision_id == decision.id
    [consumed] = p.rows(RiskReservationRecord)
    assert consumed.status is not ReservationStatus.APPROVED  # consumed by the fills

    # The monitor rests take-profits for exactly what the position holds (one per filled
    # part), each at the target moved onto the tick grid (at or beyond the target) and
    # accepted by the execution engine.
    summary = p.sim.monitor_once()
    assert summary.errors == []
    assert summary.exits_refused == 0
    p.sim.cycle(1)
    take_profits = [o for o in bench.orders() if o.purpose is OrderPurpose.EXIT]
    assert take_profits
    assert sum((o.quantity for o in take_profits), D(0)) == position.open_quantity
    for o in take_profits:
        assert o.order_type is OrderType.TAKE_PROFIT
        assert o.status is OrderStatus.OPEN
        assert o.limit_price is not None
        assert o.limit_price % TICK == 0
        assert D(0) <= o.limit_price - position.target_price < TICK
    for _ in range(3):  # further passes place nothing more
        assert p.sim.monitor_once().take_profits_placed == 0
    assert p.count(OrderRecord, OrderRecord.purpose == OrderPurpose.EXIT) == len(take_profits)
    refused = [
        e
        for e in p.rows(PositionEventRecord)
        if e.event_type is PositionEventType.EXIT_SUBMITTED
        and e.details.get("decision") == "REFUSED"
    ]
    assert refused == []

    # ---- the market reaches the target: the take-profit fills, the position closes ----
    p.sim.later(600)
    p.sim.quote(winner, "106.10", "106.15", bid_size="5000")
    p.sim.cycle(2)
    closed = p.sim.position(entry.id)
    assert closed is not None
    assert closed.status is PositionStatus.CLOSED
    assert closed.exit_reason is ExitReason.TAKE_PROFIT
    assert closed.open_quantity == 0
    exit_fills = [f for o in take_profits for f in bench.ex.fills(o.id)]
    assert sum((f.quantity for f in exit_fills), D(0)) == entry.quantity
    limit = take_profits[0].limit_price
    assert limit is not None
    assert all(f.price >= limit for f in exit_fills)  # never below the take-profit limit
    bought = sum((f.quantity * f.price for f in entry_fills), D(0))
    sold = sum((f.quantity * f.price for f in exit_fills), D(0))
    assert closed.realized_pnl == sold - bought > 0
    costs = sum((f.commission + f.fees for f in [*entry_fills, *exit_fills]), D(0))
    assert closed.commissions + closed.fees == costs > 0

    # ---- nothing is duplicated and every book agrees -----------------------------------
    p.sim.cycle(2)
    p.tick(p.orchestrator(worker_id="orchestrator:e2e-after-close"))
    assert len(bench.intents()) == len(bench.reservations()) == 1
    assert len(bench.orders()) == 1 + len(take_profits)
    assert p.count(OrderFillRecord) == len(entry_fills) + len(exit_fills)
    assert p.count(PaperPositionRecord) == 1
    p.reconcile()


def test_real_pipeline_exits_at_the_stop_with_a_loss(pipeline: Pipeline) -> None:
    p, bench = pipeline, pipeline.bench
    loser = p.asset("E2EL", trend_up())
    bench.enable()
    report = p.tick(p.orchestrator())
    assert (report.intents_taken, report.intents_delivered, report.errors) == (1, 1, [])
    entry, position = open_position(p, loser)
    p.sim.cycle(1)  # the take-profit rests
    assert position.stop_price is not None

    # The market falls through the approved stop: one protective MARKET exit, the resting
    # take-profit is cancelled (one cancels the other), the loss is realized.
    p.sim.later(300)
    p.sim.quote(loser, "95.90", "95.95", bid_size="5000")
    p.sim.cycle(3)
    p.sim.later(5)
    p.sim.quote(loser, "95.88", "95.93", bid_size="5000")
    p.sim.cycle(3)
    closed = p.sim.position(entry.id)
    assert closed is not None
    assert closed.status is PositionStatus.CLOSED
    assert closed.exit_reason is ExitReason.STOP_LOSS
    assert closed.realized_pnl < 0
    exits = [o for o in bench.orders() if o.purpose is OrderPurpose.EXIT]
    stops = [o for o in exits if o.exit_reason is ExitReason.STOP_LOSS]
    targets = [o for o in exits if o.exit_reason is ExitReason.TAKE_PROFIT]
    assert len(stops) == 1  # exactly one protective exit
    assert stops[0].order_type is OrderType.MARKET
    assert stops[0].status is OrderStatus.FILLED
    assert targets  # the resting take-profits were cancelled, never filled
    assert all(o.status is OrderStatus.CANCELLED for o in targets)
    assert all(o.filled_quantity == 0 for o in targets)
    # A stop is never filled at its own level when the market is below it.
    assert all(f.price < position.stop_price for f in bench.ex.fills(stops[0].id))
    # The loss stays near the risk the engine approved (0.5% of equity) plus the gap.
    assert closed.realized_pnl > D("-650")
    p.sim.cycle(2)
    assert p.count(OrderRecord, OrderRecord.exit_reason == ExitReason.STOP_LOSS) == 1
    p.reconcile()


def test_real_pipeline_never_orders_while_disabled_or_halted(pipeline: Pipeline) -> None:
    p, bench = pipeline, pipeline.bench
    p.asset("E2ES", trend_up())

    # DISABLED (the default): shadow operation. Proposals and decisions are recorded,
    # nothing is taken in, reserved or ordered.
    report = p.tick(p.orchestrator())
    assert report.mode == "DISABLED"
    assert report.errors == []
    assert len(p.rows(StrategyProposalRecord)) == 1
    [decision] = p.rows(AIDecisionRecord)
    assert decision.outcome == "PRIORITIZE_FOR_REVIEW"
    assert bench.intents() == []
    assert bench.reservations() == []
    assert bench.orders() == []

    # ENABLED with the kill switch engaged: the decision is taken in and the deterministic
    # risk engine refuses it. A no-trade outcome, no order, nothing left reserved.
    bench.control.risk.service.engage_kill_switch(
        bench.account, actor=OPERATOR, reason="halt for the end-to-end test"
    )
    bench.enable()
    report = p.tick(p.orchestrator(worker_id="orchestrator:e2e-halted"))
    assert (report.intents_taken, report.intents_delivered) == (1, 1)
    [intent] = bench.intents()
    assert intent.status is IntentStatus.RISK_REJECTED
    assert intent.order_id is None
    assert bench.orders() == []
    assert all(r.status is not ReservationStatus.APPROVED for r in bench.reservations())
    assert p.count(PaperPositionRecord) == 0
    p.reconcile()


def test_real_pipeline_short_sale_covers_at_its_target(pipeline: Pipeline) -> None:
    """The mirrored series: the same strategy proposes a SHORT. The entry is a short sale
    (a liability in the ledger), the take-profit is a buy-to-cover resting at the first tick
    at or BELOW the target, and the books reconcile with the short's own postings."""
    p, bench = pipeline, pipeline.bench
    falling = p.asset("E2ED", mirror(trend_up()))
    # The FIXTURE provider reference reports no short margin requirement. The risk engine
    # then rejects every short (MARGIN_DATA_MISSING) unless an operator chose the
    # documented policy of using the configured minimum rate, as here.
    with session_scope(p.factory) as session:
        governance.change(
            session,
            bench.account,
            actor=OPERATOR,
            reason="end-to-end test: use the configured minimum short margin rate",
            at=bench.now(),
            hard_limit_updates={"missing_margin_policy": "USE_CONFIGURED_MINIMUM"},
        )
    bench.enable()
    report = p.tick(p.orchestrator())
    assert (report.intents_taken, report.intents_delivered, report.errors) == (1, 1, [])
    [proposal] = p.rows(StrategyProposalRecord)
    assert proposal.direction is Direction.SHORT
    assert proposal.target_reference < proposal.reference_entry < proposal.invalidation_level
    [order] = bench.orders()
    assert order.side is OrderSide.SELL_SHORT
    assert order.limit_price is not None

    opened = p.next_session().regular_open
    p.sim.at(opened + timedelta(minutes=1))
    p.sim.cycle(1)
    p.sim.later(10)
    bid = order.limit_price + D("0.05")  # MOCK quote just above the short's limit
    p.sim.quote(falling, str(bid), str(bid + D("0.05")), bid_size="5000")
    p.sim.cycle(2)
    entry = bench.ex.order(order.id)
    assert entry.status is OrderStatus.FILLED
    position = p.sim.position(entry.id)
    assert position is not None
    assert position.direction is TradeDirection.SHORT
    assert position.target_price == proposal.target_reference
    assert all(f.price >= order.limit_price for f in bench.ex.fills(entry.id))

    summary = p.sim.monitor_once()
    assert (summary.exits_refused, summary.errors) == (0, [])
    p.sim.cycle(1)
    [cover] = [o for o in bench.orders() if o.purpose is OrderPurpose.EXIT]
    assert cover.order_type is OrderType.TAKE_PROFIT
    assert cover.side is OrderSide.BUY_TO_COVER
    assert cover.limit_price is not None
    assert cover.limit_price % TICK == 0
    assert D(0) <= position.target_price - cover.limit_price < TICK

    p.sim.later(600)
    ask = cover.limit_price - D("0.20")  # the market trades through the target
    p.sim.quote(falling, str(ask - D("0.05")), str(ask), ask_size="5000")
    p.sim.cycle(2)
    closed = p.sim.position(entry.id)
    assert closed is not None
    assert closed.status is PositionStatus.CLOSED
    assert closed.exit_reason is ExitReason.TAKE_PROFIT
    assert closed.realized_pnl > 0
    assert all(f.price <= cover.limit_price for f in bench.ex.fills(cover.id))
    p.sim.cycle(2)
    assert p.count(PaperPositionRecord) == 1
    p.reconcile()
