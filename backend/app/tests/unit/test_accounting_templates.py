"""Deterministic financial test vectors for the posting templates (Phase 6A).

Every vector is posted to an in-memory ledger state: the draft must satisfy R1 (each
currency balances) and R2 (functional balances), and the resulting state must satisfy every
invariant. Expected figures are worked out by hand in the comments.

All prices, rates and instruments here are labelled test fixtures, not market data.
"""

from __future__ import annotations

import random
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from app.accounting import chart
from app.accounting.chart import CHART
from app.accounting.enums import AccountType, EntryType, FeeKind, FinancingKind, PayableKind, Side
from app.accounting.ledger import DraftLine, EntryDraft, EntryValidationError, RateBasis, validate
from app.accounting.money import CURRENCIES, MoneyError, currency, round_functional, to_decimal
from app.accounting.state import InsufficientBalanceError, LedgerState, invariant_violations
from app.accounting.templates import (
    TEMPLATES,
    AccrualSettlementParams,
    FeeParams,
    FinancingCostParams,
    FxConversionParams,
    InstrumentInfo,
    OpeningFundingParams,
    PriceMark,
    RateQuote,
    StoredEntry,
    StoredLine,
    TemplateError,
    TradeParams,
    ValuationParams,
    accrual_settlement,
    fee,
    financing_cost,
    fx_conversion,
    long_purchase,
    long_sale,
    opening_funding,
    reversal,
    short_cover,
    short_open,
    valuation,
)

T0 = datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
AAPL = uuid.UUID("00000000-0000-0000-0000-00000000a001")
XYZ = uuid.UUID("00000000-0000-0000-0000-00000000a002")
SAP = uuid.UUID("00000000-0000-0000-0000-00000000a003")
FRAC = uuid.UUID("00000000-0000-0000-0000-00000000a004")
EURUSD = uuid.UUID("00000000-0000-0000-0000-00000000a005")


def _inst(
    asset: uuid.UUID, symbol: str, cur: str = "USD", increment: str = "1", cls: str = "EQUITY"
):
    return InstrumentInfo(asset, symbol, cls, cur, D(increment), D(increment), D(1), 1)


@dataclass
class Ctx:
    """Test-fixture rates (functional USD per unit) and instruments."""

    rates: dict[str, D] = field(default_factory=lambda: {"EUR": D("1.0850")})
    functional_currency: str = "USD"
    instruments: dict[uuid.UUID, InstrumentInfo] = field(
        default_factory=lambda: {
            AAPL: _inst(AAPL, "AAPL"),
            XYZ: _inst(XYZ, "XYZ"),
            SAP: _inst(SAP, "SAP", "EUR"),
            FRAC: _inst(FRAC, "FRAC", increment="0.001"),
            EURUSD: _inst(EURUSD, "EUR/USD", cls="FOREX"),
        }
    )

    def spot_rate(self, cur: str) -> RateQuote:
        if cur not in self.rates:
            raise TemplateError(f"no rate for {cur}")
        return RateQuote(
            cur, self.rates[cur], 1000 + sorted(CURRENCIES).index(cur), T0, "TEST_FIXTURE"
        )

    def instrument(self, asset_id: uuid.UUID) -> InstrumentInfo:
        return self.instruments[asset_id]


def post(state: LedgerState, draft: EntryDraft) -> LedgerState:
    validate(draft, state.functional_currency)
    after = state.copy()
    for line in draft.lines:
        after.apply_line(line)
    for movement in draft.movements:
        after.apply_movement(movement)
    assert invariant_violations(after) == []
    return after


def funded(amount: str = "100000.00") -> LedgerState:
    return post(
        LedgerState("USD"),
        opening_funding(LedgerState("USD"), Ctx(), OpeningFundingParams(amount=D(amount))),
    )


def cash(state: LedgerState, cur: str = "USD") -> D:
    return state.balance(chart.CASH, cur).amount


def total(state: LedgerState, code: str, cur: str = "USD") -> D:
    """Normal-side amount of an account in a currency, summed over assets."""
    sign = 1 if CHART[code].normal_side is Side.DEBIT else -1
    return (
        sum((b.amount for (c, k, _), b in state.balances.items() if c == code and k == cur), D(0))
        * sign
    )


def ftotal(state: LedgerState, types: tuple[AccountType, ...]) -> D:
    out = D(0)
    for (code, _, _), bal in state.balances.items():
        if CHART[code].account_type in types:
            out += bal.functional
    return out


def equity(state: LedgerState) -> D:
    """Assets minus liabilities, functional (the accounting equation's left side)."""
    return ftotal(state, (AccountType.ASSET,)) + ftotal(state, (AccountType.LIABILITY,))


def equity_by_components(state: LedgerState) -> D:
    """Capital + income - expenses, functional (the right side)."""
    return -(
        ftotal(state, (AccountType.EQUITY, AccountType.INCOME))
        + ftotal(state, (AccountType.EXPENSE,))
    )


