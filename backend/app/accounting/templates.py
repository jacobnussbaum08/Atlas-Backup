"""Posting templates: pure functions from a ledger state and inputs to a balanced draft.

Each template is versioned (``posting_templates``) and documented line by line in
``docs/architecture/accounting.md``. Templates never read the database: the posting service
loads the state and the rates and instrument specifications they need, and records the
result. That keeps every accounting rule unit-testable with deterministic vectors.

Phase 6A posts opening funding, valuation checkpoints and reversals on the primary account,
and controlled test transactions (trades, costs, conversions) on CONTROLLED_TEST accounts.
There is no execution source: no order engine calls these templates yet.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.accounting.chart import (
    ACCRUED_FEES,
    ACCRUED_FINANCING,
    BORROW_COSTS,
    CASH,
    COMMISSIONS,
    CONTRIBUTED_CAPITAL,
    FEES,
    FUNDING_COSTS,
    FX_CLEARING,
    FX_TRANSLATION,
    LONG_COST,
    LONG_VALUATION,
    REALIZED_FX,
    REALIZED_PNL,
    RESTRICTED_CASH,
    SHORT_PROCEEDS,
    SHORT_VALUATION,
    UNREALIZED_FX,
    UNREALIZED_PNL,
)
from app.accounting.enums import EntryType, FeeKind, FinancingKind, MovementType, PayableKind, Side
from app.accounting.ledger import DraftLine, EntryDraft, QuantityMovement, RateBasis
from app.accounting.money import (
    ONE,
    PRICE_SCALE,
    QUANTITY_SCALE,
    RATE_SCALE,
    ZERO,
    MoneyError,
    currency,
    div,
    functional_places,
    mul,
    proportion,
    require_non_negative,
    require_positive,
    require_scale,
    round_functional,
    round_to,
    to_decimal,
)
from app.accounting.state import LedgerState, Position


class TemplateError(ValueError):
    """The inputs cannot be posted (invalid, inconsistent with the books, or unsupported)."""


# ------------------------------------------------------------------------ inputs
StrictDecimal = Annotated[Decimal, BeforeValidator(lambda v: to_decimal(v))]


class _Params(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _currency_code(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("a currency is a three-letter code")
    code = value.strip().upper()
    try:
        currency(code)
    except MoneyError as exc:
        raise ValueError(str(exc)) from None
    return code


CurrencyCode = Annotated[str, BeforeValidator(_currency_code)]


class OpeningFundingParams(_Params):
    amount: StrictDecimal
    currency: CurrencyCode = "USD"


class TradeParams(_Params):
    """A controlled test trade: quantity at a price, with explicit costs in asset currency."""

    asset_id: uuid.UUID
    quantity: StrictDecimal
    price: StrictDecimal
    commission: StrictDecimal = Decimal(0)
    fees: StrictDecimal = Decimal(0)


class FeeParams(_Params):
    kind: FeeKind
    currency: CurrencyCode
    amount: StrictDecimal
    accrue: bool = False
    asset_id: uuid.UUID | None = None


class FinancingCostParams(_Params):
    kind: FinancingKind
    currency: CurrencyCode
    amount: StrictDecimal
    accrue: bool = True
    asset_id: uuid.UUID | None = None


class AccrualSettlementParams(_Params):
    payable: PayableKind
    currency: CurrencyCode
    amount: StrictDecimal


class FxConversionParams(_Params):
    sell_currency: CurrencyCode
    sell_amount: StrictDecimal
    buy_currency: CurrencyCode
    buy_amount: StrictDecimal


class PriceMark(_Params):
    """A fresh, provenance-carrying price for one open position (valuation checkpoint)."""

    asset_id: uuid.UUID
    price: StrictDecimal
    currency: CurrencyCode
    multiplier: StrictDecimal = Decimal(1)
    provenance: dict[str, Any] = Field(default_factory=dict)


class ValuationParams(_Params):
    as_of: datetime
    marks: tuple[PriceMark, ...] = ()
    # Foreign currencies whose translation is re-targeted (each needs a fresh spot rate).
    translate: tuple[CurrencyCode, ...] = ()


class ReversalParams(_Params):
    entry_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)


# ----------------------------------------------------------------------- context
@dataclass(frozen=True)
class RateQuote:
    """Functional-currency units per unit of ``currency``, from one ``fx_rates`` row."""

    currency: str
    rate: Decimal
    fx_rate_id: int
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class InstrumentInfo:
    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    currency: str
    quantity_increment: Decimal
    min_quantity: Decimal
    multiplier: Decimal
    spec_id: int | None


class TemplateContext(Protocol):
    @property
    def functional_currency(self) -> str: ...

    def spot_rate(self, currency: str) -> RateQuote:
        """The functional rate for ``currency`` at the entry's effective time (or raise)."""
        ...

    def instrument(self, asset_id: uuid.UUID) -> InstrumentInfo:
        """The asset's instrument specification at the entry's effective time (or raise)."""
        ...


@dataclass(frozen=True)
class StoredLine:
    """A posted line, as a reversal reads it."""

    account_code: str
    side: Side
    currency: str
    amount: Decimal
    functional_amount: Decimal
    rate_basis: RateBasis
    fx_rate: Decimal | None
    fx_rate_id: int | None
    asset_id: uuid.UUID | None
    memo: str | None


