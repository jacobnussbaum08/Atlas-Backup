"""The execution simulator: turns working paper orders into simulated fills (Phase 7B).

**Simulated execution only.** For each order the isolated paper broker is working, one
evaluation is one PostgreSQL transaction under the engine's locking protocol (account,
order, paper broker book row, then 6C reservation and position rows):

1. skip when no market observation became available since the last evaluation;
2. read the observable data (:mod:`app.paper_broker.market`) and decide with the fill model
   (:mod:`app.paper_broker.fill_model`) under the order's execution-model version;
3. record a TRIGGERED broker event when a stop triggers;
4. before a fill, re-run the engine's pre-fill checks (``revalidate_fill``: limit, tick and
   lot grid, reserved worst entry price, new-entry circuit breakers, position capacity);
5. record the FILL broker event with its price/cost breakdown, advance the data cursors (an
   observation's liquidity counts once), and apply the events in the same transaction: the
   6A posting (SIMULATED_EXECUTION), the 6C consumption, the fill row and the position.

A crash anywhere rolls the whole evaluation back; the next run re-evaluates from the same
cursors. A second simulator evaluating the same order waits on the account lock and then
finds the cursors advanced: no observation can produce two fills.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.money import currency
from app.accounting.reference import ReferenceDataError, instrument_info
from app.asset_discovery.sessions import CALENDARS, session_at
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.data_engine.calendar import MarketSession
from app.database.models.execution import (
    ExecutionAttemptRecord,
    ExecutionFillDetailRecord,
    ExecutionModelRecord,
    OrderRecord,
    PaperBrokerOrderRecord,
)
from app.execution.enums import (
    AttemptOutcome,
    BrokerOrderStatus,
    BrokerRunState,
    OrderPurpose,
    OrderType,
)
from app.execution.models import plain
from app.execution.protection import Levels, binding
from app.execution.service import ExecutionService
from app.execution.state_machine import FILLABLE
from app.paper_broker import market
from app.paper_broker.execution_model import DEFAULT_MODEL, AssetClassModel, ExecutionModel
from app.paper_broker.fill_model import Evaluation, OrderState, evaluate

logger = get_logger(__name__)
SIMULATOR_ACTOR = "system:execution-simulator"
XNYS = CALENDARS["XNYS"]


@dataclass
class SimulationSummary:
    candidates: int = 0
    evaluated: int = 0
    skipped: int = 0
    fills: int = 0
    triggers: int = 0
    deferred: int = 0
    paused: bool = False
    errors: list[dict[str, Any]] = field(default_factory=list)


def market_open(model: AssetClassModel, at: Any) -> bool:
    if not model.regular_session_only:
        return True
    return session_at(XNYS, at) is MarketSession.REGULAR


class ExecutionSimulator:
    def __init__(
        self,
        service: ExecutionService,
        clock: Clock,
        model: ExecutionModel = DEFAULT_MODEL,
        *,
        batch: int = 200,
    ) -> None:
        self.service = service
        self.broker = service.broker
        self.clock = clock
        self.model = model
        self.batch = batch
        self._models: dict[int, ExecutionModel] = {}

    # ------------------------------------------------------------------ models
    def model_id(self, session: Session) -> int:
        """The current model's id in this database (registered on first use)."""
        model_id = self.model.register(session)
        self._models[model_id] = self.model
        return model_id

    def model_for(self, session: Session, model_id: int) -> ExecutionModel:
        """The model version a book row is worked under (not necessarily the current one)."""
        found = self._models.get(model_id)
        if found is None:
            record = session.get(ExecutionModelRecord, model_id)
            assert record is not None  # noqa: S101  # foreign key
            found = ExecutionModel.model_validate(record.definition)
            self._models[model_id] = found
        return found

    # ------------------------------------------------------------------ runs
    def candidates(self, session: Session) -> list[uuid.UUID]:
        rows = session.scalars(
            select(PaperBrokerOrderRecord.client_order_id)
            .where(
                PaperBrokerOrderRecord.broker_id == self.broker.broker_id,
                PaperBrokerOrderRecord.status.in_(
                    [BrokerOrderStatus.WORKING, BrokerOrderStatus.PARTIALLY_FILLED]
                ),
            )
            .order_by(
                PaperBrokerOrderRecord.last_evaluated_at.asc().nulls_first(),
                PaperBrokerOrderRecord.client_order_id,
            )
            .limit(self.batch)
        ).all()
        return list(rows)

    def run_once(self) -> SimulationSummary:
        summary = SimulationSummary()
        health = self.service.transaction(self.broker.health)
        if health.status is not BrokerRunState.RUNNING:
            summary.paused = True  # a paused or stopped broker decides nothing
            return summary
        ids = self.service.transaction(self.candidates)
        summary.candidates = len(ids)
        for order_id in ids:
            try:
                result = self.evaluate_order(order_id)
            except Exception as exc:  # one order never stops the run
                logger.exception("execution.simulation_failed", order_id=str(order_id))
                summary.errors.append({"order_id": str(order_id), "error": str(exc)[:500]})
                continue
            if result is None:
                summary.skipped += 1
                continue
            summary.evaluated += 1
            if result.triggered_now:
                summary.triggers += 1
            if result.fill is not None and result.outcome in (
                AttemptOutcome.FILLED,
                AttemptOutcome.PARTIALLY_FILLED,
            ):
                summary.fills += 1
            if result.outcome is AttemptOutcome.DEFERRED:
                summary.deferred += 1
        return summary

    def evaluate_order(self, order_id: uuid.UUID, *, force: bool = False) -> Evaluation | None:
        """Evaluate one working order in its own transaction (None: nothing to evaluate)."""
        return self.service.transaction(lambda s: self.evaluate_in(s, order_id, force=force))

    # ------------------------------------------------------------------ one order
    def evaluate_in(
        self, session: Session, order_id: uuid.UUID, *, force: bool = False
    ) -> Evaluation | None:
        account, order = self.service.lock_order(session, order_id)
        if order.status not in FILLABLE:
            return None
        book = self.broker.working_book(session, order.id)
        if book is None:
            return None
        now = self.clock.now()
        if (
            not force
            and book.market_cursor_at is not None
            and not market.has_new_data(
                session, order.asset_id, after=book.market_cursor_at, as_of=now
            )
        ):
            return None
        if book.execution_model_id is None:
            book.execution_model_id = self.model_id(session)
        model_id = book.execution_model_id
        rules = self.model_for(session, model_id).for_class(order.asset_class)
        view, evaluation = self._decide(session, order, book, rules, now)
        return self._record(session, account, order, book, view, evaluation, model_id, now)

    def _decide(
        self,
        session: Session,
        order: OrderRecord,
        book: PaperBrokerOrderRecord,
        rules: AssetClassModel,
        now: Any,
    ) -> tuple[market.MarketView, Evaluation]:
        latency = timedelta(seconds=float(rules.latency_seconds))
        floor = book.received_at + latency
        view = market.load(
            session,
            order.asset_id,
            as_of=now,
            floor=floor,
            trade_after=None
            if book.trade_cursor_at is None or book.trade_cursor_id is None
            else (book.trade_cursor_at, book.trade_cursor_id),
            bar_after=book.bar_cursor_at,
            timeframe=rules.bar_timeframe,
        )
        if order.purpose is OrderPurpose.EXIT:
            max_fill = self.service.exit_capacity(session, order)
            if max_fill <= 0:
                return view, Evaluation(
                    AttemptOutcome.NO_FILL,
                    "NOTHING_HELD",
                    "the position holds nothing this exit could close",
                )
        else:
            max_fill = order.remaining_quantity
        try:
            minimum = instrument_info(session, order.asset_id, now).min_quantity
        except ReferenceDataError:
            minimum = order.quantity_increment
        state = OrderState(
            side=order.side,
            order_type=order.order_type,
            purpose=order.purpose,
            quantity=order.quantity,
            remaining=order.remaining_quantity,
            max_fill=max_fill,
            quantity_increment=order.quantity_increment,
            min_quantity=minimum,
            tick=order.price_increment,
            minor_units=currency(order.quote_currency).minor_units,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            trail_amount=order.trail_amount,
            trail_percent=order.trail_percent,
            protection_price=order.protection_price,
            triggered=book.triggered_at is not None,
            triggered_at=book.triggered_at,
            trigger_level=book.trigger_price,
            trail_reference=book.trail_reference_price,
            current_stop=book.current_stop_price,
            quote_used=book.quote_cursor_id,
            arrival_price=book.arrival_price,
            filled_quantity=order.filled_quantity,
            filled_value=order.filled_value,
            commission_charged=book.commission_charged,
            adverse_level=self._adverse_level(session, order),
        )
        return view, evaluate(state, view, rules, market_open=market_open(rules, now))

    def _adverse_level(self, session: Session, order: OrderRecord) -> Decimal | None:
        """For a take-profit exit: the binding protective level of its position."""
        if order.purpose is not OrderPurpose.EXIT or order.order_type not in (
            OrderType.TAKE_PROFIT,
            OrderType.LIMIT,
        ):
            return None
        assert order.parent_order_id is not None  # noqa: S101  # database CHECK
        position = self.service.positions.for_entry(session, order.parent_order_id, lock=False)
        if position is None:
            return None
        level, _reason = binding(
            Levels(
                direction=position.direction,
                stop=position.stop_price,
                tick=position.price_increment,
                trailing_stop=position.trailing_stop_price,
                invalidation=position.invalidation_price,
            )
        )
        return level

    def _record(
        self,
        session: Session,
        account: Any,
        order: OrderRecord,
        book: PaperBrokerOrderRecord,
        view: market.MarketView,
        ev: Evaluation,
        model_id: int,
        now: Any,
    ) -> Evaluation:
        book.evaluations += 1
        book.last_evaluated_at = now
        book.market_cursor_at = now
        if book.arrival_price is None and ev.arrival_price is not None:
            book.arrival_price = ev.arrival_price
        if order.order_type is OrderType.TRAILING_STOP and book.triggered_at is None:
            book.trail_reference_price = ev.trail_reference
            book.current_stop_price = ev.current_stop
        if view.trades:
            book.trade_cursor_at = view.trades[-1].event_time
            book.trade_cursor_id = view.trades[-1].id
        if view.bars:
            book.bar_cursor_at = view.bars[-1].start
        session.flush()

        trigger_event = None
        if ev.triggered_now and ev.trigger is not None:
            trigger_event = self.broker.record_trigger(
                session,
                book,
                level=Decimal(ev.trigger["level"]),
                at=now,
                execution_model_id=model_id,
                details=ev.trigger,
            )
        outcome, code, message = ev.outcome, ev.code, ev.message
        fill_event = None
        fill = ev.fill
        if fill is not None:
            problems = self.service.revalidate_fill(
                session, account, order, fill.quantity, fill.price
            )
            if problems:
                outcome = AttemptOutcome.TRIGGERED if ev.triggered_now else AttemptOutcome.DEFERRED
                code = problems[0]["code"]
                message = "simulated fill held back: " + "; ".join(p["message"] for p in problems)
                fill = None
            else:
                fill_event = self.broker.record_simulated_fill(
                    session,
                    book,
                    quantity=fill.quantity,
                    price=fill.price,
                    commission=fill.costs.commission,
                    fees=fill.costs.fees,
                    at=now,
                    execution_model_id=model_id,
                    reason=f"simulated fill: {ev.message}",
                    details={
                        "basis": fill.basis.value,
                        "observations": fill.observations,
                        "assumptions": list(fill.assumptions),
                        "simulated": True,
                    },
                )
                if fill.quote_id is not None:
                    book.quote_cursor_id = fill.quote_id
                session.add(
                    ExecutionFillDetailRecord(
                        broker_event_id=fill_event.id,
                        order_id=order.id,
                        portfolio_account_id=order.portfolio_account_id,
                        execution_model_id=model_id,
                        basis=fill.basis,
                        side=order.side,
                        order_type=order.order_type,
                        quantity=fill.quantity,
                        price=fill.price,
                        reference_price=fill.reference_price,
                        touch_price=fill.touch_price,
                        arrival_price=book.arrival_price,
                        spread=fill.spread,
                        slippage_bps=fill.slippage_bps,
                        impact_bps=fill.impact_bps,
                        shortfall_bps=fill.shortfall_bps,
                        liquidity=fill.liquidity,
                        participation=fill.participation,
                        commission=fill.costs.commission,
                        regulatory_fees=fill.costs.regulatory_fees,
                        exchange_fees=fill.costs.exchange_fees,
                        currency=order.quote_currency,
                        gap=fill.gap,
                        filled_at=now,
                        observations=fill.observations,
                        assumptions=list(fill.assumptions),
                    )
                )
        changed = (outcome, code) != (book.last_attempt_outcome, book.last_attempt_code)
        if changed or trigger_event is not None or fill_event is not None:
            session.add(
                ExecutionAttemptRecord(
                    order_id=order.id,
                    broker_order_id=book.id,
                    execution_model_id=model_id,
                    evaluated_at=now,
                    outcome=outcome,
                    code=code,
                    message=message[:2000],
                    triggered=trigger_event is not None,
                    fill_event_id=None if fill_event is None else fill_event.id,
                    market=view.summary(),
                    details={
                        "trigger": ev.trigger,
                        "trail_reference": None
                        if ev.trail_reference is None
                        else str(ev.trail_reference),
                        "current_stop": None if ev.current_stop is None else str(ev.current_stop),
                        "remaining_before": str(order.remaining_quantity),
                        **(
                            {}
                            if fill is None
                            else {
                                "quantity": str(fill.quantity),
                                "price": str(fill.price),
                                "basis": fill.basis.value,
                            }
                        ),
                    },
                )
            )
        book.last_attempt_outcome = outcome
        book.last_attempt_code = code
        session.flush()
        if trigger_event is not None or fill_event is not None:
            self.service.apply_pending_in(session, order, SIMULATOR_ACTOR)
            logger.info(
                "execution.simulated",
                simulated=True,
                order_id=str(order.id),
                outcome=outcome.value,
                code=code,
                quantity=None if fill is None else plain(fill.quantity),
            )
        if fill is None and ev.fill is not None:
            return Evaluation(
                outcome,
                code,
                message,
                triggered_now=ev.triggered_now,
                trigger=ev.trigger,
                trail_reference=ev.trail_reference,
                current_stop=ev.current_stop,
                arrival_price=ev.arrival_price,
            )
        return ev


__all__ = ["SIMULATOR_ACTOR", "ExecutionSimulator", "SimulationSummary", "market_open"]