def buy(state, asset, qty, price, ctx=None, **costs):
    return post(
        state,
        long_purchase(
            state,
            ctx or Ctx(),
            TradeParams(asset_id=asset, quantity=D(qty), price=D(price), **costs),
        ),
    )


def sell(state, asset, qty, price, ctx=None, **costs):
    return post(
        state,
        long_sale(
            state,
            ctx or Ctx(),
            TradeParams(asset_id=asset, quantity=D(qty), price=D(price), **costs),
        ),
    )


def short(state, asset, qty, price, ctx=None, **costs):
    return post(
        state,
        short_open(
            state,
            ctx or Ctx(),
            TradeParams(asset_id=asset, quantity=D(qty), price=D(price), **costs),
        ),
    )


def cover(state, asset, qty, price, ctx=None, **costs):
    return post(
        state,
        short_cover(
            state,
            ctx or Ctx(),
            TradeParams(asset_id=asset, quantity=D(qty), price=D(price), **costs),
        ),
    )


def mark(state, prices: dict[uuid.UUID, str], ctx=None, translate=()):
    ctx = ctx or Ctx()
    marks = tuple(
        PriceMark(asset_id=a, price=D(p), currency=ctx.instruments[a].currency)
        for a, p in prices.items()
    )
    draft = valuation(
        state, ctx, ValuationParams(as_of=T0, marks=marks, translate=tuple(translate))
    )
    return post(state, draft) if draft.lines else state


# ------------------------------------------------------------------ opening funding
def test_opening_funding_is_contributed_capital_not_a_return():
    state = funded()
    assert cash(state) == D("100000.00")
    assert total(state, chart.CONTRIBUTED_CAPITAL) == D("100000.00")
    assert equity(state) == equity_by_components(state) == D("100000.00")
    assert total(state, chart.REALIZED_PNL) == 0


def test_opening_funding_only_first_and_functional_currency():
    state = funded()
    with pytest.raises(TemplateError, match="first entry"):
        opening_funding(state, Ctx(), OpeningFundingParams(amount=D("1.00")))
    with pytest.raises(TemplateError, match="functional currency"):
        opening_funding(
            LedgerState("USD"), Ctx(), OpeningFundingParams(amount=D("1"), currency="EUR")
        )
    with pytest.raises(TemplateError, match="positive"):
        opening_funding(LedgerState("USD"), Ctx(), OpeningFundingParams(amount=D("0")))


# ------------------------------------------------------------------ long positions
def test_long_round_trip_vector():
    s = funded()
    # Buy 100 @ 150.25 = 15,025.00; commission 1.00 and fees 0.05 are expensed.
    s = buy(s, AAPL, "100", "150.25", commission=D("1.00"), fees=D("0.05"))
    assert cash(s) == D("84973.95")
    assert s.balance(chart.LONG_COST, "USD", AAPL).amount == D("15025.00")
    assert s.positions[AAPL].quantity == D(100)
    assert total(s, chart.COMMISSIONS) == D("1.00")
    assert total(s, chart.FEES) == D("0.05")
    assert equity(s) == D("99998.95")

    # Mark at 160.10: value 16,010.00, adjustment +985.00 (unrealized).
    s = mark(s, {AAPL: "160.10"})
    assert s.balance(chart.LONG_VALUATION, "USD", AAPL).amount == D("985.00")
    assert total(s, chart.UNREALIZED_PNL) == D("985.00")
    assert equity(s) == D("100983.95")

    # Sell 40 @ 158.00: proceeds 6,320.00; cost relieved 15,025 x 40/100 = 6,010.00;
    # realized +310.00; 40% of the adjustment (394.00) released from unrealized.
    s = sell(s, AAPL, "40", "158.00", commission=D("1.00"))
    assert total(s, chart.REALIZED_PNL) == D("310.00")
    assert s.balance(chart.LONG_VALUATION, "USD", AAPL).amount == D("591.00")
    assert total(s, chart.UNREALIZED_PNL) == D("591.00")
    assert cash(s) == D("91292.95")
    assert equity(s) == equity_by_components(s) == D("100898.95")
    # 100,000 + 310 realized + 591 unrealized - 2.05 costs
    assert equity(s) == D("100000") + D("310") + D("591") - D("2.05")

    # Sell the remaining 60 @ 155.00: realized +285.00; position and adjustment flat.
    s = sell(s, AAPL, "60", "155.00")
    assert s.position(AAPL) is None
    assert s.balance(chart.LONG_COST, "USD", AAPL).amount == 0
    assert s.balance(chart.LONG_VALUATION, "USD", AAPL).amount == 0
    assert total(s, chart.UNREALIZED_PNL) == 0
    assert total(s, chart.REALIZED_PNL) == D("595.00")
    assert equity(s) == cash(s) == D("100592.95")