@dataclass(frozen=True)
class StoredEntry:
    entry_id: uuid.UUID
    entry_type: EntryType
    sequence: int
    lines: tuple[StoredLine, ...]
    movements: tuple[QuantityMovement, ...]
    reversed: bool
    # Assets touched by any entry posted after this one (makes a reversal unsafe).
    assets_touched_later: frozenset[uuid.UUID] = frozenset()


# ----------------------------------------------------------------------- builder
@dataclass
class _Value:
    functional: Decimal
    basis: RateBasis
    rate: Decimal | None
    rate_id: int | None = None


class _Builder:
    """Accumulates lines on a working copy of the state, so reliefs see earlier lines."""

    def __init__(
        self, state: LedgerState, ctx: TemplateContext, entry_type: EntryType, description: str
    ) -> None:
        self.before = state
        self.state = state.copy()
        self.ctx = ctx
        self.functional = ctx.functional_currency
        self.draft = EntryDraft(entry_type=entry_type, description=description)

    # -- values
    def spot(self, cur: str, amount: Decimal) -> _Value:
        if cur == self.functional:
            return _Value(amount, RateBasis.FUNCTIONAL, ONE)
        quote = self.ctx.spot_rate(cur)
        return _Value(
            round_functional(mul(amount, quote.rate), self.functional),
            RateBasis.SPOT,
            quote.rate,
            quote.fx_rate_id,
        )

    def carrying(self, code: str, cur: str, asset: uuid.UUID | None, amount: Decimal) -> _Value:
        functional = self.state.relief_functional(code, cur, asset, amount)
        return self.carried(cur, amount, functional)

    def carried(self, cur: str, amount: Decimal, functional: Decimal) -> _Value:
        if cur == self.functional:
            return _Value(amount, RateBasis.FUNCTIONAL, ONE)
        rate = round_to(div(functional, amount), RATE_SCALE) if amount != ZERO else None
        return _Value(functional, RateBasis.CARRYING if rate is not None else RateBasis.NONE, rate)

    # -- lines
    def line(
        self,
        code: str,
        side: Side,
        cur: str,
        amount: Decimal,
        value: _Value,
        asset: uuid.UUID | None = None,
        memo: str | None = None,
    ) -> None:
        drafted = DraftLine(
            account_code=code,
            side=side,
            currency=cur,
            amount=amount,
            functional_amount=value.functional,
            rate_basis=value.basis,
            fx_rate=value.rate,
            fx_rate_id=value.rate_id,
            asset_id=asset,
            memo=memo,
        )
        self.draft.lines.append(drafted)
        self.state.apply_line(drafted)

    def pair(
        self,
        debit: str,
        credit: str,
        cur: str,
        amount: Decimal,
        value: _Value,
        *,
        debit_asset: uuid.UUID | None = None,
        credit_asset: uuid.UUID | None = None,
        memo: str | None = None,
    ) -> None:
        """Two lines with the same amount and the same functional value: balanced alone."""
        self.line(debit, Side.DEBIT, cur, amount, value, debit_asset, memo)
        self.line(credit, Side.CREDIT, cur, amount, value, credit_asset, memo)

    def signed_pair(
        self,
        positive_debit: str,
        positive_credit: str,
        cur: str,
        amount: Decimal,
        functional: Decimal,
        value_basis: _Value,
        *,
        asset: uuid.UUID | None = None,
        memo: str | None = None,
    ) -> None:
        """A pair whose direction follows the sign of ``amount`` (zero posts nothing)."""
        if amount == ZERO and functional == ZERO:
            return
        if (amount < ZERO) or (amount == ZERO and functional < ZERO):
            positive_debit, positive_credit = positive_credit, positive_debit
        value = _Value(abs(functional), value_basis.basis, value_basis.rate, value_basis.rate_id)
        self.pair(
            positive_debit,
            positive_credit,
            cur,
            abs(amount),
            value,
            debit_asset=asset,
            credit_asset=asset,
            memo=memo,
        )

    def pay_cash(
        self, debit: str, cur: str, amount: Decimal, asset: uuid.UUID | None, memo: str
    ) -> None:
        if amount == ZERO:
            return
        self.pair(
            debit,
            CASH,
            cur,
            amount,
            self.carrying(CASH, cur, None, amount),
            debit_asset=asset,
            memo=memo,
        )

    def imbalance(self) -> Decimal:
        return sum((line.signed_functional for line in self.draft.lines), ZERO)

    def close_fx_difference(self, cur: str, asset: uuid.UUID | None, memo: str) -> Decimal:
        """Post the remaining functional imbalance of a foreign-currency entry as realized FX.

        Positive means debits exceed credits: the functional value received exceeds the
        historical value given up, a gain (credit 4200). A functional-currency entry has no
        FX difference by construction; finding one is a bug and raises.
        """
        difference = self.imbalance()
        if difference == ZERO:
            return ZERO
        if cur == self.functional:
            raise TemplateError(f"internal: unbalanced functional-currency entry ({difference})")
        side = Side.CREDIT if difference > ZERO else Side.DEBIT
        self.line(
            REALIZED_FX, side, cur, ZERO, _Value(abs(difference), RateBasis.NONE, None), asset, memo
        )
        return difference

    def move(self, movement: QuantityMovement) -> None:
        self.draft.movements.append(movement)
        self.state.apply_movement(movement)

    def done(self, **details: object) -> EntryDraft:
        self.draft.details.update(details)
        return self.draft


