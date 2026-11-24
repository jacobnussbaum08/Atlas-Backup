"""X3.1 (D-244): historical dividend completeness and identity validation. Simulation
database only; GET-only provider requests; credentials never printed.

Step 1, :func:`retrieve_all`. The D-237 retrievals asked the provider for dividends over a
date range that the provider applies to the *processing* date, not the ex-date. Ex-dates whose
processing date fell after the range end were never returned: late December 2019 paid in
January, and any later payment. Every symbol of those retrievals is retrieved again in
ex-date terms (:func:`app.backtesting.dividends.retrieve`), and every response body is
saved. Stored rows are never changed: a dividend not seen before is appended, and a changed
value for a known provider id becomes a new revision (as in D-237).

Step 2, :func:`build`. Every V4 and V5 member's dividends, asset by asset:

* **Own-symbol dividends.** These are the provider rows the replay joins by symbol, going ex
  in 2016-01-05..2019-12-31. Each is verified on its own with X3's identity test
  (:func:`app.research.ca_dividends.verify`): the provider's close for the ticker, *as of the
  session before the ex-date*, must equal the asset's RAW close on that session and on the
  one before. Sessions come from the corrected calendar, in which 2018-12-05 is not a
  session.
* **Successor tickers.** The explicit candidates this investigation found (:data:`LINKS`)
  are verified the same way, dividend by dividend.
* **Identity timelines** (:data:`INVESTIGATE`) cover the eight members X3 left unverified
  and the five tickers that two V4 assets share. They compare each candidate ticker's
  month-end close, as of that month-end, with the asset's RAW close.

Decisions:

* An own dividend whose ticker meant another security on its dates becomes a ``VOID``.
* A verified successor-ticker dividend the asset does not already receive becomes a
  ``CREDIT``.
* Anything the data cannot decide stays ``UNRESOLVED``. A correction never credits it and
  never voids it.

Nothing is inferred from ticker similarity.

Step 3, :func:`register`. The set ``CA_DIVIDEND_IDENTITY_V2`` holds every member of
``CA_DIVIDEND_IDENTITY_V1`` (D-243, unchanged) plus this evidence's VERIFIED records.
Registration is append-only and happens once. No exclusion is released.
"""

from __future__ import annotations

import itertools
import json
import statistics
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.sessions import previous_trading_day
from app.backtesting import corrections, dividends
from app.backtesting.data import CALENDAR, sessions_between
from app.database.models import (
    Asset,
    CashDividendRecord,
    CashDividendRetrievalRecord,
    CorporateActionEventRecord,
    CorporateActionSetRecord,
    MarketBarRecord,
    ResearchPreregistrationRecord,
    ResearchUniverseCandidateRecord,
)
from app.research import ca_dividends, store
from app.research.bars import identity_of
from app.research.ca_dividends import Link, verify
from app.research.exclusions import sha256_file

BATCH = 50
SET_CODE = "CA_DIVIDEND_IDENTITY_V2"
BASE_SET = ca_dividends.SET_CODE
PREREGISTRATION = "X31_DIVIDEND_COMPLETENESS_IDENTITY_V1"
SCOPE = ca_dividends.SCOPE
CREATED_BY = ca_dividends.CREATED_BY
PROVIDER = ca_dividends.PROVIDER
UNIVERSES = ("RESEARCH_US_EQUITY_V4", "RESEARCH_ETF_CANDIDATES_V5")
FIRST_EX, LAST_EX = ca_dividends.FIRST_EX, ca_dividends.LAST_EX
PREFIX = "X31"
# A dividend-stream gap is an interval longer than this multiple of the asset's median.
GAP_FACTOR = Decimal("1.6")

_PUBLIC = "X3.1 identity investigation (D-244); public record: "
LINKS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
# Asset id -> the candidate tickers of its identity timeline (the label first).
INVESTIGATE = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
UNVERIFIED_EIGHT = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection

# The identity review of the investigated assets: company, finding, status. Every finding
# rests on the evidence (timeline segments, RAW moves, zero-volume runs, the replay's
# delisting date) plus the public record it names; status VERIFIED means the asset's prices
# and dividends are one company's throughout its history, the others say what is not.
IDENTITY_REVIEW = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


# ---------------------------------------------------------------------- step 1: retrieval
def legacy_ranges(session: Session) -> dict[tuple[date, date], list[str]]:
    """The D-237 retrievals (no ``basis``, so processing-date ranges): symbols by range."""
    r = CashDividendRetrievalRecord
    out: dict[tuple[date, date], set[str]] = {}
    for symbol, start, end, details in session.execute(
        select(r.symbol, r.range_start, r.range_end, r.details).where(
            r.provider == dividends.PROVIDER, r.status == dividends.OK
        )
    ):
        if (details or {}).get("basis") == dividends.EX_DATE_BASIS:
            continue
        out.setdefault((start, end), set()).add(symbol)
    return {k: sorted(v) for k, v in sorted(out.items())}