def test_average_cost_relief_never_leaves_a_residue():
    s = funded()
    s = buy(s, AAPL, "3", "10.00")
    s = buy(s, AAPL, "1", "10.01")  # cost 40.01 for 4 => 10.0025 per share
    s = sell(s, AAPL, "1", "11.00")  # relief 10.0025 -> 10.00 (half-even); realized 1.00
    assert s.positions[AAPL].cost == D("30.01")
    assert total(s, chart.REALIZED_PNL) == D("1.00")
    s = sell(s, AAPL, "3", "11.00")  # the last 3 relieve exactly the remaining 30.01
    assert s.positions[AAPL].cost == 0
    assert total(s, chart.REALIZED_PNL) == D("3.99")
    assert equity(s) == D("100000") + D("3.99")


def test_purchase_cannot_overdraw_cash():
    s = funded("1000.00")
    with pytest.raises(InsufficientBalanceError):
        long_purchase(s, Ctx(), TradeParams(asset_id=AAPL, quantity=D("10"), price=D("100.01")))


def test_sale_limits_and_direction():
    s = buy(funded(), AAPL, "5", "10")
    with pytest.raises(TemplateError, match="cannot sell 6"):
        long_sale(s, Ctx(), TradeParams(asset_id=AAPL, quantity=D("6"), price=D("10")))
    with pytest.raises(TemplateError, match="held long"):
        short_open(s, Ctx(), TradeParams(asset_id=AAPL, quantity=D("1"), price=D("10")))
    with pytest.raises(TemplateError, match="no long position"):
        long_sale(funded(), Ctx(), TradeParams(asset_id=AAPL, quantity=D("1"), price=D("10")))


def test_lot_size_and_precision_are_enforced_not_rounded():
    s = funded()
    with pytest.raises(TemplateError, match="lot size"):
        long_purchase(s, Ctx(), TradeParams(asset_id=AAPL, quantity=D("0.5"), price=D("10")))
    s = buy(s, FRAC, "0.125", "80.00")  # fractional instrument, increment 0.001
    assert s.balance(chart.LONG_COST, "USD", FRAC).amount == D("10.00")
    with pytest.raises(TemplateError, match="lot size"):
        long_purchase(s, Ctx(), TradeParams(asset_id=FRAC, quantity=D("0.0005"), price=D("80")))
    with pytest.raises(TemplateError, match="decimal places"):
        long_purchase(
            s,
            Ctx(),
            TradeParams(asset_id=AAPL, quantity=D("1"), price=D("10"), commission=D("0.001")),
        )
    with pytest.raises(TemplateError, match="use fx_conversion"):
        long_purchase(s, Ctx(), TradeParams(asset_id=EURUSD, quantity=D("1"), price=D("1.08")))


def test_notional_rounds_half_even_to_cents():
    s = buy(funded(), FRAC, "0.001", "12.345")  # 0.012345 -> 0.01
    assert s.positions[FRAC].cost == D("0.01")
    s = buy(s, FRAC, "0.001", "15.00")  # 0.015 -> 0.02 (half-even: 1 is odd)
    assert s.positions[FRAC].cost == D("0.03")
    with pytest.raises(TemplateError, match="rounds to zero"):
        long_purchase(s, Ctx(), TradeParams(asset_id=FRAC, quantity=D("0.001"), price=D("1.00")))


# ------------------------------------------------------------------ short positions
def test_short_round_trip_vector():
    s = funded()
    # Short 50 @ 20.00: proceeds 1,000.00 are restricted collateral, equity unchanged
    # except the commission.
    s = short(s, XYZ, "50", "20.00", commission=D("1.00"))
    assert s.balance(chart.RESTRICTED_CASH, "USD", XYZ).amount == D("1000.00")
    assert total(s, chart.SHORT_PROCEEDS) == D("1000.00")
    assert cash(s) == D("99999.00")  # proceeds are not available cash
    assert equity(s) == D("99999.00")  # nor equity
    assert s.positions[XYZ].quantity == D(-50)

    # Mark at 22.00: buy-back value 1,100.00 => liability adjustment 100.00 (loss).
    s = mark(s, {XYZ: "22.00"})
    assert total(s, chart.SHORT_VALUATION) == D("100.00")
    assert total(s, chart.UNREALIZED_PNL) == D("-100.00")
    assert equity(s) == D("99899.00")

    # Cover 20 @ 21.00: proceeds relieved 400.00, cost 420.00 => realized -20.00; 40% of the
    # adjustment (40.00) released.
    s = cover(s, XYZ, "20", "21.00")
    assert total(s, chart.REALIZED_PNL) == D("-20.00")
    assert total(s, chart.SHORT_VALUATION) == D("60.00")
    assert s.balance(chart.RESTRICTED_CASH, "USD", XYZ).amount == D("600.00")
    assert cash(s) == D("99979.00")
    assert equity(s) == equity_by_components(s) == D("99919.00")

    # Cover the rest @ 19.00: realized +30.00 on 600.00 of proceeds; flat.
    s = cover(s, XYZ, "30", "19.00")
    assert s.position(XYZ) is None
    assert total(s, chart.SHORT_VALUATION) == 0
    assert total(s, chart.RESTRICTED_CASH) == 0
    assert total(s, chart.REALIZED_PNL) == D("10.00")
    assert equity(s) == cash(s) == D("100009.00")