# ----------------------------------------------------------------------- helpers
def _amount(cur: str, value: Decimal, what: str, *, positive: bool = True) -> Decimal:
    spec = currency(cur)
    try:
        amount = spec.amount(value, what)
        return require_positive(amount, what) if positive else require_non_negative(amount, what)
    except MoneyError as exc:
        raise TemplateError(str(exc)) from None


@dataclass(frozen=True)
class _Trade:
    inst: InstrumentInfo
    cur: str
    quantity: Decimal
    price: Decimal
    notional: Decimal
    commission: Decimal
    fees: Decimal


def _trade(ctx: TemplateContext, p: TradeParams) -> _Trade:
    inst = ctx.instrument(p.asset_id)
    if inst.asset_class == "FOREX":
        raise TemplateError("spot forex is held as currency balances: use fx_conversion")
    if inst.multiplier != ONE:
        raise TemplateError("contract multipliers other than 1 are not supported yet")
    try:
        require_scale(p.quantity, QUANTITY_SCALE, "quantity")
        require_scale(p.price, PRICE_SCALE, "price")
        quantity = require_positive(p.quantity, "quantity")
        price = require_positive(p.price, "price")
    except MoneyError as exc:
        raise TemplateError(str(exc)) from None
    if quantity % inst.quantity_increment != ZERO:
        raise TemplateError(
            f"quantity {quantity} is not a multiple of the lot size {inst.quantity_increment} "
            f"for {inst.symbol}"
        )
    if quantity < inst.min_quantity:
        raise TemplateError(f"quantity {quantity} is below the minimum {inst.min_quantity}")
    cur = inst.currency
    notional = currency(cur).round(mul(quantity, price, inst.multiplier))
    if notional <= ZERO:
        raise TemplateError("the trade's value rounds to zero in its currency")
    commission = _amount(cur, p.commission, "commission", positive=False)
    fees = _amount(cur, p.fees, "fees", positive=False)
    return _Trade(inst, cur, quantity, price, notional, commission, fees)


def _costs(b: _Builder, t: _Trade) -> None:
    b.pay_cash(COMMISSIONS, t.cur, t.commission, t.inst.asset_id, "commission")
    b.pay_cash(FEES, t.cur, t.fees, t.inst.asset_id, "fees")


def _share(
    amount: Decimal, functional: Decimal, part: Decimal, whole: Decimal, cur: str, func_cur: str
) -> tuple[Decimal, Decimal]:
    """The ``part / whole`` share of a balance: (amount, functional value).

    The amount is rounded to the currency's minor units; the functional value to 8 places,
    except in the functional currency where it equals the amount. The whole balance is
    always relieved exactly.
    """
    share = proportion(amount, part, whole, currency(cur).minor_units)
    if cur == func_cur:
        return share, share
    return share, proportion(functional, part, whole, functional_places(func_cur))


def _relieve(pos: Position, quantity: Decimal, func_cur: str) -> tuple[Decimal, Decimal]:
    """Average-cost relief of ``quantity`` from a position: (cost, functional cost)."""
    return _share(
        pos.cost, pos.functional_cost, quantity, abs(pos.quantity), pos.currency, func_cur
    )


def _current(state: LedgerState, asset_id: uuid.UUID, cur: str) -> Position:
    found = state.positions.get(asset_id)
    if found is None:
        return Position(asset_id=asset_id, currency=cur)
    if found.quantity != ZERO and found.currency != cur:
        raise TemplateError("the instrument's currency differs from the open position's")
    return found


def _trade_details(t: _Trade) -> dict[str, object]:
    return {
        "symbol": t.inst.symbol,
        "currency": t.cur,
        "quantity": str(t.quantity),
        "price": str(t.price),
        "notional": str(t.notional),
        "commission": str(t.commission),
        "fees": str(t.fees),
        "instrument_spec_id": t.inst.spec_id,
    }


# ---------------------------------------------------------------------- templates
def opening_funding(
    state: LedgerState, ctx: TemplateContext, p: OpeningFundingParams
) -> EntryDraft:
    """Dr 1000 Cash / Cr 3000 Contributed capital, in the functional currency, once."""
    if p.currency != ctx.functional_currency:
        raise TemplateError("opening funding is in the account's functional currency")
    if state.balances:
        raise TemplateError("opening funding must be the account's first entry")
    amount = _amount(p.currency, p.amount, "opening funding")
    b = _Builder(state, ctx, EntryType.OPENING_FUNDING, "Simulated opening funding")
    b.pair(
        CASH,
        CONTRIBUTED_CAPITAL,
        p.currency,
        amount,
        b.spot(p.currency, amount),
        memo="simulated capital, not a return",
    )
    return b.done(amount=str(amount), currency=p.currency)


