"""Verified historical lot sizes for research members (D-239; the D-238 method, generalised).

On eight sampled sessions (the first session of each half-year, 2016-2019; the provider has
no earlier data) the SIP trades of 09:35-09:45 ET are fetched (GET only) for every member
(a second pass may widen the window for symbols not yet verified). A symbol is VERIFIED for
whole-share trading (quantity increment 1, minimum 1) when at least three sampled sessions
have prints,
every one of those has at least one 1-share print, and no print has a fractional size. A
VERIFIED symbol gets one instrument specification valid from its first sampled session with
evidence; nothing else is registered or defaulted. Evidence goes to a JSON file whose
SHA-256 is cited in each specification's provenance.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.enums import InstrumentSpecSource
from app.accounting.reference import register_instrument_spec
from app.backtesting.data import sessions_between
from app.data_engine.providers.alpaca.adapter import DATA_API_BASE_URL
from app.database.models import InstrumentSpecRecord
from app.research import sources
from app.research.bars import members, request_for

MIN_SESSIONS = 3


def sample_sessions() -> list[Any]:
    days = sessions_between(date(2016, 1, 4), date(2019, 12, 31))  # no provider data earlier
    out, seen = [], set()
    for d in days:
        key = (d.day.year, d.day.month <= 6)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _trades(
    client: Any, symbols: list[str], day: Any, minutes: int = 10, asof: date | None = None
) -> dict[str, list[Decimal]]:
    start = day.regular_open + timedelta(minutes=5)
    end = start + timedelta(minutes=minutes)
    sizes: dict[str, list[Decimal]] = defaultdict(list)
    token = None
    while True:
        params = {
            "symbols": ",".join(symbols),
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "feed": "sip",
            "limit": "10000",
        }
        if asof is not None:
            params["asof"] = asof.isoformat()
        if token:
            params["page_token"] = token
        for attempt in range(8):
            r = client.get(f"{DATA_API_BASE_URL}/v2/stocks/trades", params=params)
            if r.status_code != 429:
                break
            time.sleep(5 + 10 * attempt)  # rate limited: wait, then retry the same page
        r.raise_for_status()
        body = r.json()
        for sym, rows in (body.get("trades") or {}).items():
            sizes[sym].extend(Decimal(str(t["s"])) for t in rows if t.get("s") is not None)
        token = body.get("next_page_token")
        if not token:
            return sizes


def gather(
    factory: sessionmaker[Session],
    path: Path,
    progress: Any = print,
    *,
    only: list[str] | None = None,
    minutes: int = 10,
) -> dict[str, Any]:
    """Evidence for every member (or only these labels) with a minutes window; a point-in-time
    identity's trades are requested with its own (ticker, asof)."""
    with factory() as session:
        targets = members(session)
        requests = {
            s: request_for(session, a, s)[:2] for a, s in targets if only is None or s in only
        }
    groups: dict[date | None, dict[str, str]] = defaultdict(dict)  # asof -> ticker -> label
    for label, (ticker, asof) in sorted(requests.items()):
        groups[asof][ticker] = label
    evidence: dict[str, dict[str, Any]] = defaultdict(dict)
    with sources.alpaca_client() as client:
        for day in sample_sessions():
            for asof, labels in sorted(groups.items(), key=lambda x: str(x[0])):
                tickers = sorted(labels)
                for i in range(0, len(tickers), 100):
                    batch = tickers[i : i + 100]
                    got = _trades(client, batch, day, minutes, asof)
                    if len(batch) > 1:  # tickers mapped to one security: ask each alone
                        for t in batch:
                            if t not in got:
                                got.update(_trades(client, [t], day, minutes, asof))
                    for ticker, v in got.items():
                        if ticker not in labels:
                            continue
                        evidence[labels[ticker]][day.day.isoformat()] = {
                            "prints": len(v),
                            "one_share": sum(1 for x in v if x == 1),
                            "non_integer": sum(1 for x in v if x != x.to_integral_value()),
                        }
            progress(f"lot evidence {day.day}")
    result: dict[str, Any] = {
        "window": f"{minutes} minutes from 09:35 ET, SIP trades",
        "symbols": {},
    }
    for sym in sorted(requests):
        days = {d: e for d, e in evidence.get(sym, {}).items() if e["prints"] > 0}
        ok = (
            len(days) >= MIN_SESSIONS
            and all(e["one_share"] > 0 for e in days.values())
            and all(e["non_integer"] == 0 for e in days.values())
        )
        result["symbols"][sym] = {
            "status": "VERIFIED" if ok else "NOT_VERIFIED",
            "first_session_with_evidence": min(days) if days else None,
            "sessions_with_prints": len(days),
            "by_session": days,
        }
    path.write_text(json.dumps(result, indent=1))
    counts: dict[str, int] = defaultdict(int)
    for v in result["symbols"].values():
        counts[v["status"]] += 1
    return {
        "symbols": len(requests),
        **counts,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def register(factory: sessionmaker[Session], path: Path) -> dict[str, int]:
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    evidence = json.loads(raw)["symbols"]
    out = {"registered": 0, "not_verified": 0, "not_in_this_evidence": 0, "already": 0}
    with factory() as session, session.begin():
        for asset_id, symbol in members(session, current=True):
            info = evidence.get(symbol)
            if info is None:
                out["not_in_this_evidence"] += 1
                continue
            if info["status"] != "VERIFIED":
                out["not_verified"] += 1
                continue
            first = date.fromisoformat(info["first_session_with_evidence"])
            valid_from = datetime(first.year, first.month, first.day, tzinfo=UTC)
            exists = session.scalar(
                select(InstrumentSpecRecord.id).where(
                    InstrumentSpecRecord.asset_id == asset_id,
                    InstrumentSpecRecord.valid_from == valid_from,
                )
            )
            if exists:
                out["already"] += 1
                continue
            register_instrument_spec(
                session,
                asset_id=asset_id,
                quantity_increment=Decimal(1),
                min_quantity=Decimal(1),
                valid_from=valid_from,
                source=InstrumentSpecSource.OPERATOR,
                provenance=(
                    "D-239 research lot size: whole-share trading verified from SIP trade "
                    f"prints on {info['sessions_with_prints']} sampled sessions from "
                    f"{first.isoformat()} (one-share prints each, no fractional size); "
                    f"evidence research_lot_evidence.json sha256 {sha}"
                ),
                created_by="operator:research-foundation",
            )
            out["registered"] += 1
    return out


__all__ = ["gather", "register", "sample_sessions"]