def test_short_gain_marked_below_proceeds():
    s = short(funded(), XYZ, "10", "50.00")
    s = mark(s, {XYZ: "45.00"})  # liability 450 < proceeds 500: adjunct debit 50 (gain)
    assert total(s, chart.SHORT_VALUATION) == D("-50.00")
    assert total(s, chart.UNREALIZED_PNL) == D("50.00")
    assert equity(s) == D("100050.00")
    s = cover(s, XYZ, "10", "45.00")
    assert total(s, chart.REALIZED_PNL) == D("50.00")
    assert total(s, chart.UNREALIZED_PNL) == 0
    assert equity(s) == D("100050.00")


def test_cover_limits():
    s = short(funded(), XYZ, "5", "10")
    with pytest.raises(TemplateError, match="cannot cover 6"):
        short_cover(s, Ctx(), TradeParams(asset_id=XYZ, quantity=D("6"), price=D("10")))
    with pytest.raises(TemplateError, match="short_cover"):
        long_purchase(s, Ctx(), TradeParams(asset_id=XYZ, quantity=D("1"), price=D("10")))


# ------------------------------------------------------------------ costs
def test_fees_paid_and_accrued_financing():
    s = funded()
    s = post(s, fee(s, Ctx(), FeeParams(kind=FeeKind.FEE, currency="USD", amount=D("0.35"))))
    s = post(
        s,
        fee(
            s, Ctx(), FeeParams(kind=FeeKind.COMMISSION, currency="USD", amount=D("2"), accrue=True)
        ),
    )
    s = post(
        s,
        financing_cost(
            s,
            Ctx(),
            FinancingCostParams(
                kind=FinancingKind.BORROW, currency="USD", amount=D("2.50"), asset_id=XYZ
            ),
        ),
    )
    s = post(
        s,
        financing_cost(
            s,
            Ctx(),
            FinancingCostParams(kind=FinancingKind.FUNDING, currency="USD", amount=D("1.25")),
        ),
    )
    assert cash(s) == D("99999.65")
    assert total(s, chart.ACCRUED_FEES) == D("2.00")
    assert total(s, chart.ACCRUED_FINANCING) == D("3.75")
    assert equity(s) == D("100000") - D("0.35") - D("2") - D("2.50") - D("1.25")
    s = post(
        s,
        accrual_settlement(
            s,
            Ctx(),
            AccrualSettlementParams(
                payable=PayableKind.FINANCING, currency="USD", amount=D("3.75")
            ),
        ),
    )
    assert total(s, chart.ACCRUED_FINANCING) == 0
    assert cash(s) == D("99995.90")
    assert equity(s) == D("99993.90")  # settlement moves cash, not equity
    with pytest.raises(InsufficientBalanceError):
        accrual_settlement(
            s,
            Ctx(),
            AccrualSettlementParams(payable=PayableKind.FEES, currency="USD", amount=D("2.01")),
        )


# ------------------------------------------------------------------ multi-currency
def test_fx_conversion_translation_and_realized_fx_vector():
    s = funded()
    # Buy EUR 10,000.00 for USD 10,850.00: EUR carried at its cost, nothing realized.
    s = post(
        s,
        fx_conversion(
            s,
            Ctx(),
            FxConversionParams(
                sell_currency="USD",
                sell_amount=D("10850.00"),
                buy_currency="EUR",
                buy_amount=D("10000.00"),
            ),
        ),
    )
    assert cash(s, "EUR") == D("10000.00")
    assert s.balance(chart.CASH, "EUR").functional == D("10850.00")
    assert cash(s) == D("89150.00")
    assert equity(s) == D("100000.00")

    # Translate at 1.1000: 10,000 x 1.1 - 10,850 = +150.00 unrealized FX (1910 / 4300).
    ctx = Ctx(rates={"EUR": D("1.1000")})
    s = mark(s, {}, ctx, translate=["EUR"])
    assert s.translation("EUR") == D("150.00")
    assert equity(s) == D("100150.00")

    # Sell EUR 4,000.00 for USD 4,420.00: carrying 10,850 x 0.4 = 4,340.00 => realized FX
    # +80.00; 40% of the translation (60.00) released.
    s = post(
        s,
        fx_conversion(
            s,
            ctx,
            FxConversionParams(
                sell_currency="EUR",
                sell_amount=D("4000.00"),
                buy_currency="USD",
                buy_amount=D("4420.00"),
            ),
        ),
    )
    assert sum(b.functional for (c, _, _), b in s.balances.items() if c == chart.REALIZED_FX) == D(
        "-80.00"
    )
    assert s.translation("EUR") == D("90.00")
    assert cash(s) == D("93570.00")
    assert cash(s, "EUR") == D("6000.00")
    assert equity(s) == equity_by_components(s) == D("100170.00")  # 100,000 + 80 + 90
    # The clearing account's functional value is zero; per currency it holds the
    # outstanding exchange (EUR credit 6,000 / USD debit 6,510).
    clearing = [b for (c, _, _), b in s.balances.items() if c == chart.FX_CLEARING]
    assert sum(b.functional for b in clearing) == 0
    assert s.balance(chart.FX_CLEARING, "EUR").amount == D("-6000.00")
    assert s.balance(chart.FX_CLEARING, "USD").amount == D("6510.00")

    # Sell the remaining EUR: translation fully released, clearing empty in every currency.
    s = post(
        s,
        fx_conversion(
            s,
            ctx,
            FxConversionParams(
                sell_currency="EUR",
                sell_amount=D("6000.00"),
                buy_currency="USD",
                buy_amount=D("6600.00"),
            ),
        ),
    )
    assert s.translation("EUR") == 0
    assert all(
        b.amount == 0 and b.functional == 0
        for (c, _, _), b in s.balances.items()
        if c == chart.FX_CLEARING
    )
    assert equity(s) == cash(s) == D("100170.00")  # 80 + 90 realized in total


