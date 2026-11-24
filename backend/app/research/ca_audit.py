"""Corporate-action integrity audit of the reserved research data (read only).

Scans every current member of equity V4 and ETF V5 over the whole loaded window
(2016-2019, lookback included) for corporate-action defects the replay would consume, and
cross-checks each event against the provider's own corporate-action history. Nothing is
written to the database and nothing is corrected: the output is a JSON artifact for review.

What the replay consumes (``backtesting.data`` / ``engine`` / ``dividends``), and so what a
defect can distort:

* **holdings P&L** - positions are valued at RAW closes; a change of RAW/SPLIT between two
  sessions is an inferred split that rescales held quantities (``engine._split``); cash
  dividends are credited at the ex-date when stored under the run's asset symbol;
* **features** - indicators, stops and targets read the split-adjusted series rescaled
  point-in-time, which carries every inferred split but no dividend or spin-off;
* **liquidity gate** - median RAW dollar volume (adjustment invariant).

Per traded session pair (prev, day) of an asset:

* ``holder_return`` = (RAW close x inferred split ratio + credited dividend) / RAW prev - 1:
  what a replay position earns;
* ``feature_return`` = SPLIT close / SPLIT prev - 1: what an indicator sees;
* ``excess`` = the move minus SPY's move that day (market-wide days are not defects).

Detectors (thresholds are review triggers, not corrections):

* every split the replay would infer, graded against the RAW move;
* large excess holder moves without an inferred split;
* ratio-shaped RAW moves (about 1/k or k) without an inferred split: unrecorded splits and
  ADR ratio changes;
* every provider corporate action other than a regular dividend (spin-offs, mergers, splits,
  name changes...) and the data's treatment of it;
* large dividends (>= 5% of the prior close) under any ticker the security used, and whether
  the replay would credit them;
* identity seams: long interior trading gaps and volume-regime breaks at a large move;
* gap-downs with a new listing whose name shares a word with the parent's (heuristic for
  unrecorded spin-offs).

Each event is placed against the asset's member spans: inside a span; inside the default or
the registry-maximum lookback before a span; after a span while a position could still be
held (leaving the universe does not close a position); or unreachable (after a recorded
delisting).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import statistics
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.data import sessions_between, snap_ratio
from app.database.models import (
    Asset,
    CashDividendRecord,
    CashDividendRetrievalRecord,
    MarketBarRecord,
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
)
from app.research import sources, store
from app.research.bars import BARS_END, BARS_START, END_TS, START_TS, identity_of
from app.research.identity import MAPPED_ASOF
from app.research.universe import CURRENT_UNIVERSES

ACTIONS_START, ACTIONS_END = date(2015, 6, 1), date(2020, 1, 31)
STREAM_END = date(2026, 11, 1)  # dividend streams followed across later renames
CURRENT_HOLDER_ASOF = MAPPED_ASOF.isoformat()  # today's holder of a ticker (identity)
INFER = 0.01  # validation's ratio-change trigger (the engine's is 2%)
ENGINE_INFER = 0.02  # backtesting.data.SPLIT_THRESHOLD
JUMP = 0.20  # excess holder or feature move reviewed
SPIN_GAP = 0.08  # excess gap-down reviewed for a same-name new listing
BIG_DIVIDEND = 0.05  # dividend / prior close
RATIO_TOL = 0.04
RATIOS = (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 1.5, 4 / 3, 2.5)
INTERIOR_GAP = 10  # sessions without a traded bar inside an asset's traded range
QUIET = 0.10  # excess holder move reviewed when volume is ordinary...
QUIET_VOLUME = 2.0  # ...below twice the prior 20-session median (mechanical signature)
GAP_DOWN = 0.10  # excess opening gap matched against dividends under any ticker
DIVIDEND_MATCH = (0.6, 1.4)  # dividend / opening gap, for a match
STOP_WORDS = {
    "inc",
    "corp",
    "co",
    "company",
    "corporation",
    "holdings",
    "group",
    "the",
    "plc",
    "ltd",
    "class",
    "common",
    "stock",
    "shares",
    "new",
    "international",
    "trust",
    "fund",
    "etf",
    "and",
    "of",
    "de",
    "sa",
    "nv",
    "ag",
    "adr",
    "american",
    "depositary",
    "lp",
    "llc",
    "research",
    "identity",
    "as",
    "ticker",
    "since",
    "reused",
}


def _day(at: datetime) -> date:
    return at.astimezone(sources.EASTERN).date()


def _words(name: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (name or "").lower()) if len(w) > 2} - STOP_WORDS


# ------------------------------------------------------------------ provider history
def provider_actions(symbols: list[str]) -> dict[str, Any]:
    """Every corporate action the provider lists for ``symbols`` over the audit range (GET
    only, all types, by calendar year to keep requests small). Returned as recorded."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    requests = 0
    failures: list[dict[str, Any]] = []
    with sources.alpaca_client() as client:
        for i in range(0, len(symbols), 50):
            batch = symbols[i : i + 50]
            for year in range(ACTIONS_START.year, ACTIONS_END.year + 1):
                start = max(ACTIONS_START, date(year, 1, 1))
                end = min(ACTIONS_END, date(year, 12, 31))
                token = None
                while True:
                    params = {
                        "symbols": ",".join(batch),
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "limit": "1000",
                    }
                    if token:
                        params["page_token"] = token
                    try:
                        r = sources.get(
                            client, f"{sources.DATA_API_BASE_URL}/v1/corporate-actions", params
                        )
                    except Exception as exc:  # recorded, not hidden
                        failures.append(
                            {"symbols": batch, "year": year, "error": type(exc).__name__}
                        )
                        break
                    requests += 1
                    body = r.json()
                    for typ, rows in (body.get("corporate_actions") or {}).items():
                        for a in rows:
                            out[typ].append(
                                {k: v for k, v in a.items() if k != "id"} | {"id": a.get("id")}
                            )
                    token = body.get("next_page_token")
                    if not token:
                        break
    return {
        "endpoint": "/v1/corporate-actions",
        "range": [ACTIONS_START.isoformat(), ACTIONS_END.isoformat()],
        "symbols_requested": len(symbols),
        "requests": requests,
        "failures": failures,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "actions": {
            k: sorted(v, key=lambda a: (a.get("ex_date") or "", str(a.get("id"))))
            for k, v in sorted(out.items())
        },
    }


