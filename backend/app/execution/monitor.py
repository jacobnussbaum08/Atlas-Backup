"""The position monitor: automated management of simulated positions (Phase 7B).

**Simulated positions only; exits are simulated paper orders.** One pass over the open
positions (and closed ones with something left to tidy up), each in its own PostgreSQL
transaction under the account lock:

1. **valuation**: the newest *fresh* stored price (6A ``valuation_price``: trade, quote mid
   or completed bar, never invented) marks the position; unrealized P/L follows. A stale or
   missing price leaves the last mark and flags the position (``monitor_status = STALE``).
   Periodically the 6A valuation checkpoint posts the account's unrealized valuation.
2. **protection** (:mod:`app.execution.protection`): the adverse levels (approved stop,
   trailing stop, invalidation) against the observations since the last check, then expiry
   and operator close requests. For equities only regular-session data counts.
3. **action**: an adverse trigger, expiry or close request withdraws the unfilled remainder
   of a still-working entry order, cancels resting take-profit orders (one-cancels-other)
   and, once nothing else can fill, submits **one** MARKET exit for what the position holds.
   With no trigger, a resting TAKE_PROFIT limit is kept at the target for the quantity no
   other exit covers. The limit is the target moved onto the instrument's tick grid, never
   to the near side of it (strategy targets are computed levels, rarely on the grid).
4. **costs**: short positions accrue borrow daily (6A ``financing_cost``, accrued) at the
   model's rate on the last fresh mark; a closed position's accrued borrow is paid (6A
   ``accrual_settlement``).

Every exit goes through the execution engine's normal submission (authorized submitter
``system:execution-position-monitor``, validation, idempotency key derived from the
position) and the paper broker; every change is a ``position_events`` row. Exits are never
blocked by new-entry circuit breakers (D-157); nothing here can open or increase exposure.

A submission the execution engine **refuses** creates no order. It is never counted as an
exit: it is recorded (``EXIT_SUBMITTED`` event with ``decision: REFUSED``), reported in the
pass summary's ``errors`` (the worker's heartbeat turns DEGRADED) and counted in
``exits_refused``. A refused protective exit is retried on every pass (it protects the
position); an identical refused take-profit only after ``refused_retry_interval`` (D-229).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.config import AccountingSettings
from app.accounting.enums import EntrySource, PriceStatus
from app.accounting.money import currency
from app.accounting.posting import PostingRequest, PostingService
from app.core.clocks import Clock
from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.database.models import Asset
from app.database.models.execution import (
    OrderRecord,
    PaperPositionRecord,
    PositionCloseRequestRecord,
    PositionEventRecord,
)
from app.database.models.portfolio import PortfolioSnapshotRecord
from app.events.outbox import enqueue
from app.execution.enums import (
    FINAL_ORDER_STATUSES,
    CloseRequestStatus,
    ExecutionDecision,
    ExitReason,
    OrderPurpose,
    OrderStatus,
    OrderType,
    PositionEventType,
    PositionStatus,
)
from app.execution.models import OrderRequest, SubmissionResult, plain
from app.execution.positions import PositionBook
from app.execution.protection import Levels, decide
from app.execution.service import RECONCILE_TOPIC, ExecutionService
from app.execution.simulation import market_open
from app.execution.validation import closable_quantity
from app.paper_broker import market
from app.paper_broker.execution_model import DEFAULT_MODEL, ExecutionModel
from app.paper_broker.fill_model import ceil_to, cost_round, floor_to
from app.portfolio.pricing import valuation_price
from app.portfolio.valuation import ValuationService
from app.risk.enums import TradeDirection
from app.risk.state import lock_account

logger = get_logger(__name__)
MONITOR_ACTOR = "system:execution-position-monitor"
OPERATOR_PREFIX = "operator:"
ZERO = Decimal(0)
CANCELLING = frozenset({OrderStatus.CANCEL_PENDING})


@dataclass(frozen=True)
class MonitorConfig:
    valuation_interval: timedelta = timedelta(minutes=15)
    batch: int = 200
    # An identical take-profit the execution engine refused is not resubmitted before this
    # (a refusal is a validation result: the same request gets the same answer).
    refused_retry_interval: timedelta = timedelta(minutes=15)


@dataclass
class MonitorSummary:
    positions: int = 0
    triggers: int = 0
    exits_submitted: int = 0
    take_profits_placed: int = 0
    exits_refused: int = 0
    cancels_requested: int = 0
    borrow_accruals: int = 0
    valuations: int = 0
    stale: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class CloseResult:
    request_id: uuid.UUID
    position_id: uuid.UUID
    status: CloseRequestStatus
    created: bool


def _midnight(day: Any) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


def take_profit_limit(target: Decimal, tick: Decimal | None, direction: TradeDirection) -> Decimal:
    """The limit of a resting take-profit: ``target`` on the tick grid, at or beyond it (a
    long sells at or above its target, a short covers at or below), so the simulated exit
    never books a level the strategy's target had not reached. Unchanged when it is already
    on the grid or the tick is unknown."""
    if tick is None or tick <= ZERO:
        return target
    if direction is TradeDirection.LONG:
        return ceil_to(target, tick)
    below = floor_to(target, tick)
    return below if below > ZERO else ceil_to(target, tick)


def _refused(result: SubmissionResult) -> bool:
    return result.decision is ExecutionDecision.REFUSED or result.order_id is None


def _refusal(position: PaperPositionRecord, what: str, result: SubmissionResult) -> dict[str, Any]:
    codes = ", ".join(str(r.get("code")) for r in result.reasons) or "no reason given"
    return {
        "position_id": str(position.id),
        "error": f"{what} for {position.symbol} was refused by the execution engine ({codes}); "
        "no order exists",
    }


class PositionMonitor:
    def __init__(
        self,
        service: ExecutionService,
        clock: Clock,
        model: ExecutionModel = DEFAULT_MODEL,
        *,
        accounting: AccountingSettings,
        posting: PostingService | None = None,
        config: MonitorConfig | None = None,
    ) -> None:
        self.service = service
        self.clock = clock
        self.model = model
        self.accounting = accounting
        self.config = config or MonitorConfig()
        self.positions: PositionBook = service.positions
        ledger_posting = getattr(service._ledger, "_posting", None)
        self.posting: PostingService | None = posting or ledger_posting
        self.valuation = (
            None
            if self.posting is None
            else ValuationService(service._factory, clock, accounting, self.posting)
        )

    # ------------------------------------------------------------------ runs
    def candidates(self, session: Session) -> list[uuid.UUID]:
        open_rows = session.scalars(
            select(PaperPositionRecord.id)
            .where(PaperPositionRecord.status == PositionStatus.OPEN)
            .order_by(PaperPositionRecord.opened_at, PaperPositionRecord.id)
            .limit(self.config.batch)
        ).all()
        tidy = session.scalars(
            select(PaperPositionRecord.id)
            .where(
                PaperPositionRecord.status == PositionStatus.CLOSED,
                (PaperPositionRecord.borrow_paid < PaperPositionRecord.borrow_accrued)
                | PaperPositionRecord.entry_order_id.in_(
                    select(OrderRecord.parent_order_id).where(
                        OrderRecord.purpose == OrderPurpose.EXIT,
                        OrderRecord.status.notin_(list(FINAL_ORDER_STATUSES)),
                    )
                )
                | PaperPositionRecord.id.in_(
                    select(PositionCloseRequestRecord.position_id).where(
                        PositionCloseRequestRecord.status.in_(
                            [CloseRequestStatus.PENDING, CloseRequestStatus.EXIT_SUBMITTED]
                        )
                    )
                ),
            )
            .limit(self.config.batch)
        ).all()
        return list(dict.fromkeys([*open_rows, *tidy]))

    def run_once(self) -> MonitorSummary:
        summary = MonitorSummary()
        ids = self.service.transaction(self.candidates)
        accounts: set[uuid.UUID] = set()
        for position_id in ids:
            try:
                account = self.service.transaction(
                    partial(self.monitor_in, position_id=position_id, summary=summary)
                )
                if account is not None:
                    accounts.add(account)
            except Exception as exc:  # one position never stops the pass
                logger.exception("execution.monitor_failed", position_id=str(position_id))
                summary.errors.append({"position_id": str(position_id), "error": str(exc)[:500]})
        summary.positions = len(ids)
        for account_id in sorted(accounts):
            try:
                if self.service.transaction(partial(self._valuation_in, account_id=account_id)):
                    summary.valuations += 1
            except Exception as exc:
                logger.exception("execution.monitor_valuation_failed", account_id=str(account_id))
                summary.errors.append({"account_id": str(account_id), "error": str(exc)[:500]})
        return summary

    # ------------------------------------------------------------------ one position
    def _lock(self, session: Session, position_id: uuid.UUID) -> PaperPositionRecord:
        found = session.get(PaperPositionRecord, position_id)
        if found is None:
            raise NotFoundError(f"position {position_id} does not exist")
        lock_account(session, found.portfolio_account_id)
        position = session.scalar(
            select(PaperPositionRecord)
            .where(PaperPositionRecord.id == position_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert position is not None  # noqa: S101  # never deleted
        return position

    def _exits(self, session: Session, position: PaperPositionRecord) -> list[OrderRecord]:
        return list(
            session.scalars(
                select(OrderRecord)
                .where(
                    OrderRecord.parent_order_id == position.entry_order_id,
                    OrderRecord.purpose == OrderPurpose.EXIT,
                    OrderRecord.status.notin_(list(FINAL_ORDER_STATUSES)),
                )
                .order_by(OrderRecord.submitted_at, OrderRecord.id)
                .execution_options(populate_existing=True)
            ).all()
        )

    def _pending_requests(
        self, session: Session, position: PaperPositionRecord
    ) -> list[PositionCloseRequestRecord]:
        return list(
            session.scalars(
                select(PositionCloseRequestRecord)
                .where(
                    PositionCloseRequestRecord.position_id == position.id,
                    PositionCloseRequestRecord.status.in_(
                        [CloseRequestStatus.PENDING, CloseRequestStatus.EXIT_SUBMITTED]
                    ),
                )
                .with_for_update()
            ).all()
        )

    def monitor_in(
        self, session: Session, position_id: uuid.UUID, summary: MonitorSummary | None = None
    ) -> uuid.UUID | None:
        summary = summary or MonitorSummary()
        position = self._lock(session, position_id)
        now = self.clock.now()
        exits = self._exits(session, position)
        requests = self._pending_requests(session, position)
        if position.status is PositionStatus.CLOSED:
            self._tidy_closed(session, position, exits, requests, now, summary)
            return None
        entry = session.get(OrderRecord, position.entry_order_id, populate_existing=True)
        assert entry is not None  # noqa: S101  # foreign key
        rules = self.model.for_class(position.asset_class)
        self._mark(session, position, now)
        if position.direction is TradeDirection.SHORT:
            self._accrue_borrow(session, position, rules, now, summary)
        decision_reason = self._evaluate(session, position, rules, bool(requests), now, summary)
        if decision_reason is not None and position.trigger_reason in (
            None,
            ExitReason.TAKE_PROFIT,
        ):
            position.trigger_reason = decision_reason[0]
            position.triggered_at = now
            position.trigger_price = decision_reason[1]
            summary.triggers += 1
            self.positions.event(
                session,
                position,
                PositionEventType.TRIGGERED,
                at=now,
                actor=MONITOR_ACTOR,
                reason=f"{decision_reason[0].value}"
                + ("" if decision_reason[1] is None else f" at {plain(decision_reason[1])}")
                + f": exit {plain(position.open_quantity)} {position.symbol} (simulated)",
                price=decision_reason[1],
                details=decision_reason[2],
            )
        self._act(session, position, entry, exits, requests, now, summary)
        return position.portfolio_account_id

    # ------------------------------------------------------------------ steps
    def _mark(self, session: Session, position: PaperPositionRecord, now: datetime) -> None:
        asset = session.get(Asset, position.asset_id)
        assert asset is not None  # noqa: S101  # foreign key
        price = valuation_price(session, asset, now, self.accounting)
        if price.status is PriceStatus.FRESH and price.observation is not None:
            mark = price.observation.price
            sign = 1 if position.direction is TradeDirection.LONG else -1
            position.last_mark_price = mark
            position.last_mark_at = price.observation.observed_at
            position.last_mark_source = price.observation.source
            position.unrealized_pnl = currency(position.currency).round(
                (mark - position.average_entry_price) * position.open_quantity * sign
            )

    def _evaluate(
        self,
        session: Session,
        position: PaperPositionRecord,
        rules: Any,
        close_requested: bool,
        now: datetime,
        summary: MonitorSummary,
    ) -> tuple[ExitReason, Decimal | None, dict[str, Any]] | None:
        view = market.load(
            session,
            position.asset_id,
            as_of=now,
            floor=position.opened_at,
            trade_after=None
            if position.monitor_cursor_at is None or position.monitor_trade_id is None
            else (position.monitor_cursor_at, position.monitor_trade_id),
            bar_after=position.monitor_bar_at,
            timeframe=rules.bar_timeframe,
        )
        if view.trades:
            position.monitor_cursor_at = view.trades[-1].event_time
            position.monitor_trade_id = view.trades[-1].id
        if view.bars:
            position.monitor_bar_at = view.bars[-1].start
        quote = view.quote
        # The current market: the newest quote if it is fresh (the quote prevailing when the
        # position opened is the current market too; trades and bars count only after it).
        fresh = (
            quote
            if quote is not None
            and now - quote.event_time <= timedelta(seconds=rules.quote_max_age_seconds)
            else None
        )
        is_open = rules.supported and market_open(rules, now)
        has_data = bool(view.trades or view.bars or fresh)
        status = "ACTIVE" if is_open and has_data else ("MARKET_CLOSED" if not is_open else "STALE")
        if status == "STALE" and position.monitor_status != "STALE":
            summary.stale += 1
            self.positions.event(
                session,
                position,
                PositionEventType.MONITOR_DEFERRED,
                at=now,
                actor=MONITOR_ACTOR,
                reason="no fresh price observation: protective levels cannot be evaluated "
                "(no exit is executed at a stale or invented price)",
                details={"quote": None if quote is None else quote.ref()},
            )
        position.monitor_status = status
        levels = Levels(
            direction=position.direction,
            stop=position.stop_price,
            tick=position.price_increment,
            trailing_stop=position.trailing_stop_price,
            invalidation=position.invalidation_price,
            trail_amount=position.trail_amount,
            trail_percent=position.trail_percent,
            trail_reference=position.trail_reference_price,
        )
        if not is_open:
            view = market.MarketView(as_of=now, floor=position.opened_at, quote=None)
            fresh = None
        decision = decide(
            levels,
            view,
            fresh,
            now=now,
            expires_at=position.expires_at,
            close_requested=close_requested,
        )
        if (
            decision.trailing_stop is not None
            and decision.trailing_stop != position.trailing_stop_price
        ):
            before = position.trailing_stop_price
            position.trail_reference_price = decision.trail_reference
            position.trailing_stop_price = decision.trailing_stop
            self.positions.event(
                session,
                position,
                PositionEventType.PROTECTION_UPDATED,
                at=now,
                actor=MONITOR_ACTOR,
                reason=f"trailing stop {'set' if before is None else 'ratcheted'} to "
                f"{plain(decision.trailing_stop)} (reference "
                f"{plain(decision.trail_reference) if decision.trail_reference else '-'})",
                price=decision.trailing_stop,
                details={"before": None if before is None else str(before)},
            )
        if decision.reason is None:
            return None
        observed = None
        if decision.trigger is not None:
            observed = Decimal(decision.trigger["observed"])
        return decision.reason, observed, {**decision.details, "trigger": decision.trigger}

    def _act(
        self,
        session: Session,
        position: PaperPositionRecord,
        entry: OrderRecord,
        exits: list[OrderRecord],
        requests: list[PositionCloseRequestRecord],
        now: datetime,
        summary: MonitorSummary,
    ) -> None:
        reason = position.trigger_reason
        if reason is None or reason is ExitReason.TAKE_PROFIT:
            self._keep_take_profit(session, position, entry, exits, now, summary)
            return
        # An adverse trigger, the expiry or a close request: exit everything.
        if entry.status in (OrderStatus.APPROVED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
            self.service.request_cancel_in(
                session,
                entry.id,
                idempotency_key=f"position:{position.id}:entry-cancel",
                actor=MONITOR_ACTOR,
                reason=f"{reason.value}: the unfilled entry remainder is withdrawn",
            )
            summary.cancels_requested += 1
            self.positions.event(
                session,
                position,
                PositionEventType.ENTRY_CANCEL_REQUESTED,
                at=now,
                actor=MONITOR_ACTOR,
                reason=f"{reason.value}: cancellation of the unfilled entry remainder requested",
                order_id=entry.id,
            )
        waiting = False
        for order in exits:
            if order.exit_reason is ExitReason.TAKE_PROFIT or order.order_type in (
                OrderType.LIMIT,
                OrderType.TAKE_PROFIT,
            ):
                if order.status in CANCELLING:
                    waiting = True
                    continue
                self.service.request_cancel_in(
                    session,
                    order.id,
                    idempotency_key=f"position:{position.id}:oco:{order.id}",
                    actor=MONITOR_ACTOR,
                    reason=f"one-cancels-other: {reason.value} takes precedence",
                )
                summary.cancels_requested += 1
                waiting = True
            else:
                waiting = True  # a protective or operator exit is working: let it finish
        if waiting:
            return
        closable = min(closable_quantity(session, entry), position.open_quantity)
        if closable <= 0:
            return
        result = self._submit_exit(session, position, closable, OrderType.MARKET, None, reason, now)
        if _refused(result):
            # No order exists: the trigger stands and the next pass tries again.
            summary.exits_refused += 1
            summary.errors.append(_refusal(position, f"the {reason.value} exit", result))
            logger.error(
                "execution.protective_exit_refused",
                simulated=True,
                position_id=str(position.id),
                exit_reason=reason.value,
                codes=[r.get("code") for r in result.reasons][:10],
            )
            return
        summary.exits_submitted += 1
        for request in requests:
            if request.status is CloseRequestStatus.PENDING:
                request.status = CloseRequestStatus.EXIT_SUBMITTED
                request.outcome = f"exit submitted ({reason.value})"

    def _keep_take_profit(
        self,
        session: Session,
        position: PaperPositionRecord,
        entry: OrderRecord,
        exits: list[OrderRecord],
        now: datetime,
        summary: MonitorSummary,
    ) -> None:
        if position.target_price is None:
            return
        uncovered = min(closable_quantity(session, entry), position.open_quantity)
        if uncovered <= 0:
            return
        limit = take_profit_limit(
            position.target_price, position.price_increment, position.direction
        )
        earlier = self._refused_take_profit(session, position, uncovered, limit, now)
        if earlier is not None:
            summary.errors.append(earlier)  # still without a take-profit: stay visible
            return
        result = self._submit_exit(
            session,
            position,
            uncovered,
            OrderType.TAKE_PROFIT,
            limit,
            ExitReason.TAKE_PROFIT,
            now,
        )
        if _refused(result):
            summary.exits_refused += 1
            summary.errors.append(_refusal(position, "the take-profit order", result))
            logger.error(
                "execution.take_profit_refused",
                simulated=True,
                position_id=str(position.id),
                codes=[r.get("code") for r in result.reasons][:10],
            )
            return
        summary.take_profits_placed += 1

    def _refused_take_profit(
        self,
        session: Session,
        position: PaperPositionRecord,
        quantity: Decimal,
        limit: Decimal,
        now: datetime,
    ) -> dict[str, Any] | None:
        """The error to report when the newest exit attempt was this same take-profit,
        refused less than ``refused_retry_interval`` ago; None when it may be submitted."""
        last = session.scalar(
            select(PositionEventRecord)
            .where(
                PositionEventRecord.position_id == position.id,
                PositionEventRecord.event_type == PositionEventType.EXIT_SUBMITTED,
            )
            .order_by(PositionEventRecord.sequence.desc())
            .limit(1)
        )
        if last is None or last.details.get("decision") != ExecutionDecision.REFUSED.value:
            return None
        if last.details.get("exit_reason") != ExitReason.TAKE_PROFIT.value:
            return None
        if last.quantity != quantity or last.price != limit:
            return None
        if now - last.occurred_at >= self.config.refused_retry_interval:
            return None
        codes = ", ".join(str(r.get("code")) for r in last.details.get("reasons", []))
        return {
            "position_id": str(position.id),
            "error": f"the take-profit order for {position.symbol} at {plain(limit)} was "
            f"refused at {last.occurred_at.isoformat()} ({codes or 'no reason given'}); no "
            "take-profit is resting",
        }

    def _submit_exit(
        self,
        session: Session,
        position: PaperPositionRecord,
        quantity: Decimal,
        order_type: OrderType,
        limit: Decimal | None,
        reason: ExitReason,
        now: datetime,
    ) -> SubmissionResult:
        number = position.exits_submitted + 1
        expires = now + self.service.config.protective_order_lifetime
        if position.expires_at is not None and reason is ExitReason.TAKE_PROFIT:
            expires = min(expires, max(position.expires_at, now + timedelta(minutes=1)))
        result = self.service.submit_in(
            session,
            OrderRequest(
                account_id=position.portfolio_account_id,
                purpose=OrderPurpose.EXIT,
                parent_order_id=position.entry_order_id,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit,
                expires_at=expires,
                exit_reason=reason,
            ),
            idempotency_key=f"position:{position.id}:exit:{number}",
            requested_by=MONITOR_ACTOR,
        )
        position.exits_submitted = number
        self.positions.event(
            session,
            position,
            PositionEventType.EXIT_SUBMITTED,
            at=now,
            actor=MONITOR_ACTOR,
            reason=f"{reason.value}: {order_type.value} exit of {plain(quantity)}"
            + ("" if limit is None else f" at {plain(limit)}")
            + f" ({result.decision.value})",
            quantity=quantity,
            price=limit,
            order_id=result.order_id,
            details={
                "reasons": result.reasons,
                "request_id": str(result.request_id),
                "decision": result.decision.value,
                "order_type": order_type.value,
                "exit_reason": reason.value,
            },
        )
        return result

    def _tidy_closed(
        self,
        session: Session,
        position: PaperPositionRecord,
        exits: list[OrderRecord],
        requests: list[PositionCloseRequestRecord],
        now: datetime,
        summary: MonitorSummary,
    ) -> None:
        for order in exits:
            if order.status in CANCELLING:
                continue
            self.service.request_cancel_in(
                session,
                order.id,
                idempotency_key=f"position:{position.id}:closed:{order.id}",
                actor=MONITOR_ACTOR,
                reason="the position is closed: nothing is left for this exit",
            )
            summary.cancels_requested += 1
        for request in requests:
            request.status = CloseRequestStatus.COMPLETED
            request.resolved_at = now
            request.outcome = (
                f"position closed ({position.exit_reason.value if position.exit_reason else '-'})"
            )
        due = position.borrow_accrued - position.borrow_paid
        if due > 0 and self.posting is not None:
            posted = self.posting.post_in(
                session,
                PostingRequest(
                    account_id=position.portfolio_account_id,
                    template="accrual_settlement",
                    params={
                        "payable": "FINANCING",
                        "currency": position.currency,
                        "amount": str(due),
                    },
                    event_id=f"borrow-settle:{position.id}:{plain(position.borrow_accrued)}",
                    effective_at=now,
                    source=EntrySource.SIMULATED_EXECUTION,
                    actor=MONITOR_ACTOR,
                    source_ref=f"position:{position.id}",
                ),
            )
            position.borrow_paid = position.borrow_accrued
            self.positions.event(
                session,
                position,
                PositionEventType.COSTS_SETTLED,
                at=now,
                actor=MONITOR_ACTOR,
                reason=f"accrued borrow {plain(due)} {position.currency} paid (simulated)",
                journal_entry_id=posted.entry_id,
            )
            self._reconcile_later(session, position.portfolio_account_id, posted.sequence, now)

    # ------------------------------------------------------------------ costs
    def _accrue_borrow(
        self,
        session: Session,
        position: PaperPositionRecord,
        rules: Any,
        now: datetime,
        summary: MonitorSummary,
    ) -> None:
        if self.posting is None or rules.borrow_rate_annual <= 0 or position.open_quantity <= 0:
            return
        start = (position.borrow_accrued_through or position.opened_at).date()
        if now.date() <= start and position.borrow_accrued_through is not None:
            return
        days = (now.date() - start).days
        if days <= 0:
            return
        if position.last_mark_price is None:
            if position.monitor_status != "BORROW_WAITING":
                self.positions.event(
                    session,
                    position,
                    PositionEventType.MONITOR_DEFERRED,
                    at=now,
                    actor=MONITOR_ACTOR,
                    reason="borrow accrual waits for a fresh price (no value is assumed)",
                )
            return
        value = position.open_quantity * position.last_mark_price
        amount = cost_round(
            value * rules.borrow_rate_annual * days / rules.day_count,
            currency(position.currency).minor_units,
        )
        through = _midnight(now.date())
        if amount > 0:
            posted = self.posting.post_in(
                session,
                PostingRequest(
                    account_id=position.portfolio_account_id,
                    template="financing_cost",
                    params={
                        "kind": "BORROW",
                        "currency": position.currency,
                        "amount": str(amount),
                        "accrue": True,
                        "asset_id": str(position.asset_id),
                    },
                    event_id=f"borrow:{position.id}:{start.isoformat()}:{days}",
                    effective_at=now,
                    source=EntrySource.SIMULATED_EXECUTION,
                    actor=MONITOR_ACTOR,
                    source_ref=f"position:{position.id}",
                ),
            )
            position.borrow_accrued += amount
            self._reconcile_later(session, position.portfolio_account_id, posted.sequence, now)
            journal = posted.entry_id
        else:
            journal = None
        position.borrow_accrued_through = through
        summary.borrow_accruals += 1
        self.positions.event(
            session,
            position,
            PositionEventType.BORROW_ACCRUED,
            at=now,
            actor=MONITOR_ACTOR,
            reason=f"borrow {plain(amount)} {position.currency} accrued for {days} day(s) on "
            f"{plain(position.open_quantity)} x {plain(position.last_mark_price)} at "
            f"{plain(rules.borrow_rate_annual * 100)}%/year (simulated assumption)",
            journal_entry_id=journal,
            details={
                "days": days,
                "mark_at": None
                if position.last_mark_at is None
                else position.last_mark_at.isoformat(),
            },
        )

    @staticmethod
    def _reconcile_later(
        session: Session, account_id: uuid.UUID, sequence: int, at: datetime
    ) -> None:
        enqueue(
            session,
            topic=RECONCILE_TOPIC,
            aggregate_type="portfolio_account",
            aggregate_id=account_id,
            dedupe_key=f"{RECONCILE_TOPIC}:{account_id}:{sequence}",
            payload={"account_id": str(account_id), "entry_sequence": sequence},
            at=at,
        )

    def _valuation_in(self, session: Session, account_id: uuid.UUID) -> bool:
        """A 6A valuation checkpoint when the last one is older than the interval."""
        if self.valuation is None:
            return False
        now = self.clock.now()
        latest = session.scalar(
            select(PortfolioSnapshotRecord.as_of)
            .where(PortfolioSnapshotRecord.portfolio_account_id == account_id)
            .order_by(PortfolioSnapshotRecord.as_of.desc())
            .limit(1)
        )
        if latest is not None and now - latest < self.config.valuation_interval:
            return False
        result = self.valuation.checkpoint_in(session, account_id, as_of=now, actor=MONITOR_ACTOR)
        if result.created and result.valuation_entry_id is not None:
            from app.accounting.posting import last_sequence

            self._reconcile_later(session, account_id, last_sequence(session, account_id), now)
        return result.created

    # ------------------------------------------------------------------ operator commands
    def request_close(
        self, position_id: uuid.UUID, *, idempotency_key: str, actor: str, reason: str
    ) -> CloseResult:
        """An operator asks to close a position (the monitor exits it at its next pass)."""
        if not actor.startswith(OPERATOR_PREFIX) or len(actor) <= len(OPERATOR_PREFIX):
            raise InvalidRequestError("only an operator (operator:<name>) may close a position")
        if len((reason or "").strip()) < 3:
            raise InvalidRequestError("a close request needs a reason")

        def work(session: Session) -> CloseResult:
            position = self._lock(session, position_id)
            existing = session.scalar(
                select(PositionCloseRequestRecord).where(
                    PositionCloseRequestRecord.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.position_id != position_id:
                    raise InvalidRequestError(
                        f"idempotency key {idempotency_key!r} was used for another position"
                    )
                return CloseResult(existing.id, position_id, existing.status, created=False)
            now = self.clock.now()
            closed = position.status is PositionStatus.CLOSED
            request = PositionCloseRequestRecord(
                id=uuid.uuid4(),
                position_id=position_id,
                idempotency_key=idempotency_key,
                requested_by=actor,
                reason=reason.strip(),
                status=CloseRequestStatus.REFUSED if closed else CloseRequestStatus.PENDING,
                requested_at=now,
                resolved_at=now if closed else None,
                outcome="the position is already closed" if closed else None,
            )
            session.add(request)
            session.flush()
            if not closed:
                self.positions.event(
                    session,
                    position,
                    PositionEventType.CLOSE_REQUESTED,
                    at=now,
                    actor=actor,
                    reason=f"close requested: {reason.strip()}",
                    details={"request_id": str(request.id)},
                )
            return CloseResult(request.id, position_id, request.status, created=True)

        return self.service.transaction(work)

    def tighten(
        self,
        position_id: uuid.UUID,
        *,
        actor: str,
        reason: str,
        stop: Decimal | None = None,
        trail_amount: Decimal | None = None,
        trail_percent: Decimal | None = None,
    ) -> PaperPositionRecord:
        """An operator tightens protection: a stop closer to the market than the current one,
        or a trailing stop. Loosening is refused (the reservation was sized with the stop)."""
        if not actor.startswith(OPERATOR_PREFIX) or len(actor) <= len(OPERATOR_PREFIX):
            raise InvalidRequestError("only an operator may change a position's protection")
        if (trail_amount is not None) and (trail_percent is not None):
            raise InvalidRequestError("a trailing stop has one offset: amount or percent")

        def work(session: Session) -> PaperPositionRecord:
            position = self._lock(session, position_id)
            if position.status is PositionStatus.CLOSED:
                raise InvalidRequestError("the position is closed")
            long = position.direction is TradeDirection.LONG
            changes: dict[str, str] = {}
            if stop is not None:
                looser = stop < position.stop_price if long else stop > position.stop_price
                if looser or stop <= 0:
                    raise InvalidRequestError(
                        f"a stop of {plain(stop)} would loosen the current "
                        f"{plain(position.stop_price)}"
                    )
                if position.price_increment and stop % position.price_increment != 0:
                    raise InvalidRequestError(f"{plain(stop)} is not on the tick grid")
                if position.target_price is not None and (
                    stop >= position.target_price if long else stop <= position.target_price
                ):
                    raise InvalidRequestError("the stop would cross the target")
                position.stop_price = stop
                changes["stop"] = str(stop)
            if trail_amount is not None or trail_percent is not None:
                if trail_amount is not None and trail_amount <= 0:
                    raise InvalidRequestError("the trailing amount must be positive")
                if trail_percent is not None and not ZERO < trail_percent < 100:
                    raise InvalidRequestError("the trailing percent must be between 0 and 100")
                position.trail_amount = trail_amount
                position.trail_percent = trail_percent
                changes["trail_amount"] = str(trail_amount)
                changes["trail_percent"] = str(trail_percent)
            if not changes:
                raise InvalidRequestError("nothing to change")
            self.positions.event(
                session,
                position,
                PositionEventType.PROTECTION_UPDATED,
                at=self.clock.now(),
                actor=actor,
                reason=f"protection tightened: {reason.strip() or 'operator'}",
                details=changes,
            )
            session.flush()
            session.expunge(position)
            return position

        return self.service.transaction(work)


__all__ = [
    "MONITOR_ACTOR",
    "CloseResult",
    "MonitorConfig",
    "MonitorSummary",
    "PositionMonitor",
]