def long_purchase(state: LedgerState, ctx: TemplateContext, p: TradeParams) -> EntryDraft:
    t = _trade(ctx, p)
    pos = _current(state, t.inst.asset_id, t.cur)
    if pos.quantity < ZERO:
        raise TemplateError("the asset is held short: a purchase must be posted as short_cover")
    b = _Builder(
        state, ctx, EntryType.LONG_PURCHASE, f"Buy {t.quantity} {t.inst.symbol} @ {t.price}"
    )
    value = b.carrying(CASH, t.cur, None, t.notional)
    b.pair(
        LONG_COST, CASH, t.cur, t.notional, value, debit_asset=t.inst.asset_id, memo="cost basis"
    )
    b.move(
        QuantityMovement(
            asset_id=t.inst.asset_id,
            movement_type=MovementType.LONG_INCREASE,
            currency=t.cur,
            quantity_delta=t.quantity,
            quantity_after=pos.quantity + t.quantity,
            cost_delta=t.notional,
            cost_after=pos.cost + t.notional,
            functional_cost_delta=value.functional,
            functional_cost_after=pos.functional_cost + value.functional,
            price=t.price,
            realized_pnl=ZERO,
            realized_pnl_functional=ZERO,
        )
    )
    _costs(b, t)
    return b.done(**_trade_details(t))


def long_sale(state: LedgerState, ctx: TemplateContext, p: TradeParams) -> EntryDraft:
    t = _trade(ctx, p)
    asset, cur = t.inst.asset_id, t.cur
    pos = _current(state, asset, cur)
    if pos.quantity <= ZERO:
        raise TemplateError("no long position to sell (short sales use short_open)")
    if t.quantity > pos.quantity:
        raise TemplateError(
            f"cannot sell {t.quantity}: {pos.quantity} held (close, then open a short separately)"
        )
    cost_relieved, fcost_relieved = _relieve(pos, t.quantity, ctx.functional_currency)
    if cost_relieved == ZERO:
        raise TemplateError("the cost relieved rounds to zero; the quantity is too small")
    adj_amount, adj_functional = (
        state.balance(LONG_VALUATION, cur, asset).amount,
        state.balance(LONG_VALUATION, cur, asset).functional,
    )
    adj_released, adj_freleased = _share(
        adj_amount, adj_functional, t.quantity, pos.quantity, cur, ctx.functional_currency
    )

    b = _Builder(state, ctx, EntryType.LONG_SALE, f"Sell {t.quantity} {t.inst.symbol} @ {t.price}")
    proceeds = b.spot(cur, t.notional)
    b.line(CASH, Side.DEBIT, cur, t.notional, proceeds, memo="sale proceeds")
    b.line(
        LONG_COST,
        Side.CREDIT,
        cur,
        cost_relieved,
        b.carried(cur, cost_relieved, fcost_relieved),
        asset,
        "cost relieved (average cost)",
    )
    realized = t.notional - cost_relieved
    realized_value = b.spot(cur, abs(realized)) if realized != ZERO else None
    if realized_value is not None:
        b.line(
            REALIZED_PNL,
            Side.CREDIT if realized > ZERO else Side.DEBIT,
            cur,
            abs(realized),
            realized_value,
            asset,
            "proceeds minus cost",
        )
    fx = b.close_fx_difference(cur, asset, "FX on the cost relieved")
    # Release the unrealized valuation of the quantity sold, so 4100 describes open positions.
    if adj_released != ZERO:
        b.signed_pair(
            UNREALIZED_PNL,
            LONG_VALUATION,
            cur,
            adj_released,
            adj_freleased,
            b.carried(cur, abs(adj_released), abs(adj_freleased)),
            asset=asset,
            memo="release unrealized valuation of the quantity sold",
        )
    elif adj_freleased != ZERO:
        raise TemplateError("the valuation adjustment released rounds to zero; quantity too small")
    realized_functional = ZERO
    if realized_value is not None:
        realized_functional = (
            realized_value.functional if realized > ZERO else -realized_value.functional
        )
    b.move(
        QuantityMovement(
            asset_id=asset,
            movement_type=MovementType.LONG_DECREASE,
            currency=cur,
            quantity_delta=-t.quantity,
            quantity_after=pos.quantity - t.quantity,
            cost_delta=-cost_relieved,
            cost_after=pos.cost - cost_relieved,
            functional_cost_delta=-fcost_relieved,
            functional_cost_after=pos.functional_cost - fcost_relieved,
            price=t.price,
            realized_pnl=realized,
            realized_pnl_functional=realized_functional,
        )
    )
    _costs(b, t)
    return b.done(
        **_trade_details(t),
        cost_relieved=str(cost_relieved),
        realized=str(realized),
        realized_fx_functional=str(fx),
        valuation_released=str(adj_released),
    )


