"""Simulated orders of a backtest, filled by the Phase 7B fill model.

Orders follow the Phase 7A state machine (``app.execution.state_machine.next_status``: an
illegal transition raises), and every fill decision is made by
``app.paper_broker.fill_model.evaluate`` with the run's versioned
:class:`~app.paper_broker.execution_model.ExecutionModel`: touching a limit is not a fill,
stops trigger on the bar's extreme, a triggered stop fills no better than its stop, entries
never fill beyond the approval's worst entry price, participation caps size every fill from
the bar's volume (partial fills), and costs are the model's commissions and fees.

What the backtest adds, because only completed daily bars exist historically (documented in
the execution model's rules, so it is part of the model's content hash):

* the view of one evaluation is the bar of one session, used only by orders that reached the
  simulator before that session opened (the bar must start at or after the order's arrival);
  no quote or trade exists for the session, so marketable orders use the 7B bar fallback
  (the close plus the assumed half spread, slippage and impact);
* **gap rule**: a stop triggered by a bar that *opened* beyond it fills no better than that
  open (adjusted adversely by the slippage), because the market traded there first; the 7B
  bar fallback alone would cap it at the stop price.
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.backtesting.data import DayBar
from app.backtesting.enums import OrderRole
from app.execution import state_machine
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    AttemptOutcome,
    OrderEventType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.paper_broker import fill_model
from app.paper_broker.execution_model import EQUITY, AssetClassModel, ExecutionModel
from app.paper_broker.fill_model import Evaluation, Fill, OrderState, adverse_price
from app.paper_broker.market import Bar, MarketView

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)

BACKTEST_RULES = (
    "backtest: one evaluation per session, on that session's completed daily bar only, "
    "available at the regular close plus the settle delay",
    "backtest: a bar is used only by orders that reached the simulator before it started",
    "backtest: no historical quote or trade is used; marketable orders use the bar "
    "fallback (close plus the assumed half spread, slippage and impact)",
    "backtest: a stop triggered by a bar that opened beyond it fills no better than that "
    "open, adjusted adversely by the slippage (gap rule)",
    "backtest: protective stops are attached to the entry (bracket); in the bar an entry "
    "fills, only the stop is evaluated: a take-profit never fills in its entry's bar "
    "(stop first, no intrabar path assumed)",
)

DAILY_EQUITY = EQUITY.model_copy(
    update={"bar_timeframe": "1Day", "bar_max_age_seconds": 86_400, "allow_bar_fallback": True}
)


def backtest_model(overrides: dict[str, Any] | None = None) -> ExecutionModel:
    """The default 7B execution model with daily-bar settings and the backtest rules; cost
    or participation overrides produce a new content-addressed model."""
    equity = DAILY_EQUITY
    code = "atlas-backtest-daily"
    if overrides:
        equity = AssetClassModel.model_validate({**DAILY_EQUITY.model_dump(), **overrides})
    model = ExecutionModel(
        code=code,
        version=1,
        description=(
            "Phase 8A daily-bar replay of the 7B conservative execution model: bar-fallback "
            "fills, strictly-through limits, participation caps of bar volume, stop first, "
            "gap rule. Simulated; not a replica of any venue."
        ),
        classes={"EQUITY": equity, "ETF": equity},
        rules=(*ExecutionModel().rules, *BACKTEST_RULES),
    )
    if overrides:
        model = model.model_copy(
            update={"code": f"atlas-backtest-{model.definition_sha256()[:12]}"}
        )
    return model


@dataclass
class OrderEvent:
    event: OrderEventType
    at: datetime
    status: OrderStatus
    reason: str
    quantity: Decimal | None = None
    price: Decimal | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "event": self.event.value,
            "at": self.at.isoformat(),
            "status": self.status.value,
            "reason": self.reason,
            "quantity": None if self.quantity is None else str(self.quantity),
            "price": None if self.price is None else str(self.price),
        }


@dataclass
class SimFill:
    seq: int
    order_seq: int
    trade_seq: int | None
    asset_id: uuid.UUID
    symbol: str
    side: OrderSide
    role: OrderRole
    session: Any  # date
    filled_at: datetime  # the bar's end (the fill happened during that session)
    known_at: datetime  # when the replay learned of it (the bar's availability)
    quantity: Decimal
    price: Decimal
    commission: Decimal
    fees: Decimal
    basis: str
    reference_price: Decimal | None
    slippage_bps: Decimal | None
    impact_bps: Decimal
    participation: Decimal | None
    liquidity: Decimal | None
    gap: bool
    assumptions: tuple[str, ...]
    journal_sequence: int | None = None

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass
class SimOrder:
    seq: int
    role: OrderRole
    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    arrival_at: datetime
    tick: Decimal | None
    increment: Decimal
    min_quantity: Decimal
    minor_units: int
    trade_seq: int | None = None
    proposal_seq: int | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_percent: Decimal | None = None
    protection_price: Decimal | None = None
    expires_at: datetime | None = None
    status: OrderStatus | None = None
    filled: Decimal = ZERO
    filled_value: Decimal = ZERO
    commission: Decimal = ZERO
    fees: Decimal = ZERO
    triggered: bool = False
    triggered_at: datetime | None = None
    trigger_level: Decimal | None = None
    trail_reference: Decimal | None = None
    current_stop: Decimal | None = None
    arrival_price: Decimal | None = None
    adverse_level: Decimal | None = None
    final_reason: str | None = None
    last_outcome: str | None = None
    events: list[OrderEvent] = field(default_factory=list)
    attempts: int = 0

    @property
    def purpose(self) -> OrderPurpose:
        return OrderPurpose.ENTRY if self.role is OrderRole.ENTRY else OrderPurpose.EXIT

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled

    @property
    def working(self) -> bool:
        return self.status in state_machine.FILLABLE

    @property
    def final(self) -> bool:
        return self.status in FINAL_ORDER_STATUSES

    @property
    def average_price(self) -> Decimal | None:
        return None if self.filled == 0 else self.filled_value / self.filled

    # ------------------------------------------------------------------ transitions
    def _apply(
        self,
        event: OrderEventType,
        at: datetime,
        reason: str,
        *,
        fill: Decimal = ZERO,
        price: Decimal | None = None,
    ) -> None:
        target = state_machine.next_status(
            self.status, event, quantity=self.quantity, filled=self.filled, fill=fill
        )
        self.status = target
        self.events.append(OrderEvent(event, at, target, reason, fill if fill else None, price))

    def submit(self, at: datetime, reason: str) -> None:
        """CREATED -> PROPOSED -> APPROVED -> OPEN (the simulated broker accepts at once)."""
        self._apply(OrderEventType.CREATED, at, reason)
        self._apply(OrderEventType.APPROVED, at, "validated against the reservation approval")
        self._apply(OrderEventType.ACCEPTED, at, "accepted by the backtest's simulated broker")

    def cancel(self, at: datetime, reason: str) -> None:
        if self.status not in state_machine.CANCELLABLE:
            return
        self._apply(OrderEventType.CANCEL_REQUESTED, at, reason)
        self._apply(OrderEventType.CANCEL_CONFIRMED, at, "cancellation confirmed (simulated)")
        self.final_reason = reason

    def expire(self, at: datetime, reason: str) -> None:
        if self.final:
            return
        self._apply(OrderEventType.EXPIRED, at, reason)
        self.final_reason = reason

    def resize(self, quantity: Decimal) -> None:
        """A protective order follows the position it protects (bracket amendment)."""
        if quantity < self.filled:
            raise ValueError("a protective order cannot shrink below what it filled")
        self.quantity = quantity

    def record_fill(self, fill: Fill, at: datetime) -> None:
        self._apply(OrderEventType.FILL, at, fill.basis.value, fill=fill.quantity, price=fill.price)
        self.filled += fill.quantity
        self.filled_value += fill.quantity * fill.price
        self.commission += fill.costs.commission
        self.fees += fill.costs.fees

    # ------------------------------------------------------------------ fill model
    def state(self, max_fill: Decimal) -> OrderState:
        return OrderState(
            side=self.side,
            order_type=self.order_type,
            purpose=self.purpose,
            quantity=self.quantity,
            remaining=self.remaining,
            max_fill=min(max_fill, self.remaining),
            quantity_increment=self.increment,
            min_quantity=self.min_quantity,
            tick=self.tick,
            minor_units=self.minor_units,
            limit_price=self.limit_price,
            stop_price=self.stop_price,
            trail_amount=None,
            trail_percent=self.trail_percent,
            protection_price=self.protection_price if self.role is OrderRole.ENTRY else None,
            triggered=self.triggered,
            triggered_at=self.triggered_at,
            trigger_level=self.trigger_level,
            trail_reference=self.trail_reference,
            current_stop=self.current_stop,
            arrival_price=self.arrival_price,
            filled_quantity=self.filled,
            filled_value=self.filled_value,
            commission_charged=self.commission,
            adverse_level=self.adverse_level,
        )


def view_of(bar: DayBar) -> MarketView:
    """One session as the fill model sees it: its completed bar, no quote, no trade."""
    return MarketView(
        as_of=bar.available_at,
        floor=bar.start,
        quote=None,
        trades=(),
        bars=(
            Bar(
                id=bar.bar_id,
                start=bar.start,
                end=bar.end,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                available_at=bar.available_at,
            ),
        ),
    )


def _gap_cap(order: SimOrder, ev: Evaluation, bar: DayBar, model: AssetClassModel) -> Evaluation:
    """The gap rule: a stop triggered by a bar that opened beyond it fills no better than
    that open, adjusted adversely by the slippage (never invented beyond the bar's range)."""
    fill = ev.fill
    trigger = ev.trigger
    if fill is None or not trigger or trigger.get("source") != "BAR" or not trigger.get("gap"):
        return ev
    slip = model.slippage_bps / BPS
    if order.side.buys:
        cap = adverse_price(bar.open * (ONE + slip), order.tick, buys=True)
        cap = min(cap, bar.high)
        price = max(fill.price, cap)
    else:
        cap = adverse_price(bar.open * (ONE - slip), order.tick, buys=False)
        cap = max(cap, bar.low)
        price = min(fill.price, cap)
    if price == fill.price:
        return ev
    state = order.state(fill.quantity)
    adjusted = dataclasses.replace(
        fill,
        price=price,
        costs=fill_model.costs(model, state, fill.quantity, price),
        assumptions=(
            *fill.assumptions,
            "gap: the bar opened beyond the stop; filled no better than the open",
        ),
    )
    return dataclasses.replace(
        ev,
        fill=adjusted,
        message=f"{ev.message}; gap rule: no better than the open {bar.open}",
    )


def evaluate(
    order: SimOrder, bar: DayBar, model: AssetClassModel, *, max_fill: Decimal
) -> Evaluation | None:
    """Evaluate one working order against one session's bar (None: the bar predates the
    order, or nothing may be filled)."""
    if not order.working or bar.start < order.arrival_at or max_fill <= 0:
        return None
    if not bar.valid:
        return None
    order.attempts += 1
    ev = fill_model.evaluate(order.state(max_fill), view_of(bar), model, market_open=True)
    ev = _gap_cap(order, ev, bar, model)
    order.trail_reference = ev.trail_reference
    order.current_stop = ev.current_stop
    if order.arrival_price is None:
        order.arrival_price = ev.arrival_price
    if ev.triggered_now and ev.trigger is not None:
        order.triggered = True
        order.triggered_at = datetime.fromisoformat(ev.trigger["at"])
        order.trigger_level = Decimal(ev.trigger["level"])
        order.events.append(
            OrderEvent(
                OrderEventType.TRIGGERED,
                bar.available_at,
                order.status or OrderStatus.OPEN,
                f"stop {ev.trigger['level']} reached ({ev.trigger['source']})",
            )
        )
    order.last_outcome = f"{ev.outcome.value}:{ev.code}"
    if ev.outcome in (AttemptOutcome.FILLED, AttemptOutcome.PARTIALLY_FILLED):
        assert ev.fill is not None  # noqa: S101  # a fill outcome carries a fill
        order.record_fill(ev.fill, bar.available_at)
    return ev


__all__ = [
    "BACKTEST_RULES",
    "DAILY_EQUITY",
    "OrderEvent",
    "SimFill",
    "SimOrder",
    "backtest_model",
    "evaluate",
    "view_of",
]