def test_foreign_currency_stock_vector():
    s = funded()
    s = post(
        s,
        fx_conversion(
            s,
            Ctx(),
            FxConversionParams(
                sell_currency="USD",
                sell_amount=D("2170.00"),
                buy_currency="EUR",
                buy_amount=D("2000.00"),
            ),
        ),
    )
    # Buy 10 SAP @ EUR 120.00 with euros carried at 1.085: cost EUR 1,200.00 = USD 1,302.00.
    s = buy(s, SAP, "10", "120.00")
    assert s.positions[SAP].cost == D("1200.00")
    assert s.positions[SAP].functional_cost == D("1302.00000000")
    # Rate 1.1000, price EUR 130: value EUR 1,300 (unrealized EUR 100 at 1.1 = USD 110) and
    # translation of the net EUR position: (800 + 1,300) x 1.1 - (868 + 1,302 + 110) = 30.
    ctx = Ctx(rates={"EUR": D("1.1000")})
    s = mark(s, {SAP: "130.00"}, ctx, translate=["EUR"])
    assert total(s, chart.UNREALIZED_PNL, "EUR") == D("100.00")
    assert s.translation("EUR") == D("30.00")
    assert equity(s) == D("100140.00")  # 2,100 EUR x 1.1 = 2,310 + 97,830 USD
    # Sell 10 @ EUR 125 at 1.1: proceeds EUR 1,250 = USD 1,375; realized EUR 50 = USD 55;
    # FX on the cost relieved: 1,200 x 1.1 - 1,302 = USD 18 (realized FX, 4200).
    s = sell(s, SAP, "10", "125.00", ctx)
    assert total(s, chart.REALIZED_PNL, "EUR") == D("50.00")
    realized_fx = -sum(
        b.functional for (c, _, _), b in s.balances.items() if c == chart.REALIZED_FX
    )
    assert realized_fx == D("18.00")
    assert equity(s) == equity_by_components(s)


def test_cross_currency_conversion_uses_a_spot_rate():
    ctx = Ctx(rates={"EUR": D("1.10"), "GBP": D("1.30")})
    s = funded()
    s = post(
        s,
        fx_conversion(
            s,
            ctx,
            FxConversionParams(
                sell_currency="USD",
                sell_amount=D("1100.00"),
                buy_currency="EUR",
                buy_amount=D("1000.00"),
            ),
        ),
    )
    draft = fx_conversion(
        s,
        ctx,
        FxConversionParams(
            sell_currency="EUR",
            sell_amount=D("1000.00"),
            buy_currency="GBP",
            buy_amount=D("850.00"),
        ),
    )
    # GBP carried at 850 x 1.30 = 1,105.00 vs EUR carrying 1,100.00: realized FX +5.00.
    assert any(line.rate_basis is RateBasis.SPOT and line.currency == "GBP" for line in draft.lines)
    s = post(s, draft)
    assert s.balance(chart.CASH, "GBP").functional == D("1105.00000000")
    assert equity(s) == D("100005.00")
    with pytest.raises(TemplateError, match="no rate for CHF"):
        fx_conversion(
            s,
            ctx,
            FxConversionParams(
                sell_currency="GBP", sell_amount=D("1"), buy_currency="CHF", buy_amount=D("1")
            ),
        )