def retrieve_all(factory: sessionmaker[Session], pages_path: Path) -> dict[str, Any]:
    """Retrieve every legacy (symbol, range) again in ex-date terms; save the responses."""
    if pages_path.exists():
        raise FileExistsError(pages_path)
    with factory() as session:
        ranges = legacy_ranges(session)
    started = datetime.now(UTC)
    pages: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    for (start, end), symbols in ranges.items():
        for i in range(0, len(symbols), BATCH):
            part = symbols[i : i + BATCH]
            first_page = len(pages)
            counts = dividends.retrieve(factory, part, start, end, pages=pages)
            totals.update({k: v for k, v in counts.items() if isinstance(v, int)})
            lo, hi = dividends.process_window(start, end)
            batches.append(
                {
                    "symbols": part,
                    "ex_range": [start.isoformat(), end.isoformat()],
                    "process_window": [lo.isoformat(), hi.isoformat()],
                    "pages": [first_page, len(pages)],
                    "counts": counts,
                }
            )
            if not counts.get("status_ok"):
                raise RuntimeError(f"retrieval failed for {part[0]}..{part[-1]}")
    doc = {
        "kind": "x31_dividend_retrieval_pages",
        "decision": "D-244",
        "endpoint": dividends.PATH,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "basis": dividends.EX_DATE_BASIS,
        "process_lead_days": dividends.PROCESS_LEAD.days,
        "process_lag_days": dividends.PROCESS_LAG.days,
        "batches": batches,
        "pages": pages,
    }
    body = json.dumps(doc, indent=1, sort_keys=True, default=str)
    pages_path.write_text(body)
    return {
        "pages_path": str(pages_path),
        "sha256": sha256_file(pages_path),  # the file as written (line endings included)
        "batches": len(batches),
        "symbols": sum(len(b["symbols"]) for b in batches),
        **dict(totals),
    }


# ---------------------------------------------------------------------- step 2: evidence
class Lookups(ca_dividends.Lookups):
    """X3's look-ups (closes as of a date, ticker dividends). Dividends are requested over
    the processing-date window of the ex-date range (D-244)."""

    def __init__(self, data: dict[str, Any] | None, *, offline: bool) -> None:
        super().__init__(data, offline=offline)
        lo, hi = dividends.process_window(FIRST_EX, LAST_EX)
        if data is None:
            self.data["dividend_request"] = {
                "start": lo.isoformat(),
                "end": hi.isoformat(),
                "basis": "processing-date window of the ex-date range",
            }
        self._window = (lo, hi)

    def dividends(self, tickers: list[str]) -> dict[str, list[dict[str, Any]]]:
        missing = [t for t in tickers if t not in self.data["dividends"]]
        if missing and self.offline:
            raise LookupError(f"no saved dividends for {missing}")
        for i in range(0, len(missing), 50):
            part = missing[i : i + 50]
            rows = dividends.fetch_rows(self._http(), part, *self._window)
            self.requests += 1
            for t in part:
                self.data["dividends"][t] = sorted(
                    (r for r in rows if str(r.get("symbol", "")).upper() == t),
                    key=lambda r: (r.get("ex_date", ""), str(r.get("id", ""))),
                )
        return {t: self.data["dividends"][t] for t in tickers}


def _members(session: Session) -> dict[uuid.UUID, dict[str, Any]]:
    c = ResearchUniverseCandidateRecord
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for row in session.scalars(
        select(c)
        .where(c.member.is_(True), c.universe_code.in_(UNIVERSES))
        .order_by(c.asset_id, c.effective_from)
    ):
        if row.asset_id is None or row.effective_from is None or row.effective_to is None:
            continue  # the member check constraint makes this unreachable
        m = out.setdefault(row.asset_id, {"universes": set(), "spans": []})
        m["universes"].add(row.universe_code)
        span = [row.effective_from.isoformat(), row.effective_to.isoformat()]
        if span not in m["spans"]:
            m["spans"].append(span)
    return out


