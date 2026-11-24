"""X3.1 (D-244): dividend policy ``cash-at-ex-date-v2`` against the database.

Assets, dividends, retrievals and corrections are TEST FIXTURES, not provider data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from app.backtesting import corrections
from app.backtesting.config import DIVIDEND_POLICY, DIVIDEND_POLICY_V1
from app.backtesting.corrections import DividendCorrection
from app.backtesting.data import sessions_between
from app.backtesting.dividends import NOT_RETRIEVED, RETRIEVED, PointInTimeDividends
from app.database.models import Asset, CashDividendRecord
from app.database.sessions import build_session_factory, session_scope
from app.tests.integration.backtest_support import RUN_END, RUN_START
from app.tests.integration.test_backtest_engine_versions import RETRIEVED_AT, seed_dividends

pytestmark = pytest.mark.integration

KNOWN = RETRIEVED_AT + timedelta(days=1)
DAYS = [s.day for s in sessions_between(RUN_START, RUN_END)]


def asset(factory, symbol: str, *, active: bool = True) -> uuid.UUID:
    with session_scope(factory) as session:
        row = Asset(
            id=uuid.uuid4(),
            symbol=symbol,
            asset_class="EQUITY",
            name=f"{symbol} fixture identity",
            exchange_code="XNAS" if active else None,
            quote_currency="USD",
            status="ACTIVE" if active else "INACTIVE",
        )
        session.add(row)
        return row.id


def load(factory, assets: dict[uuid.UUID, str], policy: str, fixes=()) -> PointInTimeDividends:
    with factory() as session:
        return PointInTimeDividends.load(
            session, assets, start=RUN_START, end=RUN_END, known_at=KNOWN,
            corrections=list(fixes), policy=policy,
        )  # fmt: skip


def test_a_processing_date_range_covers_under_v1_only(fresh_engine: Engine) -> None:
    factory = build_session_factory(fresh_engine)
    a = asset(factory, "FIXL")
    seed_dividends(factory, a, "FIXL", Decimal("0.10"), basis=None)  # a D-237 retrieval
    v1 = load(factory, {a: "FIXL"}, DIVIDEND_POLICY_V1)
    v2 = load(factory, {a: "FIXL"}, DIVIDEND_POLICY)
    assert v1.coverage[a] == RETRIEVED
    assert v1.count() == len(DAYS)
    assert v2.coverage[a] == NOT_RETRIEVED  # its range was the provider's processing dates
    assert v2.count() == 0
    seed_dividends(factory, a, "FIXL", Decimal("0.10"), revision=1)  # an ex-date retrieval
    assert load(factory, {a: "FIXL"}, DIVIDEND_POLICY).coverage[a] == RETRIEVED


def test_exact_provider_duplicates_are_credited_once_under_v2(fresh_engine: Engine) -> None:
    factory = build_session_factory(fresh_engine)
    a = asset(factory, "FIXD")
    seed_dividends(factory, a, "FIXD", Decimal("0.20"))
    with session_scope(factory) as session:
        for n, special in enumerate((False, True), start=1):  # the XLF 2016 pattern
            session.add(
                CashDividendRecord(
                    provider="alpaca", provider_action_id=f"dup-{n}", revision=0,
                    asset_id=a, symbol="FIXD", ex_date=DAYS[2], rate=Decimal("0.20"),
                    special=special, foreign=False, retrieved_at=RETRIEVED_AT,
                    raw={"fixture": True},
                )
            )  # fmt: skip
    v1 = load(factory, {a: "FIXD"}, DIVIDEND_POLICY_V1)
    v2 = load(factory, {a: "FIXD"}, DIVIDEND_POLICY)
    assert len(v1.on(DAYS[2])) == 3  # D-237 credits all three
    (kept,) = v2.on(DAYS[2])
    assert not kept.special  # the non-special row is kept
    assert v2.corrections["provider_duplicates_collapsed"] == 2
    assert v2.count() == len(DAYS)


def test_a_shared_symbol_joins_both_identities_and_a_void_separates_them(
    fresh_engine: Engine,
) -> None:
    factory = build_session_factory(fresh_engine)
    early = asset(factory, "FIXS", active=False)  # the ticker's holder as of an earlier date
    late = asset(factory, "FIXS")
    seed_dividends(factory, late, "FIXS", Decimal("0.30"))
    both = {early: "FIXS", late: "FIXS"}
    v1 = load(factory, both, DIVIDEND_POLICY_V1)
    assert len({d.asset_id for ds in v1._by_day.values() for d in ds}) == 1  # one wins
    v2 = load(factory, both, DIVIDEND_POLICY)
    assert {d.asset_id for d in v2.on(DAYS[0])} == {early, late}
    assert v2.corrections["shared_symbol_joins"] == 2 * len(DAYS)
    # The correction set voids, for the early identity, the dividends of the other security.
    voids = [
        DividendCorrection(
            action=corrections.VOID, code=f"X31-VOID-{d}", asset_id=early,
            provider_ticker="FIXS", provider_action_id=f"fixture-FIXS-{d.isoformat()}",
            ex_date=d, rate=Decimal("0.30"), special=False, foreign=False,
        )
        for d in DAYS[3:]
    ]  # fmt: skip
    fixed = load(factory, both, DIVIDEND_POLICY, voids)
    assert [d.asset_id for d in fixed.on(DAYS[0])] in ([early, late], [late, early])
    assert [d.asset_id for d in fixed.on(DAYS[5])] == [late]
    assert fixed.corrections["voided"] == len(DAYS) - 3


def test_v2_orders_each_session_deterministically(fresh_engine: Engine) -> None:
    factory = build_session_factory(fresh_engine)
    early = asset(factory, "FIXO", active=False)
    late = asset(factory, "FIXO")
    seed_dividends(factory, late, "FIXO", Decimal("0.30"))
    one = load(factory, {early: "FIXO", late: "FIXO"}, DIVIDEND_POLICY)
    two = load(factory, {late: "FIXO", early: "FIXO"}, DIVIDEND_POLICY)
    assert one._by_day == two._by_day


def test_an_unknown_policy_is_refused(fresh_engine: Engine) -> None:
    factory = build_session_factory(fresh_engine)
    with pytest.raises(ValueError, match="unknown dividend policy"):
        load(factory, {}, "cash-at-ex-date-v9")


def test_known_at_still_hides_rows_retrieved_later(fresh_engine: Engine) -> None:
    factory = build_session_factory(fresh_engine)
    a = asset(factory, "FIXK")
    later = datetime(2027, 1, 1, tzinfo=UTC)
    seed_dividends(factory, a, "FIXK", Decimal("0.10"), retrieved_at=later)
    assert load(factory, {a: "FIXK"}, DIVIDEND_POLICY).count() == 0
    assert date(2026, 1, 1) < RUN_START
