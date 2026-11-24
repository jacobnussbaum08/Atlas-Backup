"""X3.1 (D-244): the historical trading calendar against independent records of sessions.

Atlas's NYSE calendar (``app.data_engine.calendar``) is generated from holiday rules plus a
list of unscheduled closures. Before X3.1 that list held only 2025-01-09, so
2018-12-05 (National Day of Mourning for President George H. W. Bush) and Hurricane Sandy's
2012-10-29/30 counted as sessions. This check compares the calendar, day by day, with:

* **Alpaca's exchange calendar** (Trading API ``/v2/calendar``, GET only): every session
  with its open and close time. Early closes are sessions closing before 16:00.
* **SPY's SIP daily bars** from the provider (RAW, GET only): a day with consolidated
  trading in the most liquid ETF is a session; a weekday without it is not.
* **The stored daily bars** of the simulation database: every distinct New York date with a
  ``1Day`` bar, and the number of bars on it.

The evidence keeps the provider's calendar rows and SPY's bar dates, so the comparison
reproduces offline (``compare(evidence)``); the unit tests read it. Nothing is written to
the database. Credentials are never printed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.data_engine import calendar as nyse
from app.data_engine.providers.alpaca.reference import PAPER_TRADING_API_BASE_URL
from app.research import sources
from app.research.exclusions import sha256_file

FIRST, LAST = date(1990, 1, 1), date(2027, 12, 31)
SPY_FIRST = date(2004, 1, 1)  # requested; the provider's SIP history begins 2016-01-04
EARLY = "16:00"


def _ny_date(stamp: str) -> str:
    at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return at.astimezone(sources.EASTERN).date().isoformat()


def _days(first: date, last: date) -> list[date]:
    out, day = [], first
    while day <= last:
        out.append(day)
        day += timedelta(days=1)
    return out


def _provider_calendar(client: Any) -> list[dict[str, Any]]:
    r = sources.get(
        client,
        f"{PAPER_TRADING_API_BASE_URL}/v2/calendar",
        {"start": FIRST.isoformat(), "end": LAST.isoformat()},
    )
    return sorted(
        ({"date": x["date"], "open": x["open"], "close": x["close"]} for x in r.json()),
        key=lambda x: x["date"],
    )


def _spy_days(client: Any, last: date) -> list[str]:
    return sorted(
        {_ny_date(bar["t"]) for _, bar in sources.daily_bars(client, ["SPY"], SPY_FIRST, last)}
    )


def _stored_days(session: Session) -> dict[str, int]:
    rows = session.execute(
        text(
            "select (event_time at time zone 'America/New_York')::date, count(*) "
            "from market_bars where timeframe = '1Day' group by 1 order by 1"
        )
    )
    return {d.isoformat(): int(n) for d, n in rows}


def compare(evidence: dict[str, Any]) -> dict[str, Any]:
    """Day-by-day differences between the calendar and each source (offline)."""
    cal = {date.fromisoformat(x["date"]): x for x in evidence["provider_calendar"]}
    spy = {date.fromisoformat(d) for d in evidence["spy_sip_bar_days"]}
    stored = {date.fromisoformat(d): n for d, n in evidence["stored_bar_days"].items()}
    spy_first, spy_last = (min(spy), max(spy)) if spy else (SPY_FIRST, SPY_FIRST)
    out: dict[str, Any] = {
        "calendar_not_provider": [],
        "provider_not_calendar": [],
        "early_close_differs": [],
        "calendar_not_spy": [],
        "spy_not_calendar": [],
        "calendar_without_stored_bars": [],
        "stored_bars_on_closed_days": [],
    }
    stored_lo = min(stored) if stored else None
    stored_hi = max(stored) if stored else None
    for day in _days(FIRST, LAST):
        ours = nyse.trading_day(day)
        p = cal.get(day)
        if ours and not p:
            out["calendar_not_provider"].append(day.isoformat())
        if p and not ours:
            out["provider_not_calendar"].append(day.isoformat())
        if ours and p and (p["close"] != EARLY) != ours.early_close:
            out["early_close_differs"].append([day.isoformat(), p["close"], ours.early_close])
        if spy and spy_first <= day <= spy_last:
            if ours and day not in spy:
                out["calendar_not_spy"].append(day.isoformat())
            if day in spy and not ours:
                out["spy_not_calendar"].append(day.isoformat())
        if stored_lo and stored_hi and stored_lo <= day <= stored_hi:
            if ours and day not in stored:
                out["calendar_without_stored_bars"].append(day.isoformat())
            if day in stored and not ours:
                out["stored_bars_on_closed_days"].append([day.isoformat(), stored[day]])
    since = min(nyse.CROSS_CHECKED_YEARS)
    out["agrees_from"] = f"{since}-01-01"
    out["differences_from_agrees_from"] = sum(
        1
        for key, rows in out.items()
        if isinstance(rows, list)
        for row in rows
        if (row[0] if isinstance(row, list) else row) >= f"{since}-01-01"
    )
    return out


def build(factory: sessionmaker[Session], out: Path, last_spy: date) -> dict[str, Any]:
    with sources.alpaca_client() as client:
        provider_calendar = _provider_calendar(client)
        spy = _spy_days(client, last_spy)
    with factory() as session:
        stored = _stored_days(session)
    evidence: dict[str, Any] = {
        "kind": "x31_calendar_evidence",
        "decision": "D-244",
        "sources": {
            "provider_calendar": f"Alpaca Trading API GET /v2/calendar start={FIRST} end={LAST}",
            "spy_sip_bar_days": f"Alpaca Market Data GET /v2/stocks/bars SPY 1Day SIP RAW "
            f"{SPY_FIRST}..{last_spy} (New York dates; the history begins 2016-01-04)",
            "stored_bar_days": "simulation database market_bars timeframe 1Day "
            "(New York date: bar count)",
            "special_closures": {d.isoformat(): r for d, r in nyse.SPECIAL_CLOSURES.items()},
            "public_notices": {
                "2018-12-05": "NYSE press release 2018-12-01 'New York Stock Exchange to Honor "
                "President George H. W. Bush' (ir.theice.com; businesswire.com "
                "20181201005020)",
                "2012-10-29/30": "NYSE Euronext notices 2012-10-28/29: US equity markets "
                "closed for Hurricane Sandy",
            },
        },
        "provider_calendar": provider_calendar,
        "spy_sip_bar_days": spy,
        "stored_bar_days": stored,
    }
    evidence["comparison"] = compare(evidence)
    body = json.dumps(evidence, indent=1, sort_keys=True)
    out.write_text(body)
    return {
        "path": str(out),
        "sha256": sha256_file(out),  # the file as written (line endings included)
        "provider_sessions": len(provider_calendar),
        "spy_days": len(spy),
        "stored_days": len(stored),
        **{k: v for k, v in evidence["comparison"].items()},
    }


__all__ = ["build", "compare"]