def short_open(state: LedgerState, ctx: TemplateContext, p: TradeParams) -> EntryDraft:
    """Proceeds go to restricted cash (1010), not to available cash; liability 2100."""
    t = _trade(ctx, p)
    asset, cur = t.inst.asset_id, t.cur
    pos = _current(state, asset, cur)
    if pos.quantity > ZERO:
        raise TemplateError("the asset is held long: a sale must be posted as long_sale")
    b = _Builder(
        state, ctx, EntryType.SHORT_OPEN, f"Sell short {t.quantity} {t.inst.symbol} @ {t.price}"
    )
    value = b.spot(cur, t.notional)
    b.pair(
        RESTRICTED_CASH,
        SHORT_PROCEEDS,
        cur,
        t.notional,
        value,
        debit_asset=asset,
        credit_asset=asset,
        memo="short-sale proceeds held as collateral",
    )
    b.move(
        QuantityMovement(
            asset_id=asset,
            movement_type=MovementType.SHORT_INCREASE,
            currency=cur,
            quantity_delta=-t.quantity,
            quantity_after=pos.quantity - t.quantity,
            cost_delta=t.notional,
            cost_after=pos.cost + t.notional,
            functional_cost_delta=value.functional,
            functional_cost_after=pos.functional_cost + value.functional,
            price=t.price,
            realized_pnl=ZERO,
            realized_pnl_functional=ZERO,
        )
    )
    _costs(b, t)
    return b.done(**_trade_details(t))


def short_cover(state: LedgerState, ctx: TemplateContext, p: TradeParams) -> EntryDraft:
    t = _trade(ctx, p)
    asset, cur = t.inst.asset_id, t.cur
    pos = _current(state, asset, cur)
    if pos.quantity >= ZERO:
        raise TemplateError("no short position to cover (purchases use long_purchase)")
    held = -pos.quantity
    if t.quantity > held:
        raise TemplateError(f"cannot cover {t.quantity}: {held} short")
    proceeds_relieved, fproceeds_relieved = _relieve(pos, t.quantity, ctx.functional_currency)
    if proceeds_relieved == ZERO:
        raise TemplateError("the proceeds relieved round to zero; the quantity is too small")
    adj = state.balance(SHORT_VALUATION, cur, asset)
    adj_released, adj_freleased = _share(
        adj.amount, adj.functional, t.quantity, held, cur, ctx.functional_currency
    )

    b = _Builder(
        state, ctx, EntryType.SHORT_COVER, f"Cover {t.quantity} {t.inst.symbol} @ {t.price}"
    )
    carried = b.carried(cur, proceeds_relieved, fproceeds_relieved)
    b.pair(
        CASH,
        RESTRICTED_CASH,
        cur,
        proceeds_relieved,
        carried,
        credit_asset=asset,
        memo="collateral released",
    )
    b.line(
        SHORT_PROCEEDS,
        Side.DEBIT,
        cur,
        proceeds_relieved,
        carried,
        asset,
        "liability extinguished at carrying proceeds",
    )
    b.line(
        CASH,
        Side.CREDIT,
        cur,
        t.notional,
        b.carrying(CASH, cur, None, t.notional),
        memo="buy-back cost",
    )
    realized = proceeds_relieved - t.notional
    realized_value = b.spot(cur, abs(realized)) if realized != ZERO else None
    if realized_value is not None:
        b.line(
            REALIZED_PNL,
            Side.CREDIT if realized > ZERO else Side.DEBIT,
            cur,
            abs(realized),
            realized_value,
            asset,
            "carrying proceeds minus buy-back cost",
        )
    fx = b.close_fx_difference(cur, asset, "FX on the proceeds relieved")
    # The short adjunct is a liability: a credit balance (negative here) is a recognized loss.
    if adj_released != ZERO:
        b.signed_pair(
            UNREALIZED_PNL,
            SHORT_VALUATION,
            cur,
            adj_released,
            adj_freleased,
            b.carried(cur, abs(adj_released), abs(adj_freleased)),
            asset=asset,
            memo="release unrealized valuation of the quantity covered",
        )
    elif adj_freleased != ZERO:
        raise TemplateError("the valuation adjustment released rounds to zero; quantity too small")
    realized_functional = ZERO
    if realized_value is not None:
        realized_functional = (
            realized_value.functional if realized > ZERO else -realized_value.functional
        )
    b.move(
        QuantityMovement(
            asset_id=asset,
            movement_type=MovementType.SHORT_DECREASE,
            currency=cur,
            quantity_delta=t.quantity,
            quantity_after=pos.quantity + t.quantity,
            cost_delta=-proceeds_relieved,
            cost_after=pos.cost - proceeds_relieved,
            functional_cost_delta=-fproceeds_relieved,
            functional_cost_after=pos.functional_cost - fproceeds_relieved,
            price=t.price,
            realized_pnl=realized,
            realized_pnl_functional=realized_functional,
        )
    )
    _costs(b, t)
    return b.done(
        **_trade_details(t),
        proceeds_relieved=str(proceeds_relieved),
        realized=str(realized),
        realized_fx_functional=str(fx),
        valuation_released=str(adj_released),
    )


def fee(state: LedgerState, ctx: TemplateContext, p: FeeParams) -> EntryDraft:
    amount = _amount(p.currency, p.amount, "fee")
    code = COMMISSIONS if p.kind is FeeKind.COMMISSION else FEES
    b = _Builder(state, ctx, EntryType.FEE, f"{p.kind.value.title()} {amount} {p.currency}")
    if p.accrue:
        b.pair(
            code,
            ACCRUED_FEES,
            p.currency,
            amount,
            b.spot(p.currency, amount),
            debit_asset=p.asset_id,
            memo="accrued, unpaid",
        )
    else:
        b.pay_cash(code, p.currency, amount, p.asset_id, "paid")
    return b.done(kind=p.kind.value, accrued=p.accrue)


