"""Research bar loader and delisting inference (D-239). Simulation database only.

The data engine's historical fetch serves registered *active* assets with a provider
mapping; delisted securities have neither. This loader stores SIP daily bars (RAW and
SPLIT) for research universe members directly in ``market_bars``:

* GET-only requests; every response page's SHA-256 goes to a load manifest;
* ``source = HISTORICAL_REST`` with the retrieval time as received/available time (the
  original availability of a backfill is unknown; the replay's own policy applies);
* existing rows are never touched (insert, conflict -> nothing);
* a bar with inconsistent prices (low > open/close/high, non-positive prices) is stored
  QUARANTINED, never VALID.

Delisting inference: a member with no bar in the reserved period's last two weeks
(2019-12-16..2019-12-31) whose last SIP bar is earlier stopped trading on that bar's
session (``delisted_on``; basis recorded). A symbol change is indistinguishable from a
delisting without symbol-change history (sparse before 2020): the validation lists such
cases for review.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.data import sessions_between
from app.data_engine.providers.alpaca.adapter import DATA_API_BASE_URL
from app.database.models import (
    MarketBarRecord,
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
)
from app.research import sources
from app.research.universe import CURRENT_UNIVERSES, _revise

# The provider's history starts 2016-01-04: nothing earlier exists to load.
BARS_START, BARS_END = date(2016, 1, 4), date(2019, 12, 31)
FINAL_WINDOW = date(2019, 12, 16)
START_TS = datetime(2016, 1, 4, tzinfo=UTC)
END_TS = datetime(2019, 12, 31, 23, 59, 59, tzinfo=UTC)


def members(session: Session, current: bool = False) -> list[tuple[uuid.UUID, str]]:
    """(asset, symbol) of every universe's members, or of the current universes' only."""
    c = ResearchUniverseCandidateRecord
    query = select(c.asset_id, c.symbol).where(c.member.is_(True))
    if current:
        query = query.where(c.universe_code.in_(CURRENT_UNIVERSES))
    rows = session.execute(query.distinct()).all()
    return sorted({(a, s) for a, s in rows if a is not None}, key=lambda x: x[1])


def _newest(session: Session, symbol: str) -> ResearchSecurityRecord | None:
    s = ResearchSecurityRecord
    return session.scalars(
        select(s)
        .where(s.symbol == symbol, s.provider == "alpaca")
        .order_by(s.revision.desc())
        .limit(1)
    ).first()


def identity_of(session: Session, asset_id: uuid.UUID) -> ResearchSecurityRecord | None:
    """The point-in-time identity record of an asset (``identity.PROVIDER``), if any."""
    s = ResearchSecurityRecord
    return session.scalars(
        select(s)
        .where(s.asset_id == asset_id, s.provider == "alpaca-asof")
        .order_by(s.created_at.desc(), s.revision.desc())
        .limit(1)
    ).first()


def request_for(session: Session, asset_id: uuid.UUID, label: str) -> tuple[str, date | None, date]:
    """(ticker, asof, end) to request an asset's bars: its identity's pinned mapping, or the
    label with the provider's default mapping up to the era end (equity V1-V3, ETFs)."""
    ident = identity_of(session, asset_id)
    if ident is not None:
        d = ident.details or {}
        return d["request_symbol"], date.fromisoformat(d["asof"]), BARS_END
    return label, None, era_end(_newest(session, label))


def era_end(sec: ResearchSecurityRecord | None) -> date:
    """The last day whose bars belong to the security: for a ticker reused inside the period
    (traded months resume after a gap), the end of its last traded month before the gap."""
    gap = ((sec.details or {}) if sec is not None else {}).get("reuse_gap")
    if not gap:
        return BARS_END
    last_month = date.fromisoformat(gap[0])
    nxt = date(last_month.year + last_month.month // 12, last_month.month % 12 + 1, 1)
    return min(BARS_END, nxt - timedelta(days=1))


def _page(
    client: Any,
    symbols: list[str],
    adjustment: str,
    token: str | None,
    end: date = BARS_END,
    asof: date | None = None,
) -> dict[str, Any]:
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": START_TS.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": f"{end.isoformat()}T23:59:59Z",
        "feed": "sip",
        "adjustment": adjustment,
        "limit": "10000",
        "sort": "asc",
    }
    if asof is not None:
        params["asof"] = asof.isoformat()
    if token:
        params["page_token"] = token
    r = sources.get(client, f"{DATA_API_BASE_URL}/v2/stocks/bars", params)
    return {"body": r.json(), "sha256": hashlib.sha256(r.content).hexdigest()}


