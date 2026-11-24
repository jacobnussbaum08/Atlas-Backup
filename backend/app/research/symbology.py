"""Ticker-reuse detection and corrected security types (D-239). Simulation database only.

The provider's listings describe each ticker's *current* security. A ticker that changed
REDACTED_FOR_GITHUB
and named after its new holder, while its 2016-2019 bars are the old security's. This
module looks at each scanned ticker's monthly SIP bars from 2016 to the latest month,
counting a month only if it traded (volume > 0: the provider fills non-trading stretches
with zero-volume placeholder bars repeating the last close): a gap
of at least three consecutive months without any trading between its first and last bar
means the ticker stopped trading and later resumed (a reuse, or a very long suspension).
Such a ticker's current type and name are not the formation-era security's: it gets a new
revision typed UNKNOWN (a non-fund for universe purposes) with the gap recorded, and its
members get a dedicated INACTIVE research asset row, so their bars never mix with the
current holder's.

The same pass corrects one ordering issue of the first typing: a ticker in both SEC lists is
an operating company unless its provider name says ETF/ETN.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.data_engine.providers.alpaca.adapter import DATA_API_BASE_URL
from app.database.models import ResearchSecurityRecord
from app.research import sources
from app.research.universe import _revise

MIN_GAP_MONTHS = 3


def _months(client: Any, symbols: list[str]) -> dict[str, list[date]]:
    out: dict[str, list[date]] = defaultdict(list)
    for i in range(0, len(symbols), 200):
        token = None
        while True:
            params = {
                "symbols": ",".join(symbols[i : i + 200]),
                "timeframe": "1Month",
                "start": "2016-01-01T00:00:00Z",
                "end": "2026-10-31T23:59:59Z",
                "feed": "sip",
                "adjustment": "raw",
                "limit": "10000",
            }
            if token:
                params["page_token"] = token
            r = sources.get(client, f"{DATA_API_BASE_URL}/v2/stocks/bars", params)
            body = r.json()
            for sym, rows in (body.get("bars") or {}).items():
                # A month of zero-volume placeholder bars is a month without trading.
                out[sym].extend(
                    date.fromisoformat(b["t"][:10]) for b in rows if float(b.get("v") or 0) > 0
                )
            token = body.get("next_page_token")
            if not token:
                break
    return out


def _gap(months: list[date]) -> tuple[date, date] | None:
    seq = sorted({(d.year, d.month) for d in months})
    for (y0, m0), (y1, m1) in itertools.pairwise(seq):
        if (y1 * 12 + m1) - (y0 * 12 + m0) - 1 >= MIN_GAP_MONTHS:
            return date(y0, m0, 1), date(y1, m1, 1)
    return None


def correct(factory: sessionmaker[Session], progress: Any = print) -> dict[str, Any]:
    s = ResearchSecurityRecord
    with factory() as session:
        newest: dict[str, ResearchSecurityRecord] = {}
        for r in session.scalars(select(s).order_by(s.symbol, s.revision)):
            newest[r.symbol] = r
        session.expunge_all()
    scanned = sorted(sym for sym, r in newest.items() if r.first_bar is not None)
    sec = sources.Sec()
    companies, funds = set(sec.company_tickers()), sec.fund_tickers()
    with sources.alpaca_client() as client:
        months = _months(client, scanned)
    progress(f"monthly history for {len(months)} of {len(scanned)} tickers")
    reused, retyped = [], []
    with factory() as session, session.begin():
        for sym in scanned:
            r = newest[sym]
            if "reuse_gap" in (r.details or {}):
                continue  # detected by an earlier pass
            gap = _gap(months.get(sym, []))
            name = (r.name or "").upper()
            if gap is not None:
                reused.append(sym)
                session.add(
                    _revise(
                        r,
                        security_type="UNKNOWN",
                        type_basis=f"ticker reuse suspected: no trading "
                        f"{gap[0]:%Y-%m}..{gap[1]:%Y-%m}; "
                        "the provider's current listing is not the formation-era security",
                        name=None,
                        asset_id=None,  # the current holder's row is not this security's
                        details={
                            **(r.details or {}),
                            "reuse_gap": [gap[0].isoformat(), gap[1].isoformat()],
                            "reuse_detector": "traded months (volume > 0)",
                            "current_listing": {"name": r.name, "type": r.security_type},
                        },
                    )
                )
            elif (
                r.security_type == "ETF"
                and sym in companies
                and sym in funds
                and " ETF" not in f" {name}"
                and " ETN" not in f" {name}"
            ):
                retyped.append(sym)
                session.add(
                    _revise(
                        r,
                        security_type="STOCK",
                        type_basis="SEC operating-company list (also a fund ticker; the "
                        "provider name is not a fund's)",
                    )
                )
    return {
        "scanned": len(scanned),
        "reused": len(reused),
        "reused_symbols": reused[:200],
        "retyped_to_stock": retyped,
    }


__all__ = ["MIN_GAP_MONTHS", "correct"]