def test_foreign_accrual_settled_at_a_different_rate_realizes_fx():
    s = funded()
    s = post(
        s,
        fx_conversion(
            s,
            Ctx(),
            FxConversionParams(
                sell_currency="USD",
                sell_amount=D("108.50"),
                buy_currency="EUR",
                buy_amount=D("100.00"),
            ),
        ),
    )
    accrue_ctx = Ctx(rates={"EUR": D("1.2000")})
    s = post(
        s,
        financing_cost(
            s,
            accrue_ctx,
            FinancingCostParams(kind=FinancingKind.BORROW, currency="EUR", amount=D("10.00")),
        ),
    )
    # Payable carried at 12.00; paid with euros carried at 10.85 => realized FX gain 1.15.
    s = post(
        s,
        accrual_settlement(
            s,
            accrue_ctx,
            AccrualSettlementParams(
                payable=PayableKind.FINANCING, currency="EUR", amount=D("10.00")
            ),
        ),
    )
    assert -sum(b.functional for (c, _, _), b in s.balances.items() if c == chart.REALIZED_FX) == D(
        "1.15"
    )
    assert equity(s) == equity_by_components(s)


def test_currencies_never_mix_without_conversion():
    s = funded()
    with pytest.raises(InsufficientBalanceError):  # there is no EUR cash to spend
        post(s, fee(s, Ctx(), FeeParams(kind=FeeKind.FEE, currency="EUR", amount=D("1.00"))))
    bad = EntryDraft(EntryType.FEE, "mixes currencies")
    bad.lines += [
        DraftLine(
            chart.FEES, Side.DEBIT, "EUR", D("1.00"), D("1.09"), RateBasis.SPOT, D("1.085"), 1
        ),
        DraftLine(
            chart.CASH,
            Side.CREDIT,
            "USD",
            D("1.09"),
            D("1.09"),
            RateBasis.FUNCTIONAL,
            D(1),
        ),
    ]
    with pytest.raises(EntryValidationError, match="R1"):
        validate(bad, "USD")


# ------------------------------------------------------------------ validation rules
def _line(
    code,
    side,
    amount,
    functional=None,
    cur="USD",
    basis=RateBasis.FUNCTIONAL,
    rate=D(1),
    rid=None,
    asset=None,
):
    return DraftLine(
        code,
        side,
        cur,
        D(amount),
        D(functional if functional is not None else amount),
        basis,
        rate,
        rid,
        asset,
    )


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([_line(chart.CASH, Side.DEBIT, "1.00")], "at least two lines"),
        (
            [
                _line(chart.CASH, Side.DEBIT, "1.00"),
                _line(chart.CONTRIBUTED_CAPITAL, Side.CREDIT, "0.99"),
            ],
            "R1",
        ),
        (
            [
                _line(chart.CASH, Side.DEBIT, "1.001"),
                _line(chart.CONTRIBUTED_CAPITAL, Side.CREDIT, "1.001"),
            ],
            "decimal places",
        ),
        (
            [
                _line(chart.CASH, Side.DEBIT, "1.00", "1.01"),
                _line(chart.CONTRIBUTED_CAPITAL, Side.CREDIT, "1.00"),
            ],
            "functional value == amount",
        ),
        (
            [
                _line(chart.CASH, Side.DEBIT, "0", "1", cur="EUR", basis=RateBasis.NONE, rate=None),
                _line(
                    chart.UNREALIZED_FX,
                    Side.CREDIT,
                    "0",
                    "1",
                    cur="EUR",
                    basis=RateBasis.NONE,
                    rate=None,
                ),
            ],
            "zero-amount lines are not allowed",
        ),
        (
            [
                _line(chart.FX_TRANSLATION, Side.DEBIT, "1.00"),
                _line(chart.CASH, Side.CREDIT, "1.00"),
            ],
            "functional-only",
        ),
        (
            [_line(chart.LONG_COST, Side.DEBIT, "1.00"), _line(chart.CASH, Side.CREDIT, "1.00")],
            "kept per asset",
        ),
        (
            [
                _line(chart.CASH, Side.DEBIT, "1.00", asset=AAPL),
                _line(chart.CONTRIBUTED_CAPITAL, Side.CREDIT, "1.00"),
            ],
            "not kept per asset",
        ),
        (
            [
                _line(
                    chart.CASH,
                    Side.DEBIT,
                    "1.00",
                    "1.09",
                    cur="EUR",
                    basis=RateBasis.SPOT,
                    rate=D("1.085"),
                ),
                _line(
                    chart.FEES,
                    Side.CREDIT,
                    "1.00",
                    "1.09",
                    cur="EUR",
                    basis=RateBasis.SPOT,
                    rate=D("1.085"),
                ),
            ],
            "references its fx_rates row",
        ),
        (
            [
                _line(chart.CASH, Side.DEBIT, "1.00", "1.09", cur="EUR"),
                _line(chart.FEES, Side.CREDIT, "1.00", "1.09", cur="EUR"),
            ],
            "needs a rate basis",
        ),
        (
            [
                _line(chart.CASH, Side.DEBIT, "1.00", cur="XAU"),
                _line(chart.FEES, Side.CREDIT, "1.00", cur="XAU"),
            ],
            "unsupported currency",
        ),
        (
            [
                _line(
                    chart.CASH,
                    Side.DEBIT,
                    "1.00",
                    "2.00",
                    cur="EUR",
                    basis=RateBasis.SPOT,
                    rate=D(2),
                    rid=1,
                ),
                _line(
                    chart.FEES,
                    Side.CREDIT,
                    "1.00",
                    "1.00",
                    cur="EUR",
                    basis=RateBasis.SPOT,
                    rate=D(1),
                    rid=1,
                ),
            ],
            "R2",
        ),
    ],
)
def test_validation_rejects(lines, message):
    draft = EntryDraft(EntryType.FEE, "test")
    draft.lines += lines
    with pytest.raises(EntryValidationError, match=message):
        validate(draft, "USD")