def load(
    factory: sessionmaker[Session], manifest_path: Path, progress: Any = print
) -> dict[str, Any]:
    with factory() as session:
        targets = members(session, current=True)
        specs: dict[uuid.UUID, tuple[str, date | None, date]] = {}
        for a, s in targets:
            specs.setdefault(a, request_for(session, a, s))
        b = MarketBarRecord
        loaded = set(
            session.scalars(
                select(b.asset_id)
                .where(
                    b.asset_id.in_([a for a, _ in targets]),
                    b.feed == "sip",
                    b.timeframe == "1Day",
                    b.event_time >= START_TS,
                    b.event_time <= END_TS,
                )
                .distinct()
            )
        )
    pending = {a: spec for a, spec in specs.items() if a not in loaded}  # assets without bars
    # Batches share a mapping date and an end date (a ticker reused inside the period loads
    # to its era's end); within a batch each ticker names one asset.
    groups: dict[tuple[date | None, date], dict[str, uuid.UUID]] = defaultdict(dict)
    for a, (ticker, asof, end) in sorted(pending.items(), key=lambda x: x[1][0]):
        groups[(asof, end)][ticker] = a
    batches: list[tuple[list[str], date, date | None, dict[str, uuid.UUID]]] = []
    for (asof, end), by_ticker in sorted(groups.items(), key=lambda x: (str(x[0][0]), x[0][1])):
        tickers = sorted(by_ticker)
        batches += [(tickers[i : i + 50], end, asof, by_ticker) for i in range(0, len(tickers), 50)]
    manifest: dict[str, Any] = {
        "range": [BARS_START.isoformat(), BARS_END.isoformat()],
        "truncated_at_reuse": {
            t: e.isoformat() for t, _a, e in sorted(pending.values(), key=str) if e < BARS_END
        },
        "asof": {t: a.isoformat() for t, a, _e in pending.values() if a is not None},
        "pages": [],
        "stored": 0,
        "quarantined": 0,
    }
    with sources.alpaca_client() as client:
        for adjustment, label in (("raw", "RAW"), ("split", "SPLIT")):
            queue = list(batches)
            n = 0
            while n < len(queue):
                batch, end, asof, ids = queue[n]
                n += 1
                returned: set[str] = set()
                token = None
                while True:
                    page = _page(client, batch, adjustment, token, end, asof)
                    manifest["pages"].append(
                        {
                            "adjustment": label,
                            "symbols": [batch[0], batch[-1]],
                            "sha256": page["sha256"],
                        }
                    )
                    rows = []
                    now = sources.now_utc()
                    for sym, bars in (page["body"].get("bars") or {}).items():
                        returned.add(sym)
                        for b in bars:
                            o, h, lo, c = (float(b[k]) for k in ("o", "h", "l", "c"))
                            ok = 0 < lo <= min(o, c) and max(o, c) <= h
                            rows.append(
                                {
                                    "asset_id": ids[sym],
                                    "provider": "alpaca",
                                    "feed": "sip",
                                    "provider_symbol": sym,
                                    "timeframe": "1Day",
                                    "adjustment": label,
                                    "event_time": b["t"],
                                    "revision": 0,
                                    "open": str(b["o"]),
                                    "high": str(b["h"]),
                                    "low": str(b["l"]),
                                    "close": str(b["c"]),
                                    "volume": str(b.get("v", 0)),
                                    "vwap": None if b.get("vw") is None else str(b["vw"]),
                                    "trade_count": b.get("n"),
                                    "is_complete": True,
                                    "source": "HISTORICAL_REST",
                                    "quality_status": "VALID" if ok else "QUARANTINED",
                                    "received_at": now,
                                    "available_at": now,
                                    "processed_at": now,
                                    "observation_kind": "HISTORICAL",
                                }
                            )
                            manifest["quarantined"] += not ok
                    for k in range(0, len(rows), 2000):  # < 65535 bind parameters
                        with factory() as session, session.begin():
                            res = session.execute(
                                insert(MarketBarRecord)
                                .values(rows[k : k + 2000])
                                .on_conflict_do_nothing(constraint="uq_market_bars_observation")
                                .returning(MarketBarRecord.id)
                            )
                            manifest["stored"] += len(res.all())
                    token = page["body"].get("next_page_token")
                    if not token:
                        break
                # Two tickers mapped to one security come back under one key: retry the
                # missing ones alone (each then answers for itself).
                missing = [t for t in batch if t not in returned]
                if len(batch) > 1 and missing:
                    queue += [([t], end, asof, ids) for t in missing]
                    manifest.setdefault("retried_alone", []).extend(missing)
                progress(f"{label} batch {n}/{len(queue)} stored {manifest['stored']}")
    manifest["assets"] = len(pending)
    manifest_path.write_text(json.dumps(manifest, indent=1))
    manifest["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return {k: v for k, v in manifest.items() if k != "pages"}


MIN_STOP_SESSIONS = 20


def first_long_stop(session: Session, asset_id: uuid.UUID) -> date | None:
    """The last traded session before the first run of at least ``MIN_STOP_SESSIONS``
    sessions without a trade (no bar, or a zero-volume placeholder) in 2016-2019, if any."""
    b = MarketBarRecord
    traded = {
        at.astimezone(sources.EASTERN).date()
        for (at,) in session.execute(
            select(b.event_time).where(
                b.asset_id == asset_id,
                b.feed == "sip",
                b.timeframe == "1Day",
                b.adjustment == "RAW",
                b.quality_status == "VALID",
                b.volume > 0,
                b.event_time >= START_TS,
                b.event_time <= END_TS,
            )
        )
    }
    return stop_before_gap(traded)


def stop_before_gap(traded: set[date], end: date = BARS_END) -> date | None:
    """Pure part of :func:`first_long_stop` over a set of traded sessions."""
    if not traded:
        return None
    last: date | None = None
    run = 0
    for s in sessions_between(min(traded), end):
        if s.day in traded:
            last, run = s.day, 0
            continue
        run += 1
        if run >= MIN_STOP_SESSIONS:
            return last
    return None


def infer_delistings(factory: sessionmaker[Session]) -> dict[str, int]:
    """Record the last traded session (volume > 0; zero-volume bars are the provider's
    placeholders) of current members that stopped trading in the period, or before their
    ticker's reuse inside it."""
    b = MarketBarRecord
    out = {"delisted": 0, "trading_at_end": 0, "no_bars": 0, "identity_records": 0}
    seen: set[uuid.UUID] = set()
    with factory() as session, session.begin():
        for asset_id, symbol in members(session, current=True):
            if asset_id in seen:
                continue
            seen.add(asset_id)
            ident = identity_of(session, asset_id)
            if ident is not None:
                # Delisting recorded with the identity from its last traded bar; a history
                # that stops trading and resumes later (the provider chains E.I. du Pont to
                # REDACTED_FOR_GITHUB
                out["identity_records"] += 1
                stop = first_long_stop(session, asset_id)
                if stop is not None and (ident.delisted_on is None or stop < ident.delisted_on):
                    session.add(
                        _revise(
                            ident,
                            delisted_on=stop,
                            last_bar=stop,
                            delisting_basis=(
                                f"no trade for {MIN_STOP_SESSIONS}+ sessions after {stop} "
                                "(missing or zero-volume bars); the history resumes later under "
                                "the provider's chain, which is not this security's trading"
                            ),
                        )
                    )
                    out["identity_stops"] = out.get("identity_stops", 0) + 1
                continue
            sec = _newest(session, symbol)
            if sec is None:
                continue
            cutoff = era_end(sec)
            last = session.scalar(
                select(func.max(b.event_time)).where(
                    b.asset_id == asset_id,
                    b.feed == "sip",
                    b.timeframe == "1Day",
                    b.adjustment == "RAW",
                    b.quality_status == "VALID",
                    b.volume > 0,
                    b.event_time
                    <= datetime(cutoff.year, cutoff.month, cutoff.day, 23, 59, 59, tzinfo=UTC),
                )
            )
            if last is not None and sec.delisted_on == last.astimezone(sources.EASTERN).date():
                continue  # already recorded by an earlier pass
            if last is None:
                out["no_bars"] += 1
                continue
            last_day = last.astimezone(sources.EASTERN).date()
            if last_day >= FINAL_WINDOW and cutoff == BARS_END:
                out["trading_at_end"] += 1
                continue
            reuse = "" if cutoff == BARS_END else f"; the ticker was reused after {cutoff}"
            session.add(
                _revise(
                    sec,
                    last_bar=last_day,
                    delisted_on=last_day,
                    delisting_basis=(
                        f"no traded SIP bar (volume > 0) after {last_day.isoformat()} to "
                        f"{BARS_END.isoformat()}{reuse}; provider status "
                        f"{sec.provider_status}; reason unknown (merger, acquisition, "
                        "bankruptcy or symbol change are not distinguished)"
                    ),
                )
            )
            out["delisted"] += 1
    return out


__all__ = ["BARS_END", "BARS_START", "era_end", "infer_delistings", "load", "members"]