def _symbols_of(action: dict[str, Any]) -> set[str]:
    return {
        str(action[k])
        for k in (
            "symbol",
            "source_symbol",
            "target_symbol",
            "acquirer_symbol",
            "acquiree_symbol",
            "old_symbol",
            "new_symbol",
        )
        if action.get(k)
    }


def _action_day(action: dict[str, Any]) -> date | None:
    for k in ("ex_date", "effective_date", "process_date"):
        if action.get(k):
            return date.fromisoformat(str(action[k]))
    return None


class ProviderCache:
    """GET-only provider look-ups made during the scan, cached and saved as evidence:
    cash dividends of all symbols over a date range, cash dividends of one ticker, and one
    ticker's RAW close on a session as of that session (identity confirmation)."""

    def __init__(self, data: dict[str, Any] | None = None, *, offline: bool = False) -> None:
        self.data: dict[str, Any] = data or {"dividends_between": {}, "ticker": {}, "close": {}}
        self.offline = offline
        self.requests = 0
        self._client: Any = None

    def _get(self, params: dict[str, str]) -> list[dict[str, Any]]:
        if self.offline:
            raise LookupError("not in the saved provider evidence")
        if self._client is None:
            self._client = sources.alpaca_client()
        rows: list[dict[str, Any]] = []
        token = None
        while True:
            p = dict(params, **({"page_token": token} if token else {}))
            r = sources.get(self._client, f"{sources.DATA_API_BASE_URL}/v1/corporate-actions", p)
            self.requests += 1
            body = r.json()
            for a in (body.get("corporate_actions") or {}).get("cash_dividends", []):
                rows.append(
                    {k: a.get(k) for k in ("symbol", "ex_date", "rate", "special", "foreign")}
                )
            token = body.get("next_page_token")
            if not token:
                return rows

    def dividends_between(self, start: date, end: date) -> list[dict[str, Any]]:
        key = f"{start.isoformat()}|{end.isoformat()}"
        if key not in self.data["dividends_between"]:
            self.data["dividends_between"][key] = self._get(
                {
                    "types": "cash_dividend",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "limit": "1000",
                }
            )
        rows: list[dict[str, Any]] = self.data["dividends_between"][key]
        return rows

    def ticker_dividends(self, ticker: str) -> list[dict[str, Any]]:
        if ticker not in self.data["ticker"]:
            self.data["ticker"][ticker] = self._get(
                {
                    "symbols": ticker,
                    "types": "cash_dividend",
                    "start": ACTIONS_START.isoformat(),
                    "end": STREAM_END.isoformat(),
                    "limit": "1000",
                }
            )
        rows: list[dict[str, Any]] = self.data["ticker"][ticker]
        return rows

    def close(self, ticker: str, day: date) -> float | None:
        key = f"{ticker}|{day.isoformat()}"
        if key not in self.data["close"]:
            if self.offline:
                raise LookupError("not in the saved provider evidence")
            if self._client is None:
                self._client = sources.alpaca_client()
            self.requests += 1
            got = [
                float(bar["c"])
                for sym, bar in sources.daily_bars(self._client, [ticker], day, day, asof=day)
                if sym == ticker
            ]
            self.data["close"][key] = got[0] if got else None
        value: float | None = self.data["close"][key]
        return value

    def closes(self, tickers: list[str], day: date) -> dict[str, float | None]:
        """RAW closes of many tickers on one session, each as of that session (one provider
        request per 100 tickers). A ticker the batch maps to an already-returned security
        may come back empty: a missed match, never a false one."""
        todo = sorted({t for t in tickers if f"{t}|{day.isoformat()}" not in self.data["close"]})
        if todo:
            if self.offline:
                raise LookupError("not in the saved provider evidence")
            if self._client is None:
                self._client = sources.alpaca_client()
            for i in range(0, len(todo), 100):
                batch = todo[i : i + 100]
                got: dict[str, float] = {}
                for sym, bar in sources.daily_bars(self._client, batch, day, day, asof=day):
                    got.setdefault(sym, float(bar["c"]))
                self.requests += 1
                for t in batch:
                    self.data["close"][f"{t}|{day.isoformat()}"] = got.get(t)
        return {t: self.data["close"][f"{t}|{day.isoformat()}"] for t in tickers}

    def shut(self) -> None:
        if self._client is not None:
            self._client.close()


# Author-compiled reference list of 2016-2019 corporate events of V4 parents (public
# context, not provider data), used only to measure what the detectors find. Each date is an
# approximate window centre (+/- 7 calendar days); the event session and every number are
# measured from the stored bars, never taken from this list.
REFERENCE_EVENTS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


SAME_CLOSE = 0.002
CONFIRM_OFFSETS = (5, 21, 63)  # further sessions back that must match too
MIN_SAME_SESSIONS = 3  # at least three equal closes, four when the history allows


def same_security(
    cache: ProviderCache, candidates: list[str], closes: dict[date, float], probe: date
) -> str | None:
    """The one candidate ticker whose RAW close, as of each session, equals the asset's on
    ``probe`` and 5, 21 and 63 sessions earlier, where the asset has bars then: four equal
    closes over three months, at least three near the start of the history. That identifies
    the same security under another ticker; two closes were not enough (the audit's second
    REDACTED_FOR_GITHUB"""
    days = sorted(closes)
    if probe not in closes or not candidates:
        return None
    got = cache.closes(candidates, probe)
    hits = [t for t, v in got.items() if v is not None and abs(v / closes[probe] - 1) < SAME_CLOSE]
    i = days.index(probe)
    matched = 1
    for back in CONFIRM_OFFSETS:
        if not hits:
            return None
        if i < back:
            continue  # before the asset's first bar: fewer sessions to compare
        earlier = days[i - back]
        again = cache.closes(hits, earlier)
        hits = [
            t
            for t in hits
            if (v := again.get(t)) is not None and abs(v / closes[earlier] - 1) < SAME_CLOSE
        ]
        matched += 1
    return hits[0] if len(hits) == 1 and matched >= MIN_SAME_SESSIONS else None