def financing_cost(state: LedgerState, ctx: TemplateContext, p: FinancingCostParams) -> EntryDraft:
    amount = _amount(p.currency, p.amount, "financing cost")
    code = BORROW_COSTS if p.kind is FinancingKind.BORROW else FUNDING_COSTS
    b = _Builder(
        state, ctx, EntryType.FINANCING_COST, f"{p.kind.value.title()} cost {amount} {p.currency}"
    )
    if p.accrue:
        b.pair(
            code,
            ACCRUED_FINANCING,
            p.currency,
            amount,
            b.spot(p.currency, amount),
            debit_asset=p.asset_id,
            memo="accrued, unpaid",
        )
    else:
        b.pay_cash(code, p.currency, amount, p.asset_id, "paid")
    return b.done(kind=p.kind.value, accrued=p.accrue)


def accrual_settlement(
    state: LedgerState, ctx: TemplateContext, p: AccrualSettlementParams
) -> EntryDraft:
    amount = _amount(p.currency, p.amount, "settlement")
    code = ACCRUED_FEES if p.payable is PayableKind.FEES else ACCRUED_FINANCING
    b = _Builder(
        state,
        ctx,
        EntryType.ACCRUAL_SETTLEMENT,
        f"Pay accrued {p.payable.value.lower()} {amount} {p.currency}",
    )
    b.line(
        code,
        Side.DEBIT,
        p.currency,
        amount,
        b.carrying(code, p.currency, None, amount),
        memo="payable settled",
    )
    b.line(
        CASH,
        Side.CREDIT,
        p.currency,
        amount,
        b.carrying(CASH, p.currency, None, amount),
        memo="paid",
    )
    fx = b.close_fx_difference(p.currency, None, "FX between accrual and payment")
    return b.done(payable=p.payable.value, realized_fx_functional=str(fx))


def fx_conversion(state: LedgerState, ctx: TemplateContext, p: FxConversionParams) -> EntryDraft:
    """Sell one currency for another through the FX clearing account (each currency balances).

    The bought currency is carried at the functional value of the exchange: the
    functional-currency leg when one side is the functional currency, otherwise the bought
    amount at a spot rate. The sold currency is relieved at its average carrying value; the
    difference is realized FX, which also brings the clearing account's functional value
    back to zero. The sold currency's translation adjustment is released pro rata.
    """
    if p.sell_currency == p.buy_currency:
        raise TemplateError("a conversion needs two different currencies")
    f = ctx.functional_currency
    sell = _amount(p.sell_currency, p.sell_amount, "sell amount")
    buy = _amount(p.buy_currency, p.buy_amount, "buy amount")
    b = _Builder(
        state,
        ctx,
        EntryType.FX_CONVERSION,
        f"Convert {sell} {p.sell_currency} to {buy} {p.buy_currency}",
    )

    released = ZERO
    if p.sell_currency != f:
        net, _ = state.net_position(p.sell_currency)
        translation = state.translation(p.sell_currency)
        if translation != ZERO and net > ZERO:
            released = proportion(translation, min(sell, net), net, functional_places(f))
            if released != ZERO:
                side_debit = UNREALIZED_FX if released > ZERO else FX_TRANSLATION
                side_credit = FX_TRANSLATION if released > ZERO else UNREALIZED_FX
                none = _Value(abs(released), RateBasis.NONE, None)
                b.line(
                    side_debit,
                    Side.DEBIT,
                    p.sell_currency,
                    ZERO,
                    none,
                    memo="release translation of the currency sold",
                )
                b.line(
                    side_credit,
                    Side.CREDIT,
                    p.sell_currency,
                    ZERO,
                    none,
                    memo="release translation of the currency sold",
                )

    if p.buy_currency == f:
        bought = _Value(buy, RateBasis.FUNCTIONAL, ONE)
    elif p.sell_currency == f:
        bought = _Value(sell, RateBasis.TRANSACTION, round_to(div(sell, buy), RATE_SCALE))
    else:
        bought = b.spot(p.buy_currency, buy)
    b.pair(CASH, FX_CLEARING, p.buy_currency, buy, bought, memo="currency bought")
    given = b.carrying(CASH, p.sell_currency, None, sell)
    b.pair(FX_CLEARING, CASH, p.sell_currency, sell, given, memo="currency sold")
    gain = bought.functional - given.functional
    if gain != ZERO:
        b.signed_pair(
            FX_CLEARING,
            REALIZED_FX,
            f,
            gain,
            gain,
            _Value(ZERO, RateBasis.FUNCTIONAL, ONE),
            memo="realized FX: value received minus carrying value given up",
        )
    return b.done(
        sell_currency=p.sell_currency,
        sell_amount=str(sell),
        buy_currency=p.buy_currency,
        buy_amount=str(buy),
        executed_rate=str(round_to(div(buy, sell), RATE_SCALE)),
        executed_rate_meaning=f"{p.buy_currency} per {p.sell_currency}",
        realized_fx_functional=str(gain),
        translation_released=str(released),
    )


