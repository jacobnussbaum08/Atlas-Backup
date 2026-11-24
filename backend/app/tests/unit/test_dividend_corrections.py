"""D-243 dividend identity corrections: per-dividend identity checks, the replay loader's
credit / void / duplicate rules, date boundaries and set hashes. Pure; no database.
Every dividend and close here is a TEST FIXTURE, not provider data."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.backtesting import corrections
from app.backtesting.corrections import CREDIT, VOID, DividendCorrection, content_sha256
from app.backtesting.dividends import Dividend, _apply
from app.research import ca_dividends
from app.research.ca_dividends import verify

A, B = uuid.UUID(int=1), uuid.UUID(int=2)
START, END = date(2017, 1, 3), date(2017, 12, 29)


def own(asset: uuid.UUID, ex: date, rate: str, action: str = "own-1") -> Dividend:
    return Dividend(asset, "NEW", ex, Decimal(rate), False, False, action, 0)


def fix(
    action: str,
    ex: date,
    rate: str,
    *,
    asset: uuid.UUID = A,
    pid: str = "old-1",
    special: bool = False,
    code: str | None = None,
) -> DividendCorrection:
    return DividendCorrection(
        action=action,
        code=code or f"{action}-{pid}",
        asset_id=asset,
        provider_ticker="OLD",
        provider_action_id=pid,
        ex_date=ex,
        rate=Decimal(rate),
        special=special,
        foreign=False,
    )


def apply(
    by_day: dict[date, list[Dividend]], fixes: list[DividendCorrection], assets: set[uuid.UUID]
) -> Counter[str]:
    stats: Counter[str] = Counter()
    _apply(by_day, fixes, assets, START, END, stats)
    return stats


# ------------------------------------------------------------------ identity checks
CLOSES = {date(2017, 3, 1): Decimal("50.00"), date(2017, 2, 28): Decimal("49.50")}


def test_a_dividend_is_the_assets_only_when_both_sessions_match() -> None:
    ex = date(2017, 3, 2)  # cum 2017-03-01, previous 2017-02-28
    same = verify(CLOSES, {"2017-03-01": "50.0", "2017-02-28": "49.55"}, ex)
    assert same["identity"] == "SAME_SECURITY"
    assert [s["session"] for s in same["sessions"]] == ["2017-03-01", "2017-02-28"]
    other = verify(CLOSES, {"2017-03-01": "31.2", "2017-02-28": "30.9"}, ex)
    assert other["identity"] == "OTHER_SECURITY"


def test_unknown_mappings_stay_unresolved() -> None:
    ex = date(2017, 3, 2)
    # One session matches, one does not: never credited, never voided.
    split = verify(CLOSES, {"2017-03-01": "50.0", "2017-02-28": "30.9"}, ex)
    assert split["identity"] == "UNDECIDED"
    # The ticker meant nothing on that date (no bars): unknown, not another security.
    assert verify(CLOSES, {}, ex)["identity"] == "UNDECIDED"
    # No stored bar for the asset: cannot be decided either.
    assert verify({}, {"2017-03-01": "50.0", "2017-02-28": "49.5"}, ex)["identity"] == ("UNDECIDED")


def test_the_check_reads_the_sessions_before_the_ex_date_across_holidays() -> None:
    # Ex-date Tuesday 2017-07-05: cum Monday 07-03 (07-04 closed), previous Friday 06-30.
    got = verify({}, {}, date(2017, 7, 5))
    assert [s["session"] for s in got["sessions"]] == ["2017-07-03", "2017-06-30"]


# ------------------------------------------------------------------ replay loader
def test_a_renamed_tickers_dividend_is_credited_to_the_asset() -> None:
    by_day: dict[date, list[Dividend]] = {}
    stats = apply(by_day, [fix(CREDIT, date(2017, 5, 10), "0.25")], {A})
    (d,) = by_day[date(2017, 5, 10)]
    assert (d.asset_id, d.symbol, d.rate, d.correction) == (
        A,
        "OLD",
        Decimal("0.25"),
        "CASH_DIVIDEND-old-1",
    )
    assert d.as_json()["correction"] == "CASH_DIVIDEND-old-1"
    assert stats == Counter({"credited_events": 1})


def test_a_special_dividend_keeps_its_amount_and_flag() -> None:
    by_day: dict[date, list[Dividend]] = {}
    apply(by_day, [fix(CREDIT, date(2017, 7, 10), "REDACTED_FOR_GITHUB", special=True)], {A})
    (d,) = by_day[date(2017, 7, 10)]
    assert d.rate == Decimal("REDACTED_FOR_GITHUB")
    assert d.special is True


def test_nothing_is_credited_twice() -> None:
    ex = date(2017, 5, 10)
    by_day = {ex: [own(A, ex, "0.25", action="same-id")]}
    stats = apply(
        by_day,
        [
            fix(CREDIT, ex, "0.25", pid="same-id"),  # the same provider action
            fix(CREDIT, ex, "0.25", pid="other-id"),  # the same terms under another id
        ],
        {A},
    )
    assert len(by_day[ex]) == 1
    assert stats["duplicates_skipped"] == 2
    # Two records of one dividend in the set: the second is skipped too.
    fresh: dict[date, list[Dividend]] = {}
    stats = apply(
        fresh,
        [fix(CREDIT, ex, "0.25", pid="x", code="c1"), fix(CREDIT, ex, "0.25", pid="y", code="c2")],
        {A},
    )
    assert len(fresh[ex]) == 1
    assert stats == Counter({"credited_events": 1, "duplicates_skipped": 1})


def test_a_different_dividend_on_the_same_day_is_not_a_duplicate() -> None:
    ex = date(2017, 3, 1)
    by_day = {ex: [own(A, ex, "0.50")]}
    apply(by_day, [fix(CREDIT, ex, "8.00", special=True)], {A})  # a special beside a regular
    assert sorted(d.rate for d in by_day[ex]) == [Decimal("0.50"), Decimal("8.00")]


def test_a_void_withholds_another_securitys_dividend_and_frees_its_terms() -> None:
    ex = date(2017, 8, 30)
    by_day = {
        ex: [own(A, ex, "0.70", action="reused-ticker")],
        date(2017, 2, 1): [own(B, date(2017, 2, 1), "1")],
    }
    stats = apply(
        by_day,
        [
            fix(VOID, ex, "0.70", pid="reused-ticker"),
            # The asset's real dividend with the same terms is then credited, not skipped.
            fix(CREDIT, ex, "0.70", pid="real-one"),
        ],
        {A, B},
    )
    assert stats["voided"] == 1
    assert [d.provider_action_id for d in by_day[ex]] == ["real-one"]
    assert by_day[date(2017, 2, 1)][0].asset_id == B  # other assets untouched


def test_corrections_respect_the_run_dates_and_assets() -> None:
    by_day: dict[date, list[Dividend]] = {}
    stats = apply(
        by_day,
        [
            fix(CREDIT, START, "0.1", pid="first-day"),
            fix(CREDIT, END, "0.1", pid="last-day"),
            fix(CREDIT, date(2016, 12, 30), "0.1", pid="before"),
            fix(CREDIT, date(2018, 1, 2), "0.1", pid="after"),
            fix(CREDIT, date(2017, 6, 1), "0.1", pid="not-in-run", asset=B),
        ],
        {A},
    )
    assert sorted(by_day) == [START, END]
    assert stats == Counter({"credited_events": 2})


# ------------------------------------------------------------------ set hashes
def _content(code: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "revision": 0, "cash_amount": "0.25", **extra}


def test_set_hashes_are_reproducible_and_content_sensitive() -> None:
    one, two = _content("DIV-A"), _content("DIV-B")
    assert content_sha256([one, two]) == content_sha256([two, one])
    assert content_sha256([one, two]) != content_sha256([one])
    assert content_sha256([one, two]) != content_sha256(
        [one, _content("DIV-B", cash_amount="0.26")]
    )
    # A new revision of a record is a different set.
    assert content_sha256([one]) != content_sha256([_content("DIV-A", revision=1)])


def test_set_versions_need_a_new_code() -> None:
    assert ca_dividends.SET_CODE.endswith("_V1")
    assert ca_dividends.PREREGISTRATION.endswith("_V1")
    assert corrections.POLICY == "ledger-dividend-identity-v1"


# ------------------------------------------------------------------ the explicit links
AUDIT = Path(__file__).resolve().parents[4] / "config/simulation/runs/ca_audit_6.json"


# REDACTED_FOR_GITHUB: test of withheld research data removed


# REDACTED_FOR_GITHUB: test of withheld research data removed


def test_assets_credited_by_corrections_are_reported() -> None:
    from app.backtesting.dividends import PointInTimeDividends

    by_day: dict[date, list[Dividend]] = {date(2017, 3, 1): [own(B, date(2017, 3, 1), "0.1")]}
    apply(by_day, [fix(CREDIT, date(2017, 5, 10), "0.25")], {A, B})
    loaded = PointInTimeDividends(by_day, {A: "NOT_RETRIEVED", B: "RETRIEVED"})
    assert loaded.corrected_assets() == {A}