def former_tickers(
    closes: dict[date, float],
    stream: list[tuple[date, float]],
    direction: str,
    first: date,
    last: date,
    cache: ProviderCache,
    exclude: set[str],
) -> list[dict[str, Any]]:
    """Tickers the security paid dividends under before (``BEFORE``) or after (``AFTER``)
    its credited stream, inside [first, last]: candidates paying a similar rate one interval
    beyond the stream's edge, each confirmed only if its RAW close as of a session equals
    this asset's RAW close that session (same security, other ticker). Followed up to three
    renames."""
    chain: list[dict[str, Any]] = []
    seen = set(exclude)
    days = sorted(closes)
    for _ in range(3):
        if not stream:
            return chain
        edge = stream[0] if direction == "BEFORE" else stream[-1]
        intervals = [(b[0] - a[0]).days for a, b in itertools.pairwise(stream)]
        typical = int(statistics.median(intervals)) if intervals else 91
        room = (edge[0] - first).days if direction == "BEFORE" else (last - edge[0]).days
        if room <= 1.5 * typical + 10:
            return chain
        half = 25
        if direction == "BEFORE" and edge[0] > last:
            # The stream starts after the bars: search the last full quarter of the bars.
            centre, half = last - timedelta(days=60), 50
        else:
            centre = edge[0] + timedelta(days=-typical if direction == "BEFORE" else typical)
        rows = cache.dividends_between(centre - timedelta(days=half), centre + timedelta(days=half))
        start = centre - timedelta(days=half)
        probe_days = [d for d in days if first <= d <= last and d < start]
        if not probe_days:
            return chain
        p = probe_days[-1]
        cands = sorted({str(r["symbol"]) for r in rows} - seen)
        seen.update(cands)
        hit = same_security(cache, cands, closes, p)
        found = None if hit is None else (hit, p, closes[p])
        if found is None:
            return chain
        ticker = found[0]
        theirs = sorted(
            (date.fromisoformat(x["ex_date"]), float(x["rate"]))
            for x in cache.ticker_dividends(ticker)
            if x.get("ex_date")
        )
        chain.append(
            {
                "ticker": ticker,
                "direction": direction,
                "confirmed_on": found[1].isoformat(),
                "close": found[2],
                "dividends": theirs,
            }
        )
        stream = (
            [x for x in theirs if x[0] < edge[0]]
            if direction == "BEFORE"
            else [x for x in theirs if x[0] > edge[0]]
        )
    return chain


# Candidate corrections, each tested against the data: what one parent share became on the
# session (the parent itself after any consolidation, plus distributed shares or cash).
# Quantities are public context (author-compiled) except where a provider record is cited;
# a reconciliation passes only if the components, priced from the provider's own bars,
# account for the prior close (within RECONCILE_TOL of the market's move).
RECONCILIATIONS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
RECONCILE_TOL = 0.05


def reconcile(
    factory: sessionmaker[Session], cache: ProviderCache, entries: tuple[dict[str, Any], ...]
) -> list[dict[str, Any]]:
    """For each candidate correction: the replay's holder return on the session versus the
    return the components imply, priced at the session's RAW closes (children from the
    provider, as of the session). ``artificial`` is the replay's error the correction would
    remove; nothing is corrected."""
    out: list[dict[str, Any]] = []
    with factory() as session:
        members = load_members(session, datetime.now(UTC))
        by_label = {lab: a for a, m in members.items() for lab in m.labels}
        ids = [by_label[e["label"]] for e in entries if e["label"] in by_label]
        bars = load_bars(session, ids)
        spy = next((a for a, m in members.items() if "SPY" in m.labels), None)
        spy_bars = load_bars(session, [spy])[spy] if spy else None
    for e in entries:
        row: dict[str, Any] = {k: v for k, v in e.items()}
        aid = by_label.get(e["label"])
        day = date.fromisoformat(e["session"])
        if aid is None or day not in bars[aid].raw:
            out.append(row | {"status": "NO_PARENT_BAR"})
            continue
        bs = bars[aid]
        prev = max(d for d in bs.raw if d < day)
        pc, close = bs.raw[prev][1], bs.raw[day][1]
        change = (pc / bs.split[prev]) / (close / bs.split[day])
        engine_ratio = (
            float(snap_ratio(Decimal(str(change)))) if abs(change - 1) > ENGINE_INFER else 1.0
        )
        replay = close * engine_ratio / pc - 1
        priced = []
        value = close * float(e["parent_shares"]) + float(e.get("cash", 0.0))
        missing = False
        for ticker, qty in e["shares"]:
            px = cache.close(ticker, day)
            priced.append({"ticker": ticker, "per_parent_share": qty, "close": px})
            if px is None:
                missing = True
            else:
                value += qty * px
        mkt = None
        if spy_bars is not None and day in spy_bars.raw:
            sprev = max(d for d in spy_bars.raw if d < day)
            mkt = spy_bars.raw[day][1] / spy_bars.raw[sprev][1] - 1
        implied = value / pc - 1
        status = (
            "MISSING_COMPONENT_PRICE"
            if missing
            else "RECONCILED"
            if abs(implied - (mkt or 0.0)) <= RECONCILE_TOL
            else "NOT_RECONCILED"
        )
        out.append(
            row
            | {
                "asset_id": str(aid),
                "previous_session": prev.isoformat(),
                "raw_previous": pc,
                "raw_close": close,
                "replay_engine_ratio": engine_ratio,
                "replay_holder_return": round(replay, 4),
                "components": priced,
                "implied_holder_return": round(implied, 4),
                "market_return": None if mkt is None else round(mkt, 4),
                "artificial_return": round(replay - implied, 4),
                "status": status,
            }
        )
    return out