def valuation(state: LedgerState, ctx: TemplateContext, p: ValuationParams) -> EntryDraft:
    """Re-target valuation adjustments and FX translation to fresh prices and rates.

    For each marked long position the adjunct 1150 becomes market value - cost; for each
    marked short position the adjunct 2150 becomes buy-back value - carrying proceeds; the
    change goes to 4100. Then, per foreign currency to translate, the adjustment 1910 becomes
    net balance-sheet amount x spot rate - historical functional value, the change going to
    4300. Positions or currencies without a fresh input are not in ``p`` and keep their last
    valuation; the caller flags them.
    """
    b = _Builder(state, ctx, EntryType.VALUATION, f"Valuation checkpoint {p.as_of.isoformat()}")
    marked: list[dict[str, str]] = []
    for mark in p.marks:
        pos = state.position(mark.asset_id)
        if pos is None:
            raise TemplateError(f"no open position in {mark.asset_id} to value")
        if mark.currency != pos.currency:
            raise TemplateError("a price must be in the position's currency")
        spec = currency(pos.currency)
        value = spec.round(mul(abs(pos.quantity), mark.price, mark.multiplier))
        if pos.is_long:
            target = value - pos.cost
            delta = target - state.balance(LONG_VALUATION, pos.currency, mark.asset_id).amount
            debit, credit = LONG_VALUATION, UNREALIZED_PNL
        else:
            # Liability adjunct, credit-normal: a rise in the buy-back value is a loss.
            target = value - pos.cost
            delta = target + state.balance(SHORT_VALUATION, pos.currency, mark.asset_id).amount
            debit, credit = UNREALIZED_PNL, SHORT_VALUATION
        if delta != ZERO:
            rate = b.spot(pos.currency, abs(delta))
            b.signed_pair(
                debit,
                credit,
                pos.currency,
                delta,
                rate.functional if delta > ZERO else -rate.functional,
                rate,
                asset=mark.asset_id,
                memo="valuation adjustment",
            )
        marked.append(
            {
                "asset_id": str(mark.asset_id),
                "price": str(mark.price),
                "value": str(value),
                "adjustment_delta": str(delta),
            }
        )
    translated: list[dict[str, str]] = []
    for cur in p.translate:
        if cur == ctx.functional_currency:
            continue
        quote = ctx.spot_rate(cur)
        net, functional = b.state.net_position(cur)
        target = round_functional(mul(net, quote.rate), ctx.functional_currency) - functional
        delta = target - b.state.translation(cur)
        if delta != ZERO:
            spot = _Value(abs(delta), RateBasis.SPOT, quote.rate, quote.fx_rate_id)
            debit, credit = (
                (FX_TRANSLATION, UNREALIZED_FX) if delta > ZERO else (UNREALIZED_FX, FX_TRANSLATION)
            )
            b.line(debit, Side.DEBIT, cur, ZERO, spot, memo="FX translation")
            b.line(credit, Side.CREDIT, cur, ZERO, spot, memo="FX translation")
        translated.append(
            {
                "currency": cur,
                "rate": str(quote.rate),
                "net_amount": str(net),
                "translation_target": str(target),
                "delta": str(delta),
            }
        )
    return b.done(as_of=p.as_of.isoformat(), marks=marked, translations=translated)


def reversal(
    state: LedgerState, ctx: TemplateContext, original: StoredEntry, reason: str
) -> EntryDraft:
    """Mirror every line of ``original`` (same amounts and values, opposite sides)."""
    if original.entry_type in (EntryType.REVERSAL, EntryType.OPENING_FUNDING):
        raise TemplateError(f"{original.entry_type.value} entries cannot be reversed")
    if original.reversed:
        raise TemplateError("the entry has already been reversed")
    blocked = {m.asset_id for m in original.movements} & original.assets_touched_later
    if blocked:
        raise TemplateError(
            "later entries touch the same assets; reverse those first: "
            + ", ".join(sorted(str(a) for a in blocked))
        )
    b = _Builder(
        state, ctx, EntryType.REVERSAL, f"Reversal of entry #{original.sequence}: {reason}"
    )
    for line in original.lines:
        b.line(
            line.account_code,
            line.side.opposite,
            line.currency,
            line.amount,
            _Value(line.functional_amount, line.rate_basis, line.fx_rate, line.fx_rate_id),
            line.asset_id,
            line.memo,
        )
    for m in original.movements:
        pos = _current(b.state, m.asset_id, m.currency)
        b.move(
            QuantityMovement(
                asset_id=m.asset_id,
                movement_type=MovementType.REVERSAL,
                currency=m.currency,
                quantity_delta=-m.quantity_delta,
                quantity_after=pos.quantity - m.quantity_delta,
                cost_delta=-m.cost_delta,
                cost_after=pos.cost - m.cost_delta,
                functional_cost_delta=-m.functional_cost_delta,
                functional_cost_after=pos.functional_cost - m.functional_cost_delta,
                price=None,
                realized_pnl=-m.realized_pnl,
                realized_pnl_functional=-m.realized_pnl_functional,
            )
        )
    return b.done(reverses_sequence=original.sequence, reason=reason)


