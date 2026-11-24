"""Historical lot-size evidence for 2019-2020 from consolidated (SIP) trade prints (read only).

For each traded symbol of the evidence universe, on sample sessions spread over 2019-01 to
REDACTED_FOR_GITHUB
listing, and sessions in June, August and December 2020), this fetches the SIP trades of
09:30-10:00 ET (GET-only provider requests) and records: the number of prints, prints of
exactly 1 share, odd-lot prints (size not a multiple of 100), and prints with a non-integer
size.

A symbol is VERIFIED for whole-share trading (quantity increment 1, minimum 1) from its
first sampled session when every sampled session has at least one 1-share print and no
non-integer size. Anything else is reported, never defaulted. Nothing is written to the
database. Writes runs/lot_size_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from analyze import MANIFEST

from app.backtesting.data import sessions_between

OUT = MANIFEST.parent / "lot_size_evidence.json"
SYMBOLS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
NOT_LISTED = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
LISTED_LATER = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def sample_sessions() -> list[Any]:
    days = sessions_between(date(2019, 1, 1), date(2020, 12, 31))
    out, seen = [], set()
    for d in days:
        q = (d.day.year, (d.day.month - 1) // 3)
        if q not in seen:
            seen.add(q)
            out.append(d)
    carr = next(d for d in days if d.day >= LISTED_LATER["REDACTED_FOR_GITHUB"])
    if carr not in out:
        out.append(carr)
    # Inside the 2020-H2 test window as well: its first, a middle and its last month.
    for first in (date(2020, 6, 1), date(2020, 8, 3), date(2020, 12, 1)):
        d = next(x for x in days if x.day >= first)
        if d not in out:
            out.append(d)
    return sorted(out, key=lambda d: d.day)


async def fetch(provider: Any, day: Any, symbols: list[str]) -> dict[str, list[Decimal]]:
    from app.data_engine.capabilities import DataType
    from app.data_engine.providers.base import HistoricalObservationsRequest

    request = HistoricalObservationsRequest(
        provider_symbols=tuple(sorted(symbols)),
        data_type=DataType.TRADE,
        start=day.regular_open,
        end=day.regular_open + timedelta(minutes=30),
        page_size=10000,
    )
    sizes: dict[str, list[Decimal]] = defaultdict(list)
    async for page in provider.fetch_observations(request):
        for t in page.trades:
            if t.size is not None:
                sizes[t.provider_symbol].append(Decimal(t.size))
    return sizes


async def gather() -> dict[str, Any]:
    from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
    from app.data_engine.providers.alpaca.config import AlpacaFeed, get_alpaca_settings

    settings = get_alpaca_settings().model_copy(update={"data_feed": AlpacaFeed("sip")})
    provider = AlpacaMarketDataProvider(settings)
    evidence: dict[str, dict[str, Any]] = defaultdict(dict)
    for day in sample_sessions():
        symbols = [s for s in SYMBOLS if day.day >= LISTED_LATER.get(s, date.min)]
        sizes = await fetch(provider, day, symbols)
        for s in symbols:
            v = sizes.get(s, [])
            evidence[s][day.day.isoformat()] = {
                "prints": len(v),
                "one_share": sum(1 for x in v if x == 1),
                "odd_lot": sum(1 for x in v if x % 100 != 0),
                "non_integer": sum(1 for x in v if x != x.to_integral_value()),
            }
        print(day.day, "fetched", flush=True)
    return evidence


def main() -> None:
    evidence = asyncio.run(gather())
    result: dict[str, Any] = {"window": "09:30-10:00 ET, SIP trades", "symbols": {}}
    for s in SYMBOLS:
        days = evidence[s]
        ok = bool(days) and all(d["one_share"] > 0 and d["non_integer"] == 0 for d in days.values())
        result["symbols"][s] = {
            "status": "VERIFIED" if ok else "NOT_VERIFIED",
            "first_sampled_session": min(days) if days else None,
            "sessions": len(days),
            "sessions_with_one_share_print": sum(1 for d in days.values() if d["one_share"]),
            "non_integer_prints": sum(d["non_integer"] for d in days.values()),
            "prints": sum(d["prints"] for d in days.values()),
            "by_session": days,
        }
    result["not_listed"] = NOT_LISTED
    OUT.write_text(json.dumps(result, indent=1))
    for s, v in result["symbols"].items():
        print(s, v["status"], v["first_sampled_session"], v["sessions"],
              v["sessions_with_one_share_print"], v["non_integer_prints"], v["prints"])  # fmt: skip


if __name__ == "__main__":
    main()
