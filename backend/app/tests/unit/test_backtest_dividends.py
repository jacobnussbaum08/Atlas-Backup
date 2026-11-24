"""D-237: cash dividends in the backtest ledger and the provider parsing.

All inputs are hand-written TEST FIXTURES (not provider responses)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.accounting.templates import InstrumentInfo, TradeParams
from app.backtesting.dividends import parse
from app.backtesting.ledger import (
    BacktestTemplateContext,
    CashDividendParams,
    SimulatedLedger,
)

D = Decimal
ASSET = uuid.uuid4()
T0 = datetime(2026, 10, 1, 21, tzinfo=UTC)


def ledger() -> SimulatedLedger:
    info = InstrumentInfo(ASSET, "FIX", "EQUITY", "USD", D(1), D(1), D(1), None)
    out = SimulatedLedger("USD", BacktestTemplateContext("USD", {ASSET: info}))
    out.post("opening_funding", {"amount": "10000", "currency": "USD"}, at=T0, source_ref="f")
    return out


def dividend(direction: str, amount: str) -> CashDividendParams:
    return CashDividendParams(
        asset_id=ASSET,
        currency="USD",
        direction=direction,
        ex_date=date(2026, 10, 2),
        rate=D("0.50"),
        quantity=D(10),
        amount=D(amount),
    )


def test_a_long_dividend_adds_cash_and_equity_once_and_reconciles() -> None:
    books = ledger()
    books.post("long_purchase", TradeParams(asset_id=ASSET, quantity=D(10), price=D(100)),
               at=T0, source_ref="buy")  # fmt: skip
    before = books.figures()
    entry = books.post("cash_dividend", dividend("LONG", "5.00"), at=T0, source_ref="d")
    assert entry is not None
    assert entry.entry_type.value == "DIVIDEND"
    after = books.figures()
    assert after.cash == before.cash + D(5)
    assert after.equity == before.equity + D(5)
    assert books.state.positions[ASSET].quantity == D(10)  # no quantity movement
    result = books.reconcile()
    assert result.passed, result.failures()


def test_a_short_pays_the_dividend() -> None:
    books = ledger()
    books.post("short_open", TradeParams(asset_id=ASSET, quantity=D(10), price=D(100)),
               at=T0, source_ref="short")  # fmt: skip
    before = books.figures()
    books.post("cash_dividend", dividend("SHORT", "5.00"), at=T0, source_ref="d")
    after = books.figures()
    assert after.cash == before.cash - D(5)
    assert after.equity == before.equity - D(5)
    assert books.reconcile().passed


def test_dividend_parameters_are_validated() -> None:
    with pytest.raises(ValidationError):
        dividend("LONG", "0")
    with pytest.raises(ValidationError):
        dividend("SIDEWAYS", "1")


def test_provider_rows_are_parsed_exactly_and_malformed_ones_skipped() -> None:
    rows = parse(
        [
            {"id": "a1", "symbol": "lqd", "ex_date": "2021-03-01", "rate": REDACTED_FOR_GITHUB,
             "process_date": "2021-03-05", "special": False, "foreign": False, "cusip": ""},
            {"id": "a2", "symbol": "ITUB", "ex_date": "2021-04-01", "rate": REDACTED_FOR_GITHUB,
             "foreign": True},
            {"id": "bad", "symbol": "X", "ex_date": "not a date", "rate": 1},
            {"id": "zero", "symbol": "X", "ex_date": "2021-01-04", "rate": 0},
        ]
    )  # fmt: skip
    assert [r["provider_action_id"] for r in rows] == ["a1", "a2"]
    assert rows[0]["symbol"] == "LQD"
    assert rows[0]["rate"] == D("REDACTED_FOR_GITHUB")  # no binary floating-point noise
    assert rows[0]["cusip"] is None
    assert rows[1]["foreign"] is True
    assert rows[1]["process_date"] is None