# Identity probes: the asset's RAW close on a session versus named tickers priced as of that
# session. Equal closes identify which security the asset's history carries that day.
IDENTITY_PROBES = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def identity_probes(factory: sessionmaker[Session], cache: ProviderCache) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with factory() as session:
        members = load_members(session, datetime.now(UTC))
        by_label = {lab: a for a, m in members.items() for lab in m.labels}
        bars = load_bars(session, [by_label[x[0]] for x in IDENTITY_PROBES if x[0] in by_label])
    for label, day_s, tickers in IDENTITY_PROBES:
        day = date.fromisoformat(day_s)
        aid = by_label.get(label)
        own = bars[aid].raw.get(day) if aid else None
        rows = []
        for t in tickers:
            err: str | None = None
            try:
                px = cache.close(t, day)
            except Exception as exc:  # recorded, not hidden
                px, err = None, type(exc).__name__
            rows.append(
                {
                    "ticker_as_of_session": t,
                    "close": px,
                    "equals_asset_close": None
                    if px is None or own is None
                    else abs(px / own[1] - 1) < 0.002,
                    "error": err,
                }
            )
        out.append(
            {"label": label, "session": day_s, "asset_close": own[1] if own else None,
             "probes": rows}
        )  # fmt: skip
    return out


def classify_reference(
    rows: list[tuple[date, float, float, float, float | None, float | None]], centre: date
) -> dict[str, Any]:
    """The data's treatment of one reference event: the session in the window with an
    inferred split (nearest the centre), else the largest excess holder move."""
    window = [r for r in rows if abs((r[0] - centre).days) <= 7]
    if not window:
        return {"treatment": "NO_BARS_IN_WINDOW"}
    splits = [r for r in window if r[4] is not None]
    if splits:
        day, holder, feature, raw, ratio, dev = min(splits, key=lambda r: abs((r[0] - centre).days))
        dev = dev or 0.0
        phantom = not 2 / 3 <= 1 + dev <= 3 / 2
        treatment = (
            "PHANTOM_SPLIT"
            if phantom
            else "ENCODED_AS_SPLIT_VALUE_PRESERVING"
            if abs(dev) <= 0.10
            else "ENCODED_AS_SPLIT_MISPRICED"
        )
    else:
        day, holder, feature, raw, ratio, dev = max(window, key=lambda r: abs(r[1]))
        treatment = (
            "UNADJUSTED_DISCONTINUITY" if abs(holder) >= 0.05 else "NO_VISIBLE_DISCONTINUITY"
        )
    return {
        "treatment": treatment,
        "session": day.isoformat(),
        "holder_excess": holder,
        "feature_excess": feature,
        "raw_move": raw,
        "inferred_ratio": ratio,
        "implied_raw_deviation": dev,
    }


# ------------------------------------------------------------------ assets and bars
class Member:
    def __init__(self, asset_id: uuid.UUID) -> None:
        self.asset_id = asset_id
        self.symbol = ""  # the replay's asset symbol (dividend join key)
        self.name = ""
        self.labels: set[str] = set()
        self.universes: set[str] = set()
        self.spans: list[tuple[date, date, str, str]] = []
        self.request_symbol: str | None = None
        self.asof: str | None = None
        self.delisted_on: date | None = None
        self.excluded: list[str] = []
        self.tickers: set[str] = set()

    def ref(self) -> dict[str, Any]:
        return {
            "asset_id": str(self.asset_id),
            "asset_symbol": self.symbol,
            "labels": sorted(self.labels),
            "universes": sorted(self.universes),
            "request": [self.request_symbol, self.asof],
            "delisted_on": self.delisted_on.isoformat() if self.delisted_on else None,
            "x1_excluded": bool(self.excluded),
        }


def load_members(session: Session, known_at: datetime) -> dict[uuid.UUID, Member]:
    c = ResearchUniverseCandidateRecord
    out: dict[uuid.UUID, Member] = {}
    for asset_id, sym, code, f, t in session.execute(
        select(c.asset_id, c.symbol, c.universe_code, c.effective_from, c.effective_to).where(
            c.member.is_(True), c.universe_code.in_(CURRENT_UNIVERSES)
        )
    ):
        if asset_id is None:
            continue
        m = out.setdefault(asset_id, Member(asset_id))
        m.labels.add(sym)
        m.universes.add(code)
        if f is not None and t is not None:
            m.spans.append((f, t, code, sym))
    excluded = store.exclusions(session, known_at=known_at)
    delisted = store.delistings(session, list(out), known_at=known_at)
    for asset_id, m in out.items():
        a = session.get(Asset, asset_id)
        m.symbol = a.symbol if a else ""
        m.name = a.name if a and a.name else ""
        ident = identity_of(session, asset_id)
        if ident is not None:
            d = ident.details or {}
            m.request_symbol, m.asof = d.get("request_symbol"), d.get("asof")
        m.delisted_on = delisted.get(asset_id)
        m.excluded = excluded.get(asset_id, [])
        m.tickers = {s.split("~")[0] for s in m.labels | {m.symbol}} | (
            {m.request_symbol} if m.request_symbol else set()
        )
        m.spans.sort()
    return out


class Bars:
    """Traded (volume > 0) VALID SIP daily bars of one asset, RAW and SPLIT, 2016-2019."""

    def __init__(self) -> None:
        self.raw: dict[date, tuple[float, float, float]] = {}  # open, close, volume
        self.split: dict[date, float] = {}


