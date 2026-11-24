"""X3.1 (D-244): year-end dividend retrieval, dividend-stream flags and correction records.

Provider responses here are MOCKS: a fake corporate-actions endpoint that filters on the
processing date, as the real one does (verified against it: a request for 2019-12-01..31
REDACTED_FOR_GITHUB
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.backtesting import corrections, dividends
from app.research import dividend_completeness as dc

# MOCK provider rows: a late-December ex-date paid (processed) in January.
ROWS = [
    {"id": "a1", "symbol": "FIXA", "ex_date": "2019-09-27", "process_date": "2019-10-15",
     "rate": 0.5},
    {"id": "a2", "symbol": "FIXA", "ex_date": "2019-12-30", "process_date": "2020-01-15",
     "rate": 0.5},
    {"id": "a3", "symbol": "FIXA", "ex_date": "2020-03-30", "process_date": "2020-04-15",
     "rate": 0.5},
]  # fmt: skip


def mock_client(seen: list[dict[str, str]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        lo, hi = params["start"], params["end"]
        rows = [r for r in ROWS if lo <= str(r["process_date"]) <= hi]
        return httpx.Response(200, json={"corporate_actions": {"cash_dividends": rows}})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://mock")


def test_a_processing_date_range_misses_the_late_december_ex_date():
    seen: list[dict[str, str]] = []
    rows = dividends.fetch_rows(mock_client(seen), ["FIXA"], date(2019, 1, 1), date(2019, 12, 31))
    assert [r["ex_date"] for r in rows] == ["2019-09-27"]  # the D-237 defect


def test_the_ex_date_window_finds_it_and_records_the_pages():
    seen: list[dict[str, str]] = []
    pages: list[dict[str, Any]] = []
    lo, hi = dividends.process_window(date(2019, 1, 1), date(2019, 12, 31))
    assert (lo, hi) == (date(2018, 12, 1), date(2020, 12, 31))
    rows = dividends.parse(dividends.fetch_rows(mock_client(seen), ["FIXA"], lo, hi, pages))
    inside = [r for r in rows if date(2019, 1, 1) <= r["ex_date"] <= date(2019, 12, 31)]
    assert [r["ex_date"].isoformat() for r in inside] == ["2019-09-27", "2019-12-30"]
    assert seen[0]["start"] == "2018-12-01"
    assert seen[0]["end"] == "2020-12-31"
    assert pages[0]["body"]["corporate_actions"]["cash_dividends"][1]["id"] == "a2"


def test_the_window_covers_the_longest_gaps_stored():
    lo, hi = dividends.process_window(date(2016, 1, 5), date(2019, 12, 31))
    # A row processed 13 days before its ex-date; 178 days after (the longest stored gap).
    assert lo <= date(2016, 1, 5).replace(day=5) - (date(2023, 3, 30) - date(2023, 3, 17))
    assert (hi - date(2019, 12, 31)).days >= 178
    assert dividends.EX_DATE_BASIS == "ex_date"


def test_stream_flags_find_gaps_and_a_stream_that_stops_while_trading():
    quarterly = [date(2017, m, 15) for m in (2, 5, 8, 11)] + [date(2018, 2, 15)]
    assert dc.gaps(quarterly, date(2017, 1, 3), date(2018, 3, 30)) is None
    missing = [d for d in quarterly if d != date(2017, 8, 15)]
    flags = dc.gaps(missing, date(2017, 1, 3), date(2018, 3, 30))
    assert flags is not None
    assert flags["estimated_missing_in_gaps"] == 1
    outside = dc.gaps(missing, date(2017, 1, 3), date(2018, 3, 30), [["2018-01-02", "2018-12-31"]])
    assert outside is None  # the gap is before the membership: no position could miss it
    assert flags is not None
    assert flags["gaps"] == [["2017-05-15", "2017-11-15", 184, 1]]
    stopped = dc.gaps(quarterly, date(2017, 1, 3), date(2019, 12, 31))
    assert stopped is not None
    assert stopped["stops_while_trading"]
    assert dc.gaps(quarterly[:3], date(2016, 1, 4), date(2019, 12, 31)) is None  # too few


def _record(kind: str, decision: str, ex: str, pid: str, ticker: str = "FIXA") -> dict[str, Any]:
    return {
        "kind": kind,
        "asset_id": "11111111-2222-3333-4444-555555555555",
        "label": "FIXA",
        "ticker": ticker,
        "source": "fixture",
        "dividend": {
            "provider_action_id": pid,
            "symbol": ticker,
            "ex_date": ex,
            "rate": "0.25",
            "special": False,
            "foreign": False,
            "payable_date": None,
        },
        "provider_raw": None,
        "verification": {"identity": "X", "sessions": [{"session": ex}]},
        "decision": decision,
        "reason": None,
    }


def test_events_record_voids_credits_and_unresolved_but_never_kept_dividends():
    aid = "11111111-2222-3333-4444-555555555555"
    ev = {
        "assets": {aid: {"label": "FIXA", "identity": {"identity_sha256": "e" * 64}}},
        "links": [{"asset_id": aid, "ticker": "OLDA", "source": "fixture link"}],
        "records": [
            _record("OWN", "KEEP", "2019-01-10", "k"),
            _record("OWN", "NOT_HELD", "2019-02-10", "n"),
            _record("OWN", "VOIDED_BY_V1", "2019-03-10", "v1"),
            _record("OWN", "VOID", "2019-04-10", "v"),
            _record("OWN", "UNRESOLVED", "2019-05-10", "u"),
            _record("LINK", "CREDIT", "2019-06-10", "c", ticker="OLDA"),
            _record("LINK", "REJECTED", "2019-07-10", "r", ticker="OLDA"),
        ],
    }
    events = dc._events(ev, "f" * 64)
    by_type = {(e["event_type"], e["verification_status"]) for e in events}
    assert by_type == {
        (corrections.SUCCESSION, "VERIFIED"),
        (corrections.VOID, "VERIFIED"),
        (corrections.VOID, "UNRESOLVED"),
        (corrections.CREDIT, "VERIFIED"),
        (corrections.CREDIT, "REJECTED"),
    }
    codes = [e["code"] for e in events]
    assert len(codes) == len(set(codes))
    assert all(c.startswith("X31-") and "FIXA@11111111" in c for c in codes)
    credit = next(e for e in events if e["event_type"] == corrections.CREDIT
                  and e["verification_status"] == "VERIFIED")  # fmt: skip
    assert credit["link_code"] == "X31-TS-FIXA@11111111-OLDA"
    assert credit["cash_amount"] == Decimal("0.25")
    assert {e.get("provider_action_id") for e in events} >= {"v", "u", "c"}
    assert not {"k", "n", "v1"} & {e.get("provider_action_id") for e in events}
    assert dc._events(ev, "f" * 64) == events  # deterministic
    json.dumps(events, default=str)


def test_every_investigated_identity_is_reviewed_and_links_are_explicit():
    assert set(dc.IDENTITY_REVIEW) == set(dc.INVESTIGATE)
    labels = {tickers[0] for tickers in dc.INVESTIGATE.values()}
    assert set(dc.UNVERIFIED_EIGHT) <= labels
    for link in dc.LINKS:
        assert link.asset_id in dc.INVESTIGATE  # a link only for an investigated identity
        assert link.ticker in dc.INVESTIGATE[link.asset_id]
        assert link.source.startswith("X3.1 identity investigation")
    statuses = {v[2] for v in dc.IDENTITY_REVIEW.values()}
    assert "SPLICED_AFTER_2018-12-31" in statuses  # REDACTED_FOR_GITHUB
    assert "TRUNCATED_UNRESOLVED" in statuses  # ambiguous cases stay unresolved