def test_float_inputs_are_refused():
    with pytest.raises(MoneyError, match="never a float"):
        to_decimal(0.1)
    with pytest.raises(ValidationError):
        TradeParams(asset_id=AAPL, quantity=1.5, price=D("10"))
    with pytest.raises(ValidationError):
        FeeParams(kind=FeeKind.FEE, currency="XAU", amount=D("1"))


def test_zero_decimal_currency():
    assert currency("JPY").minor_units == 0
    with pytest.raises(MoneyError):
        currency("JPY").amount(D("100.5"))
    assert currency("JPY").round(D("100.5")) == D("100")  # half-even
    assert currency("JPY").round(D("101.5")) == D("102")


# ------------------------------------------------------------------ reversals
def _stored(
    state_before: LedgerState, draft: EntryDraft, seq: int, later=frozenset()
) -> StoredEntry:
    return StoredEntry(
        entry_id=uuid.uuid4(),
        entry_type=draft.entry_type,
        sequence=seq,
        lines=tuple(
            StoredLine(
                ln.account_code,
                ln.side,
                ln.currency,
                ln.amount,
                ln.functional_amount,
                ln.rate_basis,
                ln.fx_rate,
                ln.fx_rate_id,
                ln.asset_id,
                ln.memo,
            )
            for ln in draft.lines
        ),
        movements=tuple(draft.movements),
        reversed=False,
        assets_touched_later=later,
    )


def test_reversal_restores_balances_and_quantities():
    s0 = funded()
    draft = long_purchase(
        s0, Ctx(), TradeParams(asset_id=AAPL, quantity=D("10"), price=D("10"), commission=D("1"))
    )
    s1 = post(s0, draft)
    s2 = post(s1, reversal(s1, Ctx(), _stored(s0, draft, 2), "test correction"))
    assert s2.position(AAPL) is None
    for key, bal in s2.balances.items():
        assert bal == s0.balance(*key), key
    with pytest.raises(TemplateError, match="later entries"):
        reversal(s1, Ctx(), _stored(s0, draft, 2, later=frozenset({AAPL})), "unsafe")
    with pytest.raises(TemplateError, match="cannot be reversed"):
        reversal(
            s0,
            Ctx(),
            _stored(
                LedgerState("USD"),
                opening_funding(LedgerState("USD"), Ctx(), OpeningFundingParams(amount=D(1))),
                1,
            ),
            "no",
        )


# ------------------------------------------------------------------ registry
def test_registry_covers_every_entry_type_and_hashes_are_stable():
    assert {t.entry_type for t in TEMPLATES.values()} == set(EntryType)
    for template in TEMPLATES.values():
        assert template.definition_sha256() == template.definition_sha256()
        assert len(template.definition_sha256()) == 64
    assert not TEMPLATES["opening_funding"].controlled_test_only
    assert TEMPLATES["long_purchase"].controlled_test_only


# ------------------------------------------------------------------ independent model
@dataclass
class Model:
    """Economic state computed without the ledger: what equity must be after a checkpoint."""

    cash_and_collateral: dict[str, D] = field(default_factory=lambda: defaultdict(lambda: D(0)))
    payables: dict[str, D] = field(default_factory=lambda: defaultdict(lambda: D(0)))
    quantity: dict[uuid.UUID, D] = field(default_factory=lambda: defaultdict(lambda: D(0)))

    def equity(self, ctx: Ctx, prices: dict[uuid.UUID, D]) -> D:
        net: dict[str, D] = defaultdict(lambda: D(0))
        for cur, amount in self.cash_and_collateral.items():
            net[cur] += amount
        for cur, amount in self.payables.items():
            net[cur] -= amount
        for asset, qty in self.quantity.items():
            if qty == 0:
                continue
            cur = ctx.instruments[asset].currency
            value = currency(cur).round(abs(qty) * prices[asset])
            net[cur] += value if qty > 0 else -value
        out = D(0)
        for cur, amount in net.items():
            out += amount if cur == "USD" else round_functional(amount * ctx.rates[cur], "USD")
        return out