def load_bars(session: Session, asset_ids: list[uuid.UUID]) -> dict[uuid.UUID, Bars]:
    b = MarketBarRecord
    out: dict[uuid.UUID, Bars] = defaultdict(Bars)
    for i in range(0, len(asset_ids), 100):
        for asset_id, adj, at, o, c, v in session.execute(
            select(b.asset_id, b.adjustment, b.event_time, b.open, b.close, b.volume)
            .where(
                b.asset_id.in_(asset_ids[i : i + 100]),
                b.feed == "sip",
                b.timeframe == "1Day",
                b.quality_status == "VALID",
                b.volume > 0,
                b.event_time >= START_TS,
                b.event_time <= END_TS,
            )
            .order_by(b.id)
        ):
            if c is None:
                continue
            d = _day(at)
            if adj == "RAW":
                out[asset_id].raw.setdefault(
                    d, (float(o if o is not None else c), float(c), float(v or 0))
                )
            elif adj == "SPLIT":
                out[asset_id].split.setdefault(d, float(c))
    return out


# ------------------------------------------------------------------ reach
def reach(m: Member, day: date, bounds: dict[str, int], index: dict[date, int]) -> dict[str, Any]:
    """Where an event sits relative to the asset's member spans (in sessions)."""
    in_span = [f"{code}:{lab}" for f, t, code, lab in m.spans if f <= day <= t]
    ahead = [
        index[f] - index[day]
        for f, _t, _c, _l in m.spans
        if f > day and f in index and day in index
    ]
    behind = [
        index[day] - index[t]
        for _f, t, _c, _l in m.spans
        if t < day and t in index and day in index
    ]
    before_start = min(ahead) if ahead else None
    after_end = min(behind) if behind else None
    unreachable = m.delisted_on is not None and day > m.delisted_on
    if unreachable:
        where = "UNREACHABLE_AFTER_DELISTING"
    elif in_span:
        where = "IN_SPAN"
    elif before_start is not None and before_start <= bounds["default"]:
        where = "LOOKBACK_DEFAULT"
    elif before_start is not None and before_start <= bounds["registry_max"]:
        where = "LOOKBACK_LONG_PARAMETERS"
    elif after_end is not None:
        where = "AFTER_SPAN_HELD_POSITION_ONLY"
    else:
        where = "OUT_OF_REACH"
    return {
        "where": where,
        "in_spans": in_span,
        "sessions_before_next_span_start": before_start,
        "sessions_after_previous_span_end": after_end,
    }