# ---------------------------------------------------------------------- registry
@dataclass(frozen=True)
class TemplateDef:
    code: str
    version: int
    entry_type: EntryType
    params: type[BaseModel]
    build: Callable[..., EntryDraft]
    description: str
    rules: Sequence[str] = field(default_factory=tuple)
    # Templates that move positions or currencies are controlled-test only in Phase 6A.
    controlled_test_only: bool = True

    def definition(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "version": self.version,
            "entry_type": self.entry_type.value,
            "description": self.description,
            "rules": list(self.rules),
            "params_schema": self.params.model_json_schema(),
        }

    def definition_sha256(self) -> str:
        canonical = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


TEMPLATES: dict[str, TemplateDef] = {
    t.code: t
    for t in (
        TemplateDef(
            "opening_funding",
            1,
            EntryType.OPENING_FUNDING,
            OpeningFundingParams,
            opening_funding,
            "Simulated opening funding of a paper account",
            ("Dr 1000 Cash / Cr 3000 Contributed capital, functional currency, first entry only",),
            controlled_test_only=False,
        ),
        TemplateDef(
            "long_purchase",
            1,
            EntryType.LONG_PURCHASE,
            TradeParams,
            long_purchase,
            "Buy quantity of an asset (opens or increases a long position)",
            (
                "Dr 1100 Long cost / Cr 1000 Cash at the cash's carrying value",
                "commission Dr 5000 / Cr 1000; fees Dr 5100 / Cr 1000",
                "quantity +q, cost +q x price (rounded to currency minor units)",
            ),
        ),
        TemplateDef(
            "long_sale",
            1,
            EntryType.LONG_SALE,
            TradeParams,
            long_sale,
            "Sell quantity of a long position",
            (
                "Dr 1000 Cash proceeds at spot / Cr 1100 cost relieved at average cost",
                "4000 realized = proceeds - cost relieved; FX difference to 4200",
                "release 1150 pro rata against 4100",
                "commission and fees as for a purchase",
            ),
        ),
        TemplateDef(
            "short_open",
            1,
            EntryType.SHORT_OPEN,
            TradeParams,
            short_open,
            "Sell borrowed quantity (opens or increases a short position)",
            (
                "Dr 1010 Restricted cash / Cr 2100 Short liability at proceeds",
                "proceeds are collateral, never available cash or equity",
                "commission and fees paid from 1000 Cash",
            ),
        ),
        TemplateDef(
            "short_cover",
            1,
            EntryType.SHORT_COVER,
            TradeParams,
            short_cover,
            "Buy back quantity of a short position",
            (
                "Dr 1000 / Cr 1010 collateral released; Dr 2100 at carrying proceeds",
                "Cr 1000 buy-back cost; 4000 realized = proceeds relieved - cost",
                "FX difference to 4200; release 2150 pro rata against 4100",
            ),
        ),
        TemplateDef(
            "fee",
            1,
            EntryType.FEE,
            FeeParams,
            fee,
            "A commission or fee, paid now or accrued",
            ("Dr 5000/5100 / Cr 1000 Cash (paid) or Cr 2200 Accrued fees (accrued)",),
        ),
        TemplateDef(
            "financing_cost",
            1,
            EntryType.FINANCING_COST,
            FinancingCostParams,
            financing_cost,
            "A borrow or funding cost, accrued or paid now",
            ("Dr 5200/5300 / Cr 2210 Accrued financing (accrued) or Cr 1000 (paid)",),
        ),
        TemplateDef(
            "accrual_settlement",
            1,
            EntryType.ACCRUAL_SETTLEMENT,
            AccrualSettlementParams,
            accrual_settlement,
            "Payment of an accrued cost",
            ("Dr 2200/2210 at carrying / Cr 1000 at carrying; FX difference to 4200",),
        ),
        TemplateDef(
            "fx_conversion",
            1,
            EntryType.FX_CONVERSION,
            FxConversionParams,
            fx_conversion,
            "Convert cash from one currency to another",
            (
                "Dr 1000 bought / Cr 1900; Dr 1900 / Cr 1000 sold at carrying value",
                "clearing difference to 4200 in the functional currency",
                "release the sold currency's 1910 translation pro rata against 4300",
            ),
        ),
        TemplateDef(
            "valuation",
            1,
            EntryType.VALUATION,
            ValuationParams,
            valuation,
            "Valuation checkpoint: mark positions and translate foreign balances",
            (
                "1150/2150 re-targeted to value - cost against 4100",
                "1910 re-targeted per currency to net amount x spot - functional, vs 4300",
            ),
            controlled_test_only=False,
        ),
        TemplateDef(
            "reversal",
            1,
            EntryType.REVERSAL,
            ReversalParams,
            reversal,
            "Reverse a posted entry with mirrored lines",
            (
                "every line mirrored: same amount and value, opposite side",
                "quantity movements negated; only when no later entry touches the assets",
            ),
            controlled_test_only=False,
        ),
    )
}

__all__ = [
    "TEMPLATES",
    "AccrualSettlementParams",
    "FeeParams",
    "FinancingCostParams",
    "FxConversionParams",
    "InstrumentInfo",
    "OpeningFundingParams",
    "PriceMark",
    "RateQuote",
    "ReversalParams",
    "StoredEntry",
    "StoredLine",
    "TemplateContext",
    "TemplateDef",
    "TemplateError",
    "TradeParams",
    "ValuationParams",
]