def _provider_rows(session: Session, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Newest revision per provider id, ex-date in the window, with its first retrieval."""
    d = CashDividendRecord
    newest: dict[str, CashDividendRecord] = {}
    first_seen: dict[str, datetime] = {}
    for row in session.scalars(
        select(d)
        .where(d.provider == PROVIDER, d.symbol.in_(symbols))
        .where(d.ex_date >= FIRST_EX, d.ex_date <= LAST_EX)
        .order_by(d.provider_action_id, d.revision)
    ):
        newest[row.provider_action_id] = row
        first_seen.setdefault(row.provider_action_id, row.retrieved_at)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in sorted(newest.values(), key=lambda x: (x.symbol, x.ex_date, x.provider_action_id)):
        out[x.symbol].append(
            {
                "provider_action_id": x.provider_action_id,
                "revision": x.revision,
                "symbol": x.symbol,
                "ex_date": x.ex_date.isoformat(),
                "rate": str(Decimal(x.rate)),
                "special": x.special,
                "foreign": x.foreign,
                "payable_date": x.payable_date.isoformat() if x.payable_date else None,
                "process_date": x.process_date.isoformat() if x.process_date else None,
                "cash_dividends_id": x.id,
                "first_retrieved_at": first_seen[x.provider_action_id].isoformat(),
            }
        )
    return out


def _coverage(session: Session, symbols: list[str]) -> dict[str, dict[str, bool]]:
    r = CashDividendRetrievalRecord
    out = {s: {"legacy_processing_date_range": False, "ex_date_range": False} for s in symbols}
    for symbol, start, end, details in session.execute(
        select(r.symbol, r.range_start, r.range_end, r.details).where(
            r.provider == PROVIDER, r.status == dividends.OK, r.symbol.in_(symbols)
        )
    ):
        if start <= FIRST_EX and end >= LAST_EX:
            basis = (details or {}).get("basis") == dividends.EX_DATE_BASIS
            out[symbol]["ex_date_range" if basis else "legacy_processing_date_range"] = True
    return out


def _month_ends(first: date, last: date) -> list[date]:
    days = [s.day for s in sessions_between(first, last)]
    return [d for i, d in enumerate(days) if i + 1 == len(days) or days[i + 1].month != d.month]


def _cum(ex: date) -> date:
    return previous_trading_day(CALENDAR, ex).day


def _in_spans(day: str, spans: list[list[str]]) -> bool:
    return any(a <= day <= b for a, b in spans)


def _overlap(lo: date, hi: date, spans: list[list[str]]) -> bool:
    return any(lo <= date.fromisoformat(b) and date.fromisoformat(a) <= hi for a, b in spans)


def gaps(
    ex_dates: list[date],
    first_bar: date,
    last_bar: date,
    spans: list[list[str]] | None = None,
) -> dict[str, Any] | None:
    """Intervals in a dividend stream much longer than its median, and a stream that stops
    (or starts late) while the asset still trades. With ``spans`` (membership), only what
    overlaps a span is flagged: elsewhere no position can be held. ``estimated_missing``
    is how many payments at the median interval would fit (an estimate for review: a
    company may change its schedule, start or stop paying; never a correction)."""
    days = sorted(set(ex_dates))
    if len(days) < 4:
        return None
    steps = [(b - a).days for a, b in itertools.pairwise(days)]
    median = Decimal(str(statistics.median(steps)))
    limit = median * GAP_FACTOR
    end, start = min(last_bar, LAST_EX), max(first_bar, FIRST_EX)

    def relevant(lo: date, hi: date) -> bool:
        return spans is None or _overlap(lo, hi, spans)

    def missing(days_: int) -> int:
        return max(int((Decimal(days_) / median).to_integral_value()) - 1, 0)

    found: list[list[Any]] = [
        [a.isoformat(), b.isoformat(), (b - a).days, missing((b - a).days)]
        for a, b in itertools.pairwise(days)
        if (b - a).days > limit and relevant(a, b)
    ]
    stops = (end - days[-1]).days > limit and relevant(days[-1], end)
    late = (days[0] - start).days > limit and relevant(start, days[0])
    flags = {
        "median_days": str(median),
        "gaps": found,
        "stops_while_trading": stops,
        "starts_late": late,
        "estimated_missing_in_gaps": sum(int(g[3]) for g in found),
    }
    return flags if found or stops or late else None


MOVE = Decimal("0.35")  # a one-day RAW move larger than this is listed for the identity review
STALE = 5  # zero-volume sessions in a row worth listing


def _scan(session: Session, asset_id: uuid.UUID) -> dict[str, Any]:
    """Facts for the identity review: one-day RAW moves beyond :data:`MOVE` (a splice, a
    split or a real move: the review says which) and runs of zero-volume bars (stale
    prices after a security stopped trading)."""
    b = MarketBarRecord
    bars: dict[date, tuple[Decimal, Any]] = {}
    for at, close, volume in session.execute(
        select(b.event_time, b.close, b.volume)
        .where(b.asset_id == asset_id, b.adjustment == "RAW", b.timeframe == "1Day")
        .where(b.feed == "sip")
        .order_by(b.event_time, b.revision, b.id)
    ):
        bars[at.astimezone(UTC).date()] = (Decimal(close), volume)
    days = sorted(bars)
    moves = [
        [y.isoformat(), str(bars[x][0]), str(bars[y][0])]
        for x, y in itertools.pairwise(days)
        if abs(bars[y][0] / bars[x][0] - 1) > MOVE
    ]
    runs: list[list[Any]] = []
    run: list[date] = []
    for d in [*days, None]:
        if d is not None and not bars[d][1]:
            run.append(d)
            continue
        if len(run) >= STALE:
            runs.append([run[0].isoformat(), run[-1].isoformat(), len(run)])
        run = []
    return {"raw_moves_over_35pct": moves, "zero_volume_runs": runs}


def _decide_own(check: dict[str, Any], has_bars: bool, inside: bool) -> str:
    identity = check["identity"]
    if identity == "SAME_SECURITY":
        return "KEEP"
    if identity == "OTHER_SECURITY":
        return "VOID"
    if not has_bars and not inside:
        return "NOT_HELD"  # outside the asset's history: no position can receive it
    return "UNRESOLVED"


def build(
    factory: sessionmaker[Session],
    out: Path,
    lookups_path: Path,
    evidence_path: Path,
    seed: Path | None = None,
    retrieval_pages: Path | None = None,
) -> dict[str, Any]:
    """Classify every member's dividends and write the evidence (never replaced). Reads
    ``lookups_path`` if it exists (offline); otherwise writes it from GET requests. A new one
    may reuse the closes of an earlier look-ups file (``seed``: the same requests)."""
    if evidence_path.exists():
        raise FileExistsError(evidence_path)
    offline = lookups_path.exists()
    cache = Lookups(json.loads(lookups_path.read_text()) if offline else None, offline=offline)
    if not offline and seed is not None:
        seeded = json.loads(seed.read_text())["closes"]
        cache.data["closes"] = seeded
        cache.data["closes_seeded_from"] = {"path": seed.as_posix(), "sha256": sha256_file(seed)}
    now = datetime.now(UTC)
    try:
        with factory() as session:
            members = _members(session)
            ids = sorted(members, key=str)
            assets = {a.id: a for a in session.scalars(select(Asset).where(Asset.id.in_(ids)))}
            closes = {a: ca_dividends._raw_closes(session, a) for a in ids}
            ident = {a: identity_of(session, a) for a in ids}
            symbols = sorted({assets[a].symbol for a in ids if "~" not in assets[a].symbol})
            own = _provider_rows(session, symbols)
            coverage = _coverage(session, symbols)
            rr = CashDividendRetrievalRecord
            t0 = session.scalar(
                select(rr.retrieved_at)
                .where(rr.details["basis"].astext == dividends.EX_DATE_BASIS)
                .order_by(rr.retrieved_at)
                .limit(1)
            )
            base = corrections.load_set(session, BASE_SET, known_at=now)
            investigated = [uuid.UUID(x) for x in INVESTIGATE]
            scans = {a: _scan(session, a) for a in investigated}
            delisted = store.delistings(session, investigated, known_at=now)
        if t0 is None:
            raise ValueError("run x31-retrieve first: no ex-date-basis retrieval exists")
        base_by_asset: dict[uuid.UUID, list[corrections.DividendCorrection]] = defaultdict(list)
        for bc in base.dividends():
            base_by_asset[bc.asset_id].append(bc)
        by_symbol: dict[str, list[uuid.UUID]] = defaultdict(list)
        for a in ids:
            by_symbol[assets[a].symbol].append(a)
        shared = {s: v for s, v in by_symbol.items() if len(v) > 1}

        # Candidates: own-symbol dividends of every member; successor-ticker dividends.
        link_tickers = sorted({k.ticker for k in LINKS})
        provider = cache.dividends(link_tickers)
        candidates: list[dict[str, Any]] = []
        for a in ids:
            symbol = assets[a].symbol
            for row in own.get(symbol, []):
                candidates.append({"kind": "OWN", "asset": a, "ticker": symbol, "row": row})
        for link in LINKS:
            for raw in provider[link.ticker]:
                row = ca_dividends._row(raw)
                if FIRST_EX <= date.fromisoformat(row["ex_date"]) <= LAST_EX:
                    candidates.append(
                        {
                            "kind": "LINK",
                            "asset": uuid.UUID(link.asset_id),
                            "ticker": link.ticker,
                            "row": row,
                            "raw": raw,
                            "link": link,
                        }
                    )
        need: dict[date, set[str]] = defaultdict(set)
        for c in candidates:
            ex = date.fromisoformat(c["row"]["ex_date"])
            cum = _cum(ex)
            pre = _cum(cum)
            if closes[c["asset"]].get(cum) is None and closes[c["asset"]].get(pre) is None:
                continue  # the asset has no bar to compare: nothing to ask the provider
            need[cum].add(c["ticker"])
        timeline_days: dict[str, list[date]] = {}
        for aid, tickers in INVESTIGATE.items():
            days = sorted(closes[uuid.UUID(aid)])
            timeline_days[aid] = _month_ends(days[0], days[-1]) if days else []
            for d in timeline_days[aid]:
                need[d].update(tickers)
        for cum in sorted(need):
            cache.closes(sorted(need[cum]), cum)
    finally:
        if not offline:
            lookups_path.write_text(json.dumps(cache.data, indent=1, sort_keys=True))
        cache.shut()
    lookups_sha = sha256_file(lookups_path)
    saved = cache.data["closes"]

    # Verification.
    records: list[dict[str, Any]] = []
    for c in candidates:
        a, row = c["asset"], c["row"]
        ex = date.fromisoformat(row["ex_date"])
        cum = _cum(ex)
        check = verify(closes[a], saved.get(cum.isoformat(), {}).get(c["ticker"], {}), ex)
        bars = sorted(closes[a])
        has_bars = any(s["asset_raw_close"] is not None for s in check["sessions"])
        inside = bool(bars) and bars[0] <= ex <= bars[-1]
        records.append(
            {
                "kind": c["kind"],
                "asset_id": str(a),
                "label": assets[a].symbol,
                "ticker": c["ticker"],
                "source": c["link"].source if c["kind"] == "LINK" else "own symbol (stored)",
                "dividend": row,
                "provider_raw": c.get("raw"),
                "new_in_x31": c["kind"] == "OWN" and row["first_retrieved_at"] >= t0.isoformat(),
                "in_member_span": _in_spans(row["ex_date"], members[a]["spans"]),
                "asset_history": [bars[0].isoformat(), bars[-1].isoformat()] if bars else None,
                "verification": check,
                "_has_bars": has_bars,
                "_inside": inside,
            }
        )
    records.sort(
        key=lambda r: (r["label"], r["asset_id"], r["dividend"]["ex_date"], r["kind"], r["ticker"])
    )

    # Decisions.
    base_void = {(c.asset_id, c.provider_action_id) for cs in base_by_asset.values() for c in cs
                 if c.action == corrections.VOID}  # fmt: skip
    base_credit_terms = {
        (c.asset_id, c.ex_date.isoformat(), c.rate)
        for cs in base_by_asset.values()
        for c in cs
        if c.action == corrections.CREDIT
    }
    kept_terms: set[tuple[str, str, Decimal]] = set()
    for r in records:
        if r["kind"] != "OWN":
            continue
        key = (uuid.UUID(r["asset_id"]), r["dividend"]["provider_action_id"])
        if key in base_void:
            r["decision"], r["reason"] = "VOIDED_BY_V1", f"already voided by {BASE_SET}"
            continue
        r["decision"] = _decide_own(r["verification"], r["_has_bars"], r["_inside"])
        if r["decision"] in ("KEEP", "UNRESOLVED"):
            d = r["dividend"]
            kept_terms.add((r["asset_id"], d["ex_date"], Decimal(d["rate"])))
    credited: set[tuple[str, str, Decimal]] = set()
    for r in records:
        if r["kind"] != "LINK":
            continue
        d = r["dividend"]
        terms = (r["asset_id"], d["ex_date"], Decimal(d["rate"]))
        identity = r["verification"]["identity"]
        if identity == "OTHER_SECURITY":
            r["decision"], r["reason"] = "REJECTED", "the ticker meant another security"
        elif identity == "UNDECIDED":
            if not r["_has_bars"] and not r["_inside"]:
                r["decision"], r["reason"] = "NOT_HELD", "outside the asset's history"
            else:
                r["decision"], r["reason"] = "UNRESOLVED", "identity not decided on both sessions"
        elif (
            terms in kept_terms
            or (
                uuid.UUID(r["asset_id"]),
                d["ex_date"],
                Decimal(d["rate"]),
            )
            in base_credit_terms
        ):
            r["decision"], r["reason"] = "REJECTED", "duplicate of a dividend the asset has"
        elif terms in credited:
            r["decision"], r["reason"] = "REJECTED", "duplicate of another credited record"
        else:
            r["decision"], r["reason"] = "CREDIT", "verified on both sessions"
            credited.add(terms)
    for r in records:
        r.pop("_has_bars")
        r.pop("_inside")

    # Identity timelines.
    timelines: dict[str, Any] = {}
    for aid, tickers in INVESTIGATE.items():
        a = uuid.UUID(aid)
        rows: list[dict[str, Any]] = []
        for d in timeline_days[aid]:
            asset_close = closes[a].get(d)
            got = saved.get(d.isoformat(), {})
            matches = {
                t: ca_dividends._match(asset_close, got.get(t, {}).get(d.isoformat()))
                for t in tickers
            }
            rows.append(
                {
                    "session": d.isoformat(),
                    "asset_raw_close": None if asset_close is None else str(asset_close),
                    "ticker_closes_asof": {t: got.get(t, {}).get(d.isoformat()) for t in tickers},
                    "matched": sorted(t for t, m in matches.items() if m),
                }
            )
        spans: list[dict[str, Any]] = []
        for x in rows:
            seg = ",".join(x["matched"]) or "NONE"
            if spans and spans[-1]["matched"] == seg:
                spans[-1]["to"] = x["session"]
                spans[-1]["months"] += 1
            else:
                spans.append({"matched": seg, "from": x["session"], "to": x["session"],
                              "months": 1})  # fmt: skip
        timelines[aid] = {
            "label": assets[a].symbol,
            "name": assets[a].name,
            "tickers": list(tickers),
            "member_spans": members[a]["spans"],
            "replay_delisting": None if a not in delisted else delisted[a].isoformat(),
            **scans[a],
            "segments": spans,
            "month_ends": rows,
        }

    # Asset-by-asset completeness.
    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_asset[r["asset_id"]].append(r)
    report: dict[str, Any] = {}
    for a in ids:
        sid = str(a)
        asset = assets[a]
        rs = by_asset.get(sid, [])
        own_rs = [r for r in rs if r["kind"] == "OWN"]
        v1 = base_by_asset.get(a, [])
        credited_v2: list[str] = []
        for r in own_rs:
            if r["decision"] in ("KEEP", "UNRESOLVED", "NOT_HELD"):
                credited_v2.append(r["dividend"]["ex_date"])
        credited_v2 += [c.ex_date.isoformat() for c in v1 if c.action == corrections.CREDIT]
        credited_v2 += [r["dividend"]["ex_date"] for r in rs if r["decision"] == "CREDIT"]
        held = [x for x in credited_v2 if _in_spans(x, members[a]["spans"])]
        bars = sorted(closes[a])
        decided = Counter(r["decision"] for r in own_rs)
        voids = decided["VOID"] + sum(1 for c in v1 if c.action == corrections.VOID)
        if not own_rs and not v1 and not any(r["kind"] == "LINK" for r in rs):
            confidence = "NO_DIVIDENDS"
        elif decided["UNRESOLVED"]:
            confidence = "PARTLY_UNRESOLVED"
        elif voids or v1 or decided["CREDIT"] or any(r["decision"] == "CREDIT" for r in rs):
            confidence = "VERIFIED_WITH_CORRECTIONS"
        else:
            confidence = "VERIFIED"
        cov = coverage.get(asset.symbol, {"legacy_processing_date_range": False,
                                          "ex_date_range": False})  # fmt: skip
        report[sid] = {
            "label": asset.symbol,
            "name": asset.name,
            "universes": sorted(members[a]["universes"]),
            "member_spans": members[a]["spans"],
            "identity": {
                "kind": "research-identity" if ident[a] is not None else "provider-listing",
                "request": None
                if ident[a] is None
                else {
                    k: (getattr(ident[a], "details", None) or {}).get(k)
                    for k in ("request_symbol", "asof")
                },
                "identity_sha256": ca_dividends._entity(ident[a]),
                "confidence": confidence,
                "review": None
                if sid not in IDENTITY_REVIEW
                else dict(zip(("company", "finding", "status"), IDENTITY_REVIEW[sid], strict=True)),
                "shared_symbol_with": sorted(
                    str(x) for x in shared.get(asset.symbol, []) if x != a
                ),
            },
            "coverage": {
                **cov,
                "corrections_only": "~" in asset.symbol,
            },
            "history": [bars[0].isoformat(), bars[-1].isoformat()] if bars else None,
            "own_symbol_dividends": {
                "existing": sum(1 for r in own_rs if not r["new_in_x31"]),
                "new_in_x31": sum(1 for r in own_rs if r["new_in_x31"]),
                "decisions": dict(sorted(decided.items())),
            },
            "v1_corrections": dict(sorted(Counter(c.action for c in v1).items())),
            "x31_credits": sum(1 for r in rs if r["decision"] == "CREDIT"),
            "x31_unresolved_links": sum(
                1 for r in rs if r["kind"] == "LINK" and r["decision"] == "UNRESOLVED"
            ),
            "dividends_credited_with_v2": len(credited_v2),
            "dividends_in_member_spans": len(held),
            "stream_flags": gaps(
                [date.fromisoformat(d) for d in credited_v2],
                bars[0] if bars else FIRST_EX,
                bars[-1] if bars else LAST_EX,
                members[a]["spans"],
            ),
        }

    # Exact provider duplicates (v2 credits one).
    duplicates = []
    for symbol in symbols:
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in own.get(symbol, []):
            groups[(row["ex_date"], row["rate"])].append(row["provider_action_id"])
        for (ex_day, rate), pids in sorted(groups.items()):
            if len(pids) > 1:
                duplicates.append({"symbol": symbol, "ex_date": ex_day, "rate": rate, "ids": pids})

    summary = {
        "assets": len(ids),
        "symbols": len(symbols),
        "shared_symbols": {s: sorted(str(x) for x in v) for s, v in sorted(shared.items())},
        "candidates": len(records),
        "decisions": dict(sorted(Counter(f"{r['kind']}:{r['decision']}" for r in records).items())),
        "new_in_x31": sum(1 for r in records if r["new_in_x31"]),
        "new_in_x31_in_member_spans": sum(
            1 for r in records if r["new_in_x31"] and r["in_member_span"]
        ),
        "confidence": dict(
            sorted(Counter(v["identity"]["confidence"] for v in report.values()).items())
        ),
        "stream_flags_in_member_spans": sum(1 for v in report.values() if v["stream_flags"]),
        "estimated_missing_in_member_span_gaps": sum(
            v["stream_flags"]["estimated_missing_in_gaps"]
            for v in report.values()
            if v["stream_flags"]
        ),
        "provider_exact_duplicates": len(duplicates),
    }
    doc = {
        "code": SET_CODE,
        "base_set": {"code": BASE_SET, "content_sha256": base.content_sha256},
        "preregistration": PREREGISTRATION,
        "scope": SCOPE,
        "generated_at": now.isoformat(),
        "window": {"first_ex": FIRST_EX.isoformat(), "last_ex": LAST_EX.isoformat()},
        "tolerance": str(ca_dividends.TOLERANCE),
        "calendar": "app.data_engine.calendar with the D-244 special closures",
        "ex_date_retrieval_started": t0.isoformat(),
        "retrieval_pages": None
        if retrieval_pages is None
        else {"path": retrieval_pages.as_posix(), "sha256": sha256_file(retrieval_pages)},
        "lookups": {
            "path": lookups_path.as_posix(),
            "sha256": lookups_sha,
            "dividend_request": cache.data.get("dividend_request"),
            "closes_seeded_from": cache.data.get("closes_seeded_from"),
        },
        "links": [
            {"label": k.label, "asset_id": k.asset_id, "ticker": k.ticker, "source": k.source}
            for k in LINKS
        ],
        "timelines": timelines,
        "assets": report,
        "provider_exact_duplicates": duplicates,
        "records": records,
        "summary": summary,
    }
    evidence_path.write_text(json.dumps(doc, indent=1, default=str))
    return {
        "evidence": str(evidence_path),
        "sha256": sha256_file(evidence_path),
        "lookups_sha256": lookups_sha,
        "provider_requests": cache.requests,
        **summary,
    }


# ---------------------------------------------------------------------- step 3: register
def _events(ev: dict[str, Any], ev_sha: str) -> list[dict[str, Any]]:
    """The records to register, as column values (deterministic)."""
    assets = ev["assets"]
    out: list[dict[str, Any]] = []
    succession: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in ev["records"]:
        if r["kind"] == "LINK" and r["decision"] == "CREDIT":
            succession[(r["asset_id"], r["ticker"])].append(
                r["verification"]["sessions"][0]["session"]
            )

    def tag(asset_id: str) -> str:
        return f"{assets[asset_id]['label']}@{asset_id[:8]}"

    for (aid, ticker), days in sorted(succession.items()):
        out.append(
            {
                "code": f"{PREFIX}-TS-{tag(aid)}-{ticker}",
                "asset_id": aid,
                "event_type": corrections.SUCCESSION,
                "provider_ticker": ticker,
                "valid_from": date.fromisoformat(min(days)),
                "valid_to": date.fromisoformat(max(days)),
                "known_on": date.fromisoformat(max(days)),
                "verification_status": corrections.VERIFIED,
                "verification": {"verified_cum_sessions": sorted(days)},
                "evidence": {
                    "sources": sorted(
                        {
                            k["source"]
                            for k in ev["links"]
                            if (k["asset_id"], k["ticker"]) == (aid, ticker)
                        }
                    )
                },
            }
        )
    for r in ev["records"]:
        d = r["dividend"]
        if r["kind"] == "LINK" and r["decision"] in ("CREDIT", "UNRESOLVED", "REJECTED"):
            event_type, prefix = corrections.CREDIT, "DIV"
            state = {"CREDIT": corrections.VERIFIED}.get(r["decision"], r["decision"])
        elif r["kind"] == "OWN" and r["decision"] == "VOID":
            event_type, state, prefix = corrections.VOID, corrections.VERIFIED, "VOID"
        elif r["kind"] == "OWN" and r["decision"] == "UNRESOLVED":
            event_type, state, prefix = corrections.VOID, "UNRESOLVED", "VOID"
        else:
            continue  # kept, not held, already voided: nothing to record
        link = (
            f"{PREFIX}-TS-{tag(r['asset_id'])}-{r['ticker']}"
            if r["kind"] == "LINK" and state == corrections.VERIFIED
            else None
        )
        out.append(
            {
                "code": f"{PREFIX}-{prefix}-{tag(r['asset_id'])}-{r['ticker']}-{d['ex_date']}-"
                f"{d['provider_action_id']}",
                "asset_id": r["asset_id"],
                "event_type": event_type,
                "provider_ticker": r["ticker"],
                "provider_action_id": d["provider_action_id"],
                "ex_date": date.fromisoformat(d["ex_date"]),
                "payable_date": date.fromisoformat(d["payable_date"])
                if d["payable_date"]
                else None,
                "cash_amount": Decimal(d["rate"]),
                "special": d["special"],
                "foreign": d["foreign"],
                "link_code": link,
                "known_on": date.fromisoformat(d["ex_date"]),
                "verification_status": state,
                "verification": {
                    **r["verification"],
                    "decision": r["decision"],
                    "reason": r.get("reason"),
                },
                "evidence": {
                    "source": r["source"],
                    "provider_raw": r["provider_raw"],
                    "dividend": d,
                },
            }
        )
    for e in out:
        aid = e.pop("asset_id")
        e["asset_id"] = uuid.UUID(aid)
        e["asset_label"] = assets[aid]["label"]
        e["identity_sha256"] = assets[aid]["identity"]["identity_sha256"]
        e["provider"] = PROVIDER
        e["evidence_sha256"] = ev_sha
    return out


def register(
    factory: sessionmaker[Session],
    document: Path,
    evidence_path: Path,
    *,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Register the new records and the set once, in one transaction. Refuses if the set
    exists, if the document does not cite the evidence, if the base set changed, or if the
    stored data the evidence relied on changed (identities, RAW closes)."""
    doc_sha, ev_sha = sha256_file(document), sha256_file(evidence_path)
    if ev_sha not in document.read_text(encoding="utf-8"):
        raise ValueError("the pre-registration document does not cite the evidence's sha256")
    ev = json.loads(evidence_path.read_text())
    if ev["code"] != SET_CODE:
        raise ValueError("the evidence is not this set's")
    events = _events(ev, ev_sha)
    with factory() as session, session.begin():
        s = CorporateActionSetRecord
        if session.scalar(select(s.id).where(s.code == SET_CODE)) is not None:
            raise ValueError(f"{SET_CODE} is already registered (append-only)")
        p = ResearchPreregistrationRecord
        if session.scalar(select(p.id).where(p.code == PREREGISTRATION)) is not None:
            raise ValueError(f"{PREREGISTRATION} is already registered (append-only)")
        base = corrections.load_set(session, BASE_SET, known_at=datetime.now(UTC))
        if base.content_sha256 != ev["base_set"]["content_sha256"]:
            raise ValueError(f"{BASE_SET} changed since the evidence")
        touched = {e["asset_id"] for e in events}
        for aid in sorted(touched, key=str):
            a = ev["assets"][str(aid)]
            if ca_dividends._entity(identity_of(session, aid)) != a["identity"]["identity_sha256"]:
                raise ValueError(f"{a['label']}: identity changed since the evidence")
            closes = ca_dividends._raw_closes(session, aid)
            for r in ev["records"]:
                if r["asset_id"] != str(aid):
                    continue
                for v in r["verification"]["sessions"]:
                    got = closes.get(date.fromisoformat(v["session"]))
                    if (None if got is None else str(got)) != v["asset_raw_close"]:
                        raise ValueError(f"{a['label']} {v['session']}: RAW close changed")
        rows = [CorporateActionEventRecord(**e, created_by=CREATED_BY) for e in events]
        session.add_all(rows)
        session.flush()
        new_members = [r for r in rows if r.verification_status == corrections.VERIFIED]
        members = list(base.events) + new_members
        sha = corrections.content_sha256([corrections.event_content(r) for r in members])
        session.add(
            CorporateActionSetRecord(
                code=SET_CODE,
                version=2,
                scope=SCOPE,
                description="Dividend identity corrections, second scope (D-244): every "
                f"{BASE_SET} member plus the X3.1 completeness and identity records",
                rule={
                    "members": f"every member of {BASE_SET} and every VERIFIED record of the "
                    "X3.1 evidence",
                    "credit": "provider dividend under an explicit successor ticker whose close "
                    "as of the cum session equals the asset's RAW close on the cum and previous "
                    "sessions",
                    "void": "provider dividend under the asset's own ticker that fails both",
                    "tolerance": ev["tolerance"],
                    "window": ev["window"],
                    "calendar": ev["calendar"],
                },
                members=[{"code": r.code, "revision": r.revision} for r in members],
                content_sha256=sha,
                preregistration_code=PREREGISTRATION,
                document_sha256=doc_sha,
                evidence_sha256=ev_sha,
                created_by=CREATED_BY,
            )
        )
        session.add(
            ResearchPreregistrationRecord(
                code=PREREGISTRATION,
                document_path=document.as_posix(),
                document_sha256=doc_sha,
                reservation_codes=[],
                created_by=CREATED_BY,
            )
        )
        counts = Counter(f"{r.event_type}:{r.verification_status}" for r in rows)
        progress(json.dumps(dict(sorted(counts.items()))))
    return {
        "set": SET_CODE,
        "content_sha256": sha,
        "document_sha256": doc_sha,
        "members": len(members),
        "base_members": len(base.events),
        "new_records": dict(sorted(counts.items())),
    }


__all__ = [
    "IDENTITY_REVIEW",
    "INVESTIGATE",
    "LINKS",
    "PREREGISTRATION",
    "SET_CODE",
    "build",
    "gaps",
    "legacy_ranges",
    "register",
    "retrieve_all",
]


# ---------------------------------------------------------------------- the report table
def asset_table(ev: dict[str, Any], evidence_name: str) -> str:
    """The asset-by-asset completeness table (Markdown) of an evidence document."""
    dup: Counter[str] = Counter(d["symbol"] for d in ev["provider_exact_duplicates"])
    lines = [
        "# X3.1 dividend completeness, asset by asset",
        "",
        f"Generated from `{evidence_name}` (sha256 `{ev['_sha256']}`) by "
        "`app.research.dividend_completeness.asset_table`. Window: ex-dates "
        f"{ev['window']['first_ex']}..{ev['window']['last_ex']}. Each asset's records are in the "
        "evidence under `assets.<asset id>` and `records` (by `asset_id`).",
        "",
        "Columns: **existing** / **new** = own-symbol provider dividends retrieved before / by "
        "X3.1; **keep, void, unres, n/h** = own dividends verified as the asset's, voided "
        "(another security), unresolved, not holdable (outside the asset's history); "
        "**V1 c/v** = X3 credits / voids; **X31 c** = X3.1 successor-ticker credits; "
        "**credited** = dividends a run with policy v2 and set V2 credits in the window; "
        "**in spans** = of those, inside membership; **dup** = exact provider duplicates "
        "credited once; **flags** = membership-span stream flags (estimated missing "
        "payments in gaps; S = stops while trading, L = starts late).",
        "",
        "| label | asset | univ | spans | identity | confidence | coverage | existing | new "
        "| keep | void | unres | n/h | V1 c/v | X31 c | credited | in spans | dup | flags |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for aid, a in sorted(ev["assets"].items(), key=lambda kv: (kv[1]["label"], kv[0])):
        dec = a["own_symbol_dividends"]["decisions"]
        v1 = a["v1_corrections"]
        cov = a["coverage"]
        coverage = (
            "corrections only"
            if cov["corrections_only"]
            else "ex-date"
            if cov["ex_date_range"]
            else "legacy only"
            if cov["legacy_processing_date_range"]
            else "none"
        )
        f = a["stream_flags"]
        flags = ""
        if f:
            flags = str(f["estimated_missing_in_gaps"]) if f["gaps"] else ""
            flags += (" S" if f["stops_while_trading"] else "") + (" L" if f["starts_late"] else "")
        spans = ",".join(sorted({s[0][:4] for s in a["member_spans"]}))
        univ = "V5" if any("ETF" in u for u in a["universes"]) else "V4"
        lines.append(
            "| "
            + " | ".join(
                str(x)
                for x in (
                    a["label"],
                    f"`{aid[:8]}`",
                    univ,
                    spans,
                    ("pinned" if a["identity"]["kind"] == "research-identity" else "listing")
                    + (
                        f" ({a['identity']['review']['status']})"
                        if a["identity"].get("review")
                        else ""
                    ),
                    a["identity"]["confidence"],
                    coverage,
                    a["own_symbol_dividends"]["existing"],
                    a["own_symbol_dividends"]["new_in_x31"],
                    dec.get("KEEP", 0),
                    dec.get("VOID", 0) + dec.get("VOIDED_BY_V1", 0),
                    dec.get("UNRESOLVED", 0),
                    dec.get("NOT_HELD", 0),
                    f"{v1.get('CASH_DIVIDEND', 0)}/{v1.get('CASH_DIVIDEND_VOID', 0)}",
                    a["x31_credits"],
                    a["dividends_credited_with_v2"],
                    a["dividends_in_member_spans"],
                    dup.get(a["label"], 0) or "",
                    flags.strip(),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"