# ------------------------------------------------------------------ the scan
def _ratio_shaped(move: float) -> float | None:
    x = 1 + move
    for k in RATIOS:
        for target in (1 / k, k):
            if abs(x / target - 1) <= RATIO_TOL:
                return target
    return None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def scan(
    factory: sessionmaker[Session],
    snapshot: dict[str, Any],
    bounds: dict[str, int],
    cache: ProviderCache,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    with factory() as session:
        members = load_members(session, now)
        bars = load_bars(session, list(members))
        # The replay's dividend credit: stored under the asset symbol, retrieval covering.
        r = CashDividendRetrievalRecord
        covered = {
            s
            for s in session.scalars(
                select(r.symbol).where(
                    r.status == "OK", r.range_start <= BARS_START, r.range_end >= BARS_END
                )
            )
        }
        d = CashDividendRecord
        stored: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
        stored_all: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
        newest: dict[str, CashDividendRecord] = {}
        for row in session.scalars(
            select(d)
            .where(d.ex_date >= ACTIONS_START, d.ex_date <= STREAM_END)
            .order_by(d.provider_action_id, d.revision)
        ):
            newest[row.provider_action_id] = row
        for row in newest.values():
            stored_all[row.symbol][row.ex_date] += float(row.rate)
            if BARS_START <= row.ex_date <= BARS_END:
                stored[row.symbol][row.ex_date] += float(row.rate)
        # Security master new listings (for the spin-off heuristic).
        s = ResearchSecurityRecord
        listings: dict[date, list[tuple[str, str]]] = defaultdict(list)
        for sym, name, first in session.execute(
            select(s.symbol, s.name, s.first_bar).where(
                s.provider == "alpaca", s.first_bar.is_not(None), s.first_bar > BARS_START
            )
        ):
            if first is not None:
                listings[first].append((sym, name or ""))
        spy = next((a for a, m in members.items() if "SPY" in m.labels), None)

    sessions = [x.day for x in sessions_between(BARS_START, BARS_END)]
    index = {dd: i for i, dd in enumerate(sessions)}
    market: dict[date, float] = {}
    if spy is not None:
        spy_raw = bars[spy].raw
        days = sorted(spy_raw)
        for prev, day in itertools.pairwise(days):
            div = stored["SPY"].get(day, 0.0)
            market[day] = (spy_raw[day][1] + div) / spy_raw[prev][1] - 1

    # Provider actions by ticker, regular cash dividends separated.
    by_ticker: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    provider_dividends: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for typ, rows in snapshot["actions"].items():
        for a in rows:
            if typ == "cash_dividends":
                ex = _action_day(a)
                if ex is not None:
                    provider_dividends[str(a["symbol"])][ex] += float(a["rate"])
                continue
            for sym in _symbols_of(a):
                by_ticker[sym].append((typ, a))

    events: list[dict[str, Any]] = []
    splits_seen = 0
    seams: list[dict[str, Any]] = []
    series: dict[uuid.UUID, list[tuple[date, float, float, float, float | None, float | None]]]
    series = defaultdict(list)
    gap_downs: list[tuple[uuid.UUID, date, float, float, float, float]] = []
    for asset_id, m in sorted(members.items(), key=lambda x: x[1].symbol):
        bs = bars.get(asset_id)
        if bs is None or not bs.raw:
            continue
        days = sorted(dd for dd in bs.raw if dd in bs.split and bs.split[dd] > 0)
        credited = stored.get(m.symbol, {}) if m.symbol in covered else {}
        any_div: dict[date, list[tuple[str, float, str]]] = defaultdict(list)
        for t in m.tickers:
            for ex, rate in stored.get(t, {}).items():
                any_div[ex].append((t, rate, "stored"))
            for ex, rate in provider_dividends.get(t, {}).items():
                if not any(x[0] == t and abs(x[1] - rate) < 1e-9 for x in any_div[ex]):
                    any_div[ex].append((t, rate, "provider_only"))
        actions = [(typ, a) for t in m.tickers for typ, a in by_ticker.get(t, [])]
        vols = [bs.raw[dd][2] for dd in days]
        # Interior trading gaps (identity seam candidates).
        for prev, day in itertools.pairwise(days):
            if prev in index and day in index and index[day] - index[prev] - 1 >= INTERIOR_GAP:
                seams.append(
                    {
                        **m.ref(),
                        "kind": "INTERIOR_GAP",
                        "from": prev.isoformat(),
                        "to": day.isoformat(),
                        "missing_sessions": index[day] - index[prev] - 1,
                        "raw_close_before": bs.raw[prev][1],
                        "raw_close_after": bs.raw[day][1],
                        "reach": reach(m, day, bounds, index),
                    }
                )
        for k, (prev, day) in enumerate(itertools.pairwise(days), start=1):
            o, c, v = bs.raw[day]
            pc = bs.raw[prev][1]
            sp, sc = bs.split[prev], bs.split[day]
            change = (pc / sp) / (c / sc)  # new shares per old if a split took effect
            inferred = abs(change - 1) > INFER
            engine_ratio = (
                float(snap_ratio(Decimal(str(change)))) if abs(change - 1) > ENGINE_INFER else 1.0
            )
            div_credit = credited.get(day, 0.0)
            raw_move = c / pc - 1
            holder = (c * engine_ratio + div_credit) / pc - 1
            feature = sc / sp - 1
            mkt = market.get(day, 0.0)
            gap = o / pc - 1
            divs = any_div.get(day, [])
            div_total = max((x[1] for x in divs), default=0.0)
            near = [
                (typ, a)
                for typ, a in actions
                if (ad := _action_day(a)) is not None and abs((ad - day).days) <= 3
            ]
            reasons = []
            if inferred:
                splits_seen += 1
                reasons.append("INFERRED_SPLIT")
            if abs(holder - mkt) > JUMP:
                reasons.append("HOLDER_JUMP")
            if abs(feature - mkt) > JUMP and not inferred:
                reasons.append("FEATURE_JUMP")
            shaped = _ratio_shaped(raw_move) if abs(raw_move) > 0.3 and not inferred else None
            if shaped is not None:
                reasons.append("RATIO_SHAPED")
            if div_total / pc >= BIG_DIVIDEND:
                reasons.append("LARGE_DIVIDEND")
            if any(
                typ != "cash_dividends" and prev < (_action_day(a) or prev) <= day
                for typ, a in near
            ):
                reasons.append("PROVIDER_ACTION")
            med_vol = _median(vols[max(0, k - 21) : k - 1])
            vol_ratio = v / med_vol if med_vol else None
            if abs(holder - mkt) > QUIET and (vol_ratio is None or vol_ratio < QUIET_VOLUME):
                reasons.append("QUIET_JUMP")
            series[asset_id].append(
                (day, round(holder - mkt, 4), round(feature - mkt, 4), round(raw_move, 4),
                 round(change, 6) if inferred else None,
                 round(c / (pc / change) - 1, 4) if inferred else None)
            )  # fmt: skip
            if not inferred and div_credit == 0 and gap - mkt <= -GAP_DOWN:
                gap_downs.append((asset_id, day, pc, o, c, round(gap - mkt, 4)))
            spin_children: list[tuple[str, str]] = []
            if gap - mkt < -SPIN_GAP and not inferred:
                parent = _words(m.name)
                for back in range(0, 4):
                    j = index.get(day)
                    if j is None or j - back < 0:
                        continue
                    for sym, nm in listings.get(sessions[j - back], []):
                        if parent & _words(nm):
                            spin_children.append((sym, nm))
                if spin_children:
                    reasons.append("SPIN_HEURISTIC")
            if not reasons:
                continue
            before = vols[max(0, k - 21) : k - 1]
            after = vols[k : k + 20]
            events.append(
                {
                    **m.ref(),
                    "session": day.isoformat(),
                    "previous_session": prev.isoformat(),
                    "reasons": reasons,
                    "raw_previous": pc,
                    "raw_open": o,
                    "raw_close": c,
                    "raw_move": round(raw_move, 4),
                    "raw_gap": round(gap, 4),
                    "split_previous": sp,
                    "split_close": sc,
                    "feature_return": round(feature, 4),
                    "inferred_ratio": round(change, 6) if inferred else None,
                    "engine_ratio": engine_ratio,
                    "implied_raw_deviation": round(c / (pc / change) - 1, 4) if inferred else None,
                    "dividend_credited": div_credit,
                    "dividends_any_ticker": divs,
                    "holder_return": round(holder, 4),
                    "market_return": round(mkt, 4),
                    "holder_excess": round(holder - mkt, 4),
                    "feature_excess": round(feature - mkt, 4),
                    "ratio_shape": shaped,
                    "volume": v,
                    "volume_vs_prior_20_median": round(v / med, 2)
                    if (med := _median(before))
                    else None,
                    "volume_regime_after_vs_before": round(a / b_, 2)
                    if (a := _median(after)) and (b_ := _median(before))
                    else None,
                    "provider_actions_within_3_days": [
                        {
                            "type": typ,
                            **{
                                kk: a.get(kk)
                                for kk in sorted(a)
                                if kk != "id" and a.get(kk) not in (None, "")
                            },
                        }
                        for typ, a in near
                    ],
                    "spin_heuristic_listings": spin_children[:5],
                    "reach": reach(m, day, bounds, index),
                }
            )
    # Provider non-dividend actions on member tickers, with the data's treatment.
    treatment: list[dict[str, Any]] = []
    for asset_id, m in members.items():
        bs = bars.get(asset_id)
        for t in sorted(m.tickers):
            for typ, a in by_ticker.get(t, []):
                ad = _action_day(a)
                if ad is None or not BARS_START <= ad <= BARS_END:
                    continue
                hit = [
                    e
                    for e in events
                    if e["asset_id"] == str(asset_id)
                    and abs((date.fromisoformat(e["session"]) - ad).days) <= 3
                ]
                treatment.append(
                    {
                        **m.ref(),
                        "ticker": t,
                        "type": typ,
                        "day": ad.isoformat(),
                        "action": {
                            kk: a.get(kk)
                            for kk in sorted(a)
                            if kk != "id" and a.get(kk) not in (None, "")
                        },
                        "has_bars_that_day": bool(bs and ad in bs.raw),
                        "flagged_events_within_3_days": [(e["session"], e["reasons"]) for e in hit],
                    }
                )
    # Opening gaps matched to a dividend the provider recorded under any ticker that day.
    other_ticker: list[dict[str, Any]] = []
    for asset_id, day, pc, o, c, excess in gap_downs:
        m = members[asset_id]
        drop = pc - o
        hits = [
            r
            for r in cache.dividends_between(day, day)
            if drop > 0
            and DIVIDEND_MATCH[0] <= float(r["rate"]) / drop <= DIVIDEND_MATCH[1]
            and float(r["rate"]) / pc >= BIG_DIVIDEND
        ]
        confirmed = None
        if hits:
            bs_closes = {dd: v[1] for dd, v in bars[asset_id].raw.items()}
            prev_days = [dd for dd in bs_closes if dd < day]
            if prev_days:
                confirmed = same_security(
                    cache, sorted({str(h["symbol"]) for h in hits}), bs_closes, max(prev_days)
                )
        if confirmed is not None:
            hits = [h for h in hits if h["symbol"] == confirmed]
            other_ticker.append(
                {
                    **m.ref(),
                    "session": day.isoformat(),
                    "raw_previous": pc,
                    "raw_open": o,
                    "raw_close": c,
                    "gap_excess": excess,
                    "provider_dividends_matching_gap": hits,
                    "payer_confirmed_same_security": confirmed,
                    "credited_by_replay": False,
                    "reach": reach(m, day, bounds, index),
                }
            )
    # Dividend identity: what the replay credits (asset symbol, covered) versus what the
    # security paid under every ticker it used inside its traded range.
    dividend_rows: list[dict[str, Any]] = []
    for asset_id, m in sorted(members.items(), key=lambda x: x[1].symbol):
        bs = bars.get(asset_id)
        if bs is None or not bs.raw:
            continue
        first, last = min(bs.raw), max(bs.raw)
        last_use = min(last, m.delisted_on) if m.delisted_on else last
        closes = {dd: v[1] for dd, v in bs.raw.items()}
        paid: list[tuple[date, float]] = (
            sorted(
                (ex, rt) for ex, rt in stored.get(m.symbol, {}).items() if first <= ex <= last_use
            )
            if m.symbol in covered
            else []
        )
        paid_days = {ex for ex, _ in paid}
        missed: dict[date, tuple[str, float, str]] = {}
        for t in sorted({x.split("~")[0] for x in m.labels} - {m.symbol.split("~")[0]}):
            for dd, r_ in stored.get(t, {}).items():
                if first <= dd <= last_use and dd not in paid_days:
                    missed.setdefault(dd, (t, r_, "stored_under_other_label"))
        stream = sorted(stored_all.get(m.symbol.split("~")[0], {}).items())
        if not paid and m.asof == CURRENT_HOLDER_ASOF:
            # A renamed company under today's ticker with nothing credited in 2016-2019:
            # REDACTED_FOR_GITHUB
            stream = [x for x in stream if x[0] > last_use] or [(STREAM_END, 1.0)]
        chains: list[dict[str, Any]] = []
        if stream:
            for direction in ("BEFORE", "AFTER"):
                part = [x for x in stream if (x[0] >= first if direction == "BEFORE" else True)]
                found = former_tickers(
                    closes, part, direction, first, last_use, cache, {m.symbol.split("~")[0]}
                )
                for link in found:
                    for dd, r_ in link["dividends"]:
                        if first <= dd <= last_use and dd not in paid_days:
                            missed.setdefault(dd, (link["ticker"], r_, "provider_former_ticker"))
                    chains.append({k: v for k, v in link.items() if k != "dividends"})
        if m.symbol not in covered or missed or chains:

            def prior_close(day: date, closes: dict[date, float] = closes) -> float | None:
                earlier = [x for x in closes if x < day]
                return closes[max(earlier)] if earlier else None

            sized = [(ex, info, prior_close(ex)) for ex, info in missed.items()]
            largest = max(
                (x for x in sized if x[2]), key=lambda x: x[1][1] / (x[2] or 1), default=None
            )
            dividend_rows.append(
                {
                    **m.ref(),
                    "retrieved_under_asset_symbol": m.symbol in covered,
                    "credited_2016_2019": len(paid),
                    "credited_per_share": round(sum(rt for _, rt in paid), 4),
                    "former_or_other_tickers": chains,
                    "missed": [
                        {"ex_date": dd.isoformat(), "ticker": t, "rate": r_, "source": src}
                        for dd, (t, r_, src) in sorted(missed.items())
                    ],
                    "missed_per_share": round(sum(x[1] for x in missed.values()), 4),
                    "largest_missed": None
                    if largest is None
                    else {
                        "ex_date": largest[0].isoformat(),
                        "ticker": largest[1][0],
                        "rate": largest[1][1],
                        "of_prior_close": round(largest[1][1] / (largest[2] or 1), 4),
                        "reach": reach(m, largest[0], bounds, index),
                    },
                    "missed_in_spans": sum(
                        1 for dd in missed if any(f <= dd <= t for f, t, _c, _l in m.spans)
                    ),
                }
            )
    # Detector coverage of the reference list.
    by_label: dict[str, uuid.UUID] = {}
    for asset_id, m in members.items():
        for lab in m.labels | {m.symbol}:
            by_label.setdefault(lab, asset_id)
    flagged = {(e["asset_id"], e["session"]): e["reasons"] for e in events}
    for match in other_ticker:
        flagged.setdefault((match["asset_id"], match["session"]), []).append(
            "DIVIDEND_OTHER_TICKER"
        )
    reference: list[dict[str, Any]] = []
    for label, approx, what in REFERENCE_EVENTS:
        entry: dict[str, Any] = {"label": label, "window_centre": approx, "public_context": what}
        hit_id = by_label.get(label)
        if hit_id is None:
            entry["treatment"] = "NOT_A_CURRENT_MEMBER"
        else:
            m = members[hit_id]
            got = classify_reference(series[hit_id], date.fromisoformat(approx))
            entry |= {**m.ref(), **got}
            if "session" in got:
                entry["detected_by"] = flagged.get((str(hit_id), got["session"]), [])
                entry["reach"] = reach(m, date.fromisoformat(got["session"]), bounds, index)
        reference.append(entry)
    return {
        "generated_at": now.isoformat(),
        "universes": list(CURRENT_UNIVERSES),
        "members": len(members),
        "members_with_bars": sum(1 for a in members if bars.get(a) and bars[a].raw),
        "x1_excluded_members": sum(1 for m in members.values() if m.excluded),
        "thresholds": {
            "inferred_split_ratio_change": INFER,
            "engine_split_threshold": ENGINE_INFER,
            "excess_jump": JUMP,
            "spin_gap": SPIN_GAP,
            "large_dividend": BIG_DIVIDEND,
            "ratio_shape_tolerance": RATIO_TOL,
            "interior_gap_sessions": INTERIOR_GAP,
        },
        "lookback_bounds": bounds,
        "market_series": "SPY RAW close + stored dividends" if spy else "missing",
        "inferred_splits": splits_seen,
        "events": events,
        "identity_seams": seams,
        "provider_action_treatment": treatment,
        "gap_downs_checked": len(gap_downs),
        "dividends_under_other_tickers_at_gaps": other_ticker,
        "dividend_identity": dividend_rows,
        "reference_events": reference,
    }


def run(
    factory: sessionmaker[Session],
    out: Path,
    snapshot_path: Path,
    lookups_path: Path,
    seed_path: Path | None = None,
) -> dict[str, Any]:
    """Fetch (or reuse) the provider snapshot and look-ups, scan, write ``out``.

    ``out`` must not exist. An existing snapshot or look-up file is reused as recorded (so
    a rerun reproduces the scan offline); a look-up file is written only when new."""
    from app.research import exclusions

    if out.exists():
        raise FileExistsError(out)
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text())
    else:
        with factory() as session:
            members = load_members(session, datetime.now(UTC))
        tickers = sorted({t for m in members.values() for t in m.tickers if t})
        snapshot = provider_actions(tickers)
        snapshot_path.write_text(json.dumps(snapshot, indent=1, default=str))
    lb = exclusions.lookback_bounds()
    bounds = {
        "default": int(lb["max_read_sessions_default_with_coordinator"]),
        "loaded": int(lb["loaded_sessions_bound"]),
        "registry_max": 400 + 60 + 10,
    }
    reuse = lookups_path.exists()
    seed = json.loads(seed_path.read_text()) if seed_path and not reuse else None
    cache = ProviderCache(json.loads(lookups_path.read_text()) if reuse else seed, offline=reuse)
    try:
        report = scan(factory, snapshot, bounds, cache)
    finally:
        cache.shut()
    if not reuse:
        lookups_path.write_text(json.dumps(cache.data, indent=1, default=str))
    report["provider_lookups"] = {
        "path": str(lookups_path),
        "sha256": hashlib.sha256(lookups_path.read_bytes()).hexdigest(),
        "requests_this_run": cache.requests,
        "reused": reuse,
        "seeded_from": str(seed_path) if seed else None,
    }
    report["provider_snapshot"] = {
        "path": str(snapshot_path),
        "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        "retrieved_at": snapshot.get("retrieved_at"),
        "action_counts": {k: len(v) for k, v in snapshot["actions"].items()},
        "failures": snapshot.get("failures"),
    }
    out.write_text(json.dumps(report, indent=1, default=str))
    return report