@pytest.mark.parametrize("seed", range(12))
def test_random_sequences_reconcile_to_an_independent_model(seed):
    rng = random.Random(seed)  # noqa: S311  # deterministic test vectors, not security
    ctx = Ctx(rates={"EUR": D("1.0850")})
    s = funded()
    model = Model()
    model.cash_and_collateral["USD"] = D("100000.00")
    prices = {AAPL: D("100.00"), XYZ: D("40.00"), SAP: D("90.00")}
    s = post(
        s,
        fx_conversion(
            s,
            ctx,
            FxConversionParams(
                sell_currency="USD",
                sell_amount=D("21700.00"),
                buy_currency="EUR",
                buy_amount=D("20000.00"),
            ),
        ),
    )
    model.cash_and_collateral["USD"] -= D("21700.00")
    model.cash_and_collateral["EUR"] += D("20000.00")
    for step in range(60):
        action = rng.choice(["trade", "trade", "trade", "cost", "mark", "fx"])
        if action == "mark":
            for asset in prices:
                prices[asset] = (prices[asset] * D(rng.randint(90, 111)) / 100).quantize(D("0.01"))
            ctx.rates["EUR"] = (ctx.rates["EUR"] * D(rng.randint(97, 103)) / 100).quantize(
                D("0.0001")
            )
            open_marks = {a: str(prices[a]) for a in prices if s.position(a) is not None}
            s = mark(s, open_marks, ctx, translate=["EUR"])
            assert equity(s) == equity_by_components(s) == model.equity(ctx, prices), step
        elif action == "trade":
            asset = rng.choice(list(prices))
            cur = ctx.instruments[asset].currency
            qty = D(rng.randint(1, 30))
            commission = D(rng.randint(0, 300)) / 100
            pos = s.position(asset)
            held = pos.quantity if pos else D(0)
            p = prices[asset]
            if held > 0 and rng.random() < 0.5:
                qty = min(qty, held)
                s = sell(s, asset, qty, p, ctx, commission=commission)
                model.cash_and_collateral[cur] += currency(cur).round(qty * p) - commission
                model.quantity[asset] -= qty
            elif held < 0 and rng.random() < 0.5:
                qty = min(qty, -held)
                s = cover(s, asset, qty, p, ctx, commission=commission)
                model.cash_and_collateral[cur] -= currency(cur).round(qty * p) + commission
                model.quantity[asset] += qty
            elif held >= 0 and (held > 0 or rng.random() < 0.6):
                if cash(s, cur) < currency(cur).round(qty * p) + commission:
                    continue
                s = buy(s, asset, qty, p, ctx, commission=commission)
                model.cash_and_collateral[cur] -= currency(cur).round(qty * p) + commission
                model.quantity[asset] += qty
            else:
                if cash(s, cur) < commission:
                    continue
                s = short(s, asset, qty, p, ctx, commission=commission)
                model.cash_and_collateral[cur] += currency(cur).round(qty * p) - commission
                model.quantity[asset] -= qty
        elif action == "cost":
            cur = rng.choice(["USD", "EUR"])
            amount = D(rng.randint(1, 500)) / 100
            if rng.random() < 0.5:
                s = post(
                    s,
                    financing_cost(
                        s,
                        ctx,
                        FinancingCostParams(kind=FinancingKind.BORROW, currency=cur, amount=amount),
                    ),
                )
                model.payables[cur] += amount
            elif total(s, chart.ACCRUED_FINANCING, cur) > 0:
                due = total(s, chart.ACCRUED_FINANCING, cur)
                s = post(
                    s,
                    accrual_settlement(
                        s,
                        ctx,
                        AccrualSettlementParams(
                            payable=PayableKind.FINANCING, currency=cur, amount=due
                        ),
                    ),
                )
                model.payables[cur] -= due
                model.cash_and_collateral[cur] -= due
        else:
            eur = cash(s, "EUR")
            if eur > 100:
                sell_eur = (eur * D(rng.randint(5, 50)) / 100).quantize(D("0.01"))
                usd = (sell_eur * ctx.rates["EUR"] * D(rng.randint(995, 1005)) / 1000).quantize(
                    D("0.01")
                )
                s = post(
                    s,
                    fx_conversion(
                        s,
                        ctx,
                        FxConversionParams(
                            sell_currency="EUR",
                            sell_amount=sell_eur,
                            buy_currency="USD",
                            buy_amount=usd,
                        ),
                    ),
                )
                model.cash_and_collateral["EUR"] -= sell_eur
                model.cash_and_collateral["USD"] += usd
        assert equity(s) == equity_by_components(s)
    # A final full checkpoint: the books equal the economic value exactly.
    s = mark(
        s, {a: str(prices[a]) for a in prices if s.position(a) is not None}, ctx, translate=["EUR"]
    )
    assert equity(s) == model.equity(ctx, prices)
    for asset, qty in model.quantity.items():
        assert (s.positions[asset].quantity if asset in s.positions else 0) == qty