__all__ = [
    "RECONCILIATIONS",
    "REFERENCE_EVENTS",
    "ProviderCache",
    "classify_reference",
    "former_tickers",
    "identity_probes",
    "provider_actions",
    "reach",
    "reconcile",
    "run",
    "same_security",
    "scan",
]


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import sys

    from app.core.config import get_settings
    from app.database.sessions import build_engine, build_session_factory

    settings = get_settings()
    if "sim" not in settings.postgres_db:
        raise SystemExit("the audit reads the simulation database only")
    factory = build_session_factory(build_engine(settings))
    if sys.argv[1] in ("reconcile", "identity"):  # reconcile|identity OUT LOOKUPS
        out_path, lookups = Path(sys.argv[2]), Path(sys.argv[3])
        if out_path.exists():
            raise SystemExit(f"{out_path} exists")
        reuse = lookups.exists()
        cache = ProviderCache(json.loads(lookups.read_text()) if reuse else None, offline=reuse)
        try:
            rows = (
                reconcile(factory, cache, RECONCILIATIONS)
                if sys.argv[1] == "reconcile"
                else identity_probes(factory, cache)
            )
        finally:
            cache.shut()
        if not reuse:
            lookups.write_text(json.dumps(cache.data, indent=1, default=str))
        out_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "tolerance": RECONCILE_TOL,
                    "lookups": {
                        "path": str(lookups),
                        "sha256": hashlib.sha256(lookups.read_bytes()).hexdigest(),
                    },
                    sys.argv[1]: rows,
                },
                indent=1,
                default=str,
            )
        )
        for r in rows:
            print(json.dumps(r, default=str))
        raise SystemExit(0)
    seed_arg = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    result = run(factory, Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), seed_arg)
    print(
        json.dumps(
            {
                "members": result["members"],
                "events": len(result["events"]),
                "seams": len(result["identity_seams"]),
                "provider": result["provider_snapshot"]["action_counts"],
                "gap_matches": len(result["dividends_under_other_tickers_at_gaps"]),
                "dividend_identity_rows": len(result["dividend_identity"]),
                "lookups": result["provider_lookups"],
            }
        )
    )
