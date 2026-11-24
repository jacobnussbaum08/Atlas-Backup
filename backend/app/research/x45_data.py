"""X4.5 (D-251): historical market data for the confirmed X4.4 members.

Evidence (write-once store ``config/simulation/evidence/x45``, its own manifest):

* ``provider/bars/<run>/<adjustment>/<sha>.json.gz``: raw provider daily bars (SIP) for each
  formation's confirmed members, ticker mapped as of the formation, over the exact membership
  window; ``raw`` and ``split`` adjustments.
* ``provider/recovery/<sha>.json.gz``: documented recovery requests (another ``asof`` mapping, or
  the security's later ticker), raw and split.
* ``provider/corporate_actions/<sha>.json.gz``: every corporate-action type, cash dividends
  included, for the members' tickers (processing-date window of D-244).

:func:`build` turns the evidence into the dataset (``config/simulation/lifecycle/
x45_member_data_v1.json.gz``, written once): per membership slot, the attributable bars (raw
OHLCV and the provider's split-adjusted close, never mixed), the attributed dividends and
corporate actions, the adjustment decisions, the quality and coverage report, and every
recovery with its rule. Nothing is fabricated: a missing bar stays missing, a zero-volume bar
is a provider placeholder (no trade recorded), and a bar after a security's recorded exit or
conversion is never its own.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.backtesting import dividends
from app.backtesting.data import sessions_between
from app.research import graph_v2 as g2
from app.research import ledger_v2 as l2
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import sources
from app.research import universe_x44 as u
from app.research import x4_evidence as x4

ROOT = Path("config/simulation/evidence/x45")
DATA_PATH = lc.OUT / "x45_member_data_v1.json.gz"
UNIVERSE_SHA256 = "6ad21763c8b5dc1ddaf4c07161f6fafcd5b74e2379491b2bfc10f57aabab26d7"
ADJUSTMENTS = ("raw", "split")
CA_TYPES = "cash_dividend," + "forward_split,reverse_split,unit_split,stock_dividend,spin_off,"
CA_TYPES += "cash_merger,stock_merger,stock_and_cash_merger,redemption,name_change,"
CA_TYPES += "worthless_removal,rights_distribution"
JUMP = math.log(3)
GAP_SESSIONS = 5  # a run of missing sessions this long or longer is a gap
OVERLAP_MIN = 20
CONFIRMED, UNRESOLVED = rs.CONFIRMED, rs.UNRESOLVED

METHOD: dict[str, str] = {
    "raw": "Raw provider OHLCV (SIP, adjustment=raw) as delivered; never altered.",
    "provider_split": "The provider's split-adjusted close (adjustment=split), stored beside the "
    "raw close; never used to fill a raw value.",
    "verified_split_factor": "Our split factor applies only provider split records that the raw "
    "prices confirm (the raw close moves by the record's ratio within 25%). A record the raw "
    "prices do not show (a provider adjustment artifact) is not applied; the return across it "
    "is marked unusable.",
    "dividends": "Cash dividends are kept as dated cash flows (ex-date, rate) attributed to the "
    "security holding the ticker on the ex-date; no total-return series is computed here.",
    "attribution": "Bars belong to a member only inside its membership window and up to its "
    "recorded exit or conversion; a successor's bars are never a predecessor's. When the "
    "provider's series continues past the filing that removed or converted the member (a "
    "Form 25 or successor notice), the series has passed to another security and the member's "
    "data ends the session before that filing.",
    "placeholders": "A zero-volume bar is a provider placeholder (no trade recorded), counted "
    "apart from traded bars and from missing sessions.",
}


class Store(x4.Store):
    KIND = "x45_evidence_manifest"
    DECISION = "D-251"

    def __init__(self, root: Path = ROOT) -> None:
        super().__init__(root)


# ---------------------------------------------------------------------- membership slots
def load_universe(path: Path = u.UNIVERSE_PATH, expect: str = UNIVERSE_SHA256) -> dict[str, Any]:
    got = l2._sha_file(path)
    if got != expect:
        raise ValueError(f"{path}: sha256 {got} is not the X4.4 universe {expect}")
    doc: dict[str, Any] = x4.read_json(path)
    if u.content_sha256(doc) != doc["content_sha256"]:
        raise ValueError(f"{path}: the universe does not hash to its content_sha256")
    return doc


def exit_cut(r: dict[str, Any]) -> tuple[str | None, str | None]:
    """The last date a member's data can be its own, and why: a conversion (its filing date)
    or a recorded end (its upper bound, else its filing date)."""
    lf = r.get("lifecycle") or {}
    cuts = []
    if lf.get("successor"):
        cuts.append((lf["successor"]["filed"], "CONVERSION_TO_SUCCESSOR"))
    for e in lf.get("ends") or []:
        if not e["before_window"]:
            d = e["not_after"] or e["filing_date"]
            if d:
                cuts.append((d, f"RECORDED_END:{e['code']}"))
    if not cuts:
        return None, None
    d, why = min(cuts)
    return d, why


PLAIN_FAMILY = ("COMMON", "ORDINARY", "ADS", "UNSPECIFIED", "CLASS_", "LP_UNIT")


def _plain(key: str) -> bool:
    label = key.split("|", 1)[1] if "|" in key else ""
    return label.startswith(PLAIN_FAMILY)


def _family(key: str) -> str:
    """Depositary shares and LP units are families of their own; common, ordinary,
    unspecified and lettered classes are one share family."""
    label = key.split("|", 1)[1] if "|" in key else ""
    return next((f for f in ("ADS", "LP_UNIT") if label.startswith(f)), "SHARE")


def issuer_exit(
    r: dict[str, Any], v2: dict[str, Any] | None, lo: str, hi: str
) -> tuple[str | None, str | None]:
    """When the member's class key has no recorded end: the one confirmed end of the same
    issuer's plain class of the member's own family (shares, depositary shares or LP units)
    inside the window (an issuer-level exit, MEDIUM: X4.4's class keys can word one class
    differently, e.g. 'Common' and 'Class A Common')."""
    key = r.get("security_key") or ""
    if not v2 or not r.get("entity_key") or not _plain(key):
        return None, None
    ends = sorted(
        (e["not_after"] or e["filing_date"], e["code"])
        for e in v2["events"]
        if e["event_type"] in lc.TERMINAL
        and e["resolution_status"] == CONFIRMED
        and e["security_key"].startswith(r["entity_key"] + "|")
        and _plain(e["security_key"])
        and _family(e["security_key"]) == _family(key)
        and lo <= (e["filing_date"] or e["not_before"] or "") <= hi
    )
    if len(ends) == 1 and ends[0][0]:
        return ends[0][0], f"ISSUER_LEVEL_END:{ends[0][1]}"
    return None, None


def removal_filed(
    r: dict[str, Any], cut: str | None, why: str | None, v2: dict[str, Any] | None
) -> str | None:
    """The filing date of the notice behind a member's exit: the removal (Form 25) of its
    recorded or issuer-level end, the successor notice of a conversion, or the X4.4 notice."""
    if not why:
        return None
    kind, _, code = why.partition(":")
    if kind == "RECORDED_END":
        ends = (r.get("lifecycle") or {}).get("ends") or []
        return next((e["filing_date"] for e in ends if e["code"] == code), None)
    if kind == "ISSUER_LEVEL_END":
        events = (v2 or {}).get("events", [])
        return next((e["filing_date"] for e in events if e["code"] == code), None)
    return cut  # a conversion or an X4.4 notice: its cut is its filing date


def slots(universe: dict[str, Any], v2: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every confirmed membership slot: identity, window, exit, flags. Unresolved slots are
    listed apart and never receive data."""
    out = []
    for key, f in sorted(universe["formations"].items()):
        lo, hi = f["window"]
        for r in f["records"]:
            if r["decision"] != "MEMBER":
                continue
            cut, why = exit_cut(r)
            if cut is None:
                cut, why = issuer_exit(r, v2, lo, hi)
            if cut is None:
                pn = [
                    n
                    for n in r.get("priority_notices") or []
                    if n["impact"] == "MEMBER_END_EVIDENCE"
                ]
                if pn:
                    cut, why = min(n["filed"] for n in pn), "X44_PRIORITY_NOTICE_END"
            lf = r.get("lifecycle") or {}
            out.append(
                {
                    "slot": f"{key}|{r['ticker']}",
                    "pool": f["pool"],
                    "formation": f["formation"],
                    "ticker": r["ticker"],
                    "security_key": r["security_key"],
                    "ledger_key": lf.get("ledger_key") or r["security_key"],
                    "entity_key": r["entity_key"],
                    "venue": r["venue"],
                    "window": [lo, hi],
                    "exit_cut": cut,
                    "exit_reason": why,
                    "removal_filed": removal_filed(r, cut, why, v2),
                    "end": min(hi, cut) if cut else hi,
                    "continuity": lf.get("continuity") or [],
                    "flags": r["flags"],
                    "identity_confidence": r["identity_confidence"],
                }
            )
    return out


def runs(sl: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Request groups: one per formation and window (asof = the formation)."""
    out: dict[str, dict[str, Any]] = {}
    for s in sl:
        k = f"{s['pool']}_{s['formation']}"
        r = out.setdefault(
            k,
            {
                "asof": s["formation"],
                "start": s["window"][0],
                "end": s["window"][1],
                "tickers": set(),
            },
        )
        r["tickers"].add(s["ticker"])
    return {k: {**v, "tickers": sorted(v["tickers"])} for k, v in sorted(out.items())}


# ---------------------------------------------------------------------- capture (GET only)
def _bars(
    client: Any, symbols: list[str], start: str, end: str, asof: str, adj: str
) -> dict[str, Any]:
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": f"{start}T00:00:00Z",
        "end": f"{end}T23:59:59Z",
        "feed": "sip",
        "adjustment": adj,
        "limit": "10000",
        "sort": "asc",
        "asof": asof,
    }
    pages: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        p = {**params, **({"page_token": token} if token else {})}
        code, body, error, attempts = x4._get(
            client, f"{sources.DATA_API_BASE_URL}/v2/stocks/bars", p
        )
        pages.append({"status": code, "error": error, "attempts": attempts, "body": body})
        token = body.get("next_page_token") if isinstance(body, dict) else None
        if error or not token:
            break
    return {
        "params": {k: v for k, v in params.items() if k != "symbols"},
        "symbols": symbols,
        "pages": pages,
        "ok": not any(p["error"] for p in pages),
    }


def capture_bars(
    store: Store, universe: dict[str, Any], client: Any = None, v2: dict[str, Any] | None = None
) -> dict[str, Any]:
    own = client is None
    client = client or sources.alpaca_client()
    done: Counter[str] = Counter()
    try:
        for name, r in runs(slots(universe, v2)).items():
            for i in range(0, len(r["tickers"]), 100):
                part = r["tickers"][i : i + 100]
                for adj in ADJUSTMENTS:
                    tag = x4.sha256(",".join(part).encode())[:12]
                    rel = f"provider/bars/{name}/{adj}/{tag}.json.gz"
                    if store.has(rel):
                        continue
                    body = _bars(client, part, r["start"], r["end"], r["asof"], adj)
                    store.put_json(
                        rel,
                        body,
                        {
                            "kind": "alpaca_bars",
                            "run": name,
                            "asof": r["asof"],
                            "adjustment": adj,
                            "start": r["start"],
                            "end": r["end"],
                            "tickers": len(part),
                        },
                    )
                    done[adj] += 1
            store.save()
    finally:
        if own:
            client.close()
    return dict(done)


def capture_recovery(
    store: Store, needs: list[dict[str, Any]], client: Any = None
) -> dict[str, Any]:
    own = client is None
    client = client or sources.alpaca_client()
    done = 0
    try:
        for n in needs:
            for adj in ADJUSTMENTS:
                key = f"{n['ticker']}|{n['asof']}|{n['start']}|{n['end']}|{adj}"
                rel = f"provider/recovery/{x4.sha256(key.encode())[:16]}.json.gz"
                if store.has(rel):
                    continue
                body = _bars(client, [n["ticker"]], n["start"], n["end"], n["asof"], adj)
                store.put_json(
                    rel,
                    {**body, "need": n},
                    {
                        "kind": "alpaca_bars_recovery",
                        "ticker": n["ticker"],
                        "asof": n["asof"],
                        "adjustment": adj,
                        "reason": n["reason"],
                    },
                )
                done += 1
        store.save()
    finally:
        if own:
            client.close()
    return {"requests": done}


def capture_actions(
    store: Store, universe: dict[str, Any], v2: dict[str, Any], client: Any = None
) -> dict[str, Any]:
    sl = slots(universe, v2)
    tickers = {s["ticker"] for s in sl}
    keys = {s["ledger_key"] for s in sl}
    for e in v2["events"]:
        if e["security_key"] in keys and e["event_type"] == "TICKER_CHANGE":
            tickers |= {e["from_ticker"], e["to_ticker"]}
    tickers.discard(None)
    lo, hi = dividends.process_window(date(2017, 1, 3), date(2019, 12, 31))
    own = client is None
    client = client or sources.alpaca_client()
    done = 0
    try:
        ts = sorted(tickers)
        for i in range(0, len(ts), 50):
            part = ts[i : i + 50]
            rel = f"provider/corporate_actions/{x4.sha256(','.join(part).encode())[:12]}.json.gz"
            if store.has(rel):
                continue
            pages: list[dict[str, Any]] = []
            token: str | None = None
            while True:
                p = {
                    "symbols": ",".join(part),
                    "types": CA_TYPES,
                    "start": lo.isoformat(),
                    "end": hi.isoformat(),
                    "limit": "1000",
                    **({"page_token": token} if token else {}),
                }
                code, body, error, attempts = x4._get(
                    client, f"{sources.DATA_API_BASE_URL}{dividends.PATH}", p
                )
                pages.append({"status": code, "error": error, "attempts": attempts, "body": body})
                token = body.get("next_page_token") if isinstance(body, dict) else None
                if error or not token:
                    break
            store.put_json(
                rel,
                {"symbols": part, "pages": pages},
                {
                    "kind": "alpaca_corporate_actions",
                    "types": CA_TYPES,
                    "start": lo.isoformat(),
                    "end": hi.isoformat(),
                    "note": "start/end filter on the processing date (D-244)",
                },
            )
            done += 1
        store.save()
    finally:
        if own:
            client.close()
    return {"artifacts": done, "tickers": len(ts)}


# ---------------------------------------------------------------------- reading the evidence
def _parse(doc: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pg in doc["pages"]:
        for sym, rows in ((pg.get("body") or {}).get("bars") or {}).items():
            out[sym].extend(rows)
    return out


def bars_by_run(store: Store) -> dict[tuple[str, str], dict[str, dict[str, dict[str, Any]]]]:
    """(run, adjustment) -> ticker -> session -> bar, with the artifact of each bar."""
    out: dict[tuple[str, str], dict[str, dict[str, dict[str, Any]]]] = defaultdict(dict)
    for rel in sorted(k for k in store.entries if k.startswith("provider/bars/")):
        _, _, run, adj, _ = rel.split("/")
        for sym, rows in _parse(x4.read_json(store.path(rel))).items():
            d = out[(run, adj)].setdefault(sym, {})
            for b in rows:
                day = b["t"][:10]
                d.setdefault(day, {**b, "_artifact": rel, "_dup": 0})["_dup"] += 1
    return out


def recovery_docs(store: Store) -> list[dict[str, Any]]:
    out = []
    for rel in sorted(k for k in store.entries if k.startswith("provider/recovery/")):
        doc = x4.read_json(store.path(rel))
        bars: dict[str, dict[str, Any]] = {}
        for rows in _parse(doc).values():
            for b in rows:
                bars.setdefault(b["t"][:10], {**b, "_artifact": rel})
        out.append(
            {
                "rel": rel,
                "need": doc["need"],
                "adjustment": doc["params"]["adjustment"],
                "bars": bars,
            }
        )
    return out


def actions(store: Store) -> list[dict[str, Any]]:
    out = []
    for rel in sorted(k for k in store.entries if k.startswith("provider/corporate_actions/")):
        for pg in x4.read_json(store.path(rel))["pages"]:
            for kind, rows in sorted(
                ((pg.get("body") or {}).get("corporate_actions") or {}).items()
            ):
                for i, r in enumerate(rows or []):
                    out.append({"type": kind, "artifact": rel, "index": i, **r})
    return out


# ---------------------------------------------------------------------- per-slot assembly
def _sessions(lo: str, hi: str) -> list[str]:
    return [
        s.day.isoformat() for s in sessions_between(date.fromisoformat(lo), date.fromisoformat(hi))
    ]


def _quality(
    days: list[str], raw: dict[str, dict[str, Any]], expected: list[str]
) -> dict[str, Any]:
    bad_ohlc, nonpos, dups, zero = [], [], [], []
    for d in days:
        b = raw[d]
        o, h, l_, c, v = (float(b.get(k) or 0) for k in ("o", "h", "l", "c", "v"))
        if min(o, h, l_, c) <= 0:
            nonpos.append(d)
        elif not (l_ <= min(o, c) and max(o, c) <= h):
            bad_ohlc.append(d)
        if b.get("_dup", 1) > 1:
            dups.append(d)
        if v <= 0:
            zero.append(d)
    have = set(days)
    missing = [d for d in expected if d not in have]
    gaps: list[list[Any]] = []
    run: list[str] = []
    for d in expected:
        if d in have:
            if len(run) >= GAP_SESSIONS:
                gaps.append([run[0], run[-1], len(run)])
            run = []
        else:
            run.append(d)
    off_calendar = sorted(have - set(expected))
    return {
        "bars": len(days),
        "expected_sessions": len(expected),
        "missing_sessions": len(missing),
        "missing_sample": missing[:10],
        "gaps": gaps,
        "trailing_missing": len(run),
        "zero_volume_placeholders": len(zero),
        "invalid_ohlc": bad_ohlc[:10],
        "invalid_ohlc_count": len(bad_ohlc),
        "non_positive": nonpos[:10],
        "duplicate_sessions": dups[:10],
        "off_calendar_sessions": off_calendar[:10],
    }


def split_checks(
    days: list[str],
    raw: dict[str, dict[str, Any]],
    spl: dict[str, dict[str, Any]],
    acts: list[dict[str, Any]],
    tickers: set[str],
) -> dict[str, Any]:
    """Provider split records against the raw prices; the provider's implied split factor
    against the records; verified factors only."""
    recs = []
    lo, hi = (days[0], days[-1]) if days else ("", "")
    for a in acts:
        if a["type"] not in ("forward_splits", "reverse_splits", "unit_splits", "stock_dividends"):
            continue
        if a.get("symbol") not in tickers or not (lo < a.get("ex_date", "") <= hi):
            continue
        ex = a["ex_date"]
        prev = max((d for d in days if d < ex), default=None)
        if prev is None or ex not in raw:
            recs.append({**_ca(a), "verdict": "UNVERIFIABLE", "why": "no bars around the ex-date"})
            continue
        ratio = float(a.get("new_rate") or 1) / float(a.get("old_rate") or 1)
        raw_ratio = float(raw[ex]["c"]) / float(raw[prev]["c"])
        ok = abs(raw_ratio * ratio - 1) < 0.25
        recs.append(
            {
                **_ca(a),
                "raw_ratio": round(raw_ratio, 4),
                "record_ratio": round(ratio, 6),
                "verdict": "VERIFIED_BY_RAW_PRICES" if ok else "PROVIDER_ADJUSTMENT_ARTIFACT",
                "why": "the raw close moves by the record's ratio"
                if ok
                else "the raw close does not move by the record's ratio: not applied; the "
                "return across it is unusable",
            }
        )
    # The provider's implied factor (split close / raw close) changes only at records.
    changes = []
    prev_f = None
    for d in days:
        if d not in spl or float(raw[d]["c"]) <= 0:
            continue
        f = float(spl[d]["c"]) / float(raw[d]["c"])
        if prev_f is not None and abs(f / prev_f - 1) > 0.02:
            changes.append(
                {
                    "date": d,
                    "factor_before": round(prev_f, 6),
                    "factor_after": round(f, 6),
                    "record": any(r["ex_date"] == d for r in recs),
                }
            )
        prev_f = f
    return {
        "records": recs,
        "provider_factor_changes": changes,
        "unrecorded_factor_changes": [c for c in changes if not c["record"]],
    }


def _ca(a: dict[str, Any]) -> dict[str, Any]:
    return {
        k: a.get(k)
        for k in ("type", "symbol", "ex_date", "old_rate", "new_rate", "artifact", "index")
    }


def _shares_history(
    same: list[str], differ: list[str], events: list[dict[str, Any]], need: dict[str, Any]
) -> bool:
    """A new ticker's series is this security's only if it matches the primary before the
    ticker change (at least 3 identical sessions) and diverges no earlier than the change."""
    ev = next((e for e in events if e["code"] == need.get("event")), None)
    start = (ev or {}).get("not_before") or ""
    pre = [d for d in same if d < differ[0]]
    return len(pre) >= 3 and differ[0] >= start


def classify_jumps(
    jm: list[dict[str, Any]], raw: dict[str, dict[str, Any]], dv: list[dict[str, Any]]
) -> None:
    """A jump explained by a captured cash distribution on its date (the close falls by about
    the rate) is a DISTRIBUTION; any other stays UNEXPLAINED. Neither is a usable price return."""
    for j in jm:
        prev = float(raw[j["from"]]["c"])
        hit = [
            d
            for d in dv
            if d["ex_date"] == j["to"]
            and abs(prev - float(d.get("rate") or 0) - float(raw[j["to"]]["c"])) / prev < 0.1
        ]
        j["finding"] = "DISTRIBUTION" if hit else "UNEXPLAINED"
        if hit:
            j["dividend_id"] = hit[0].get("id")


def jumps(
    days: list[str], raw: dict[str, dict[str, Any]], verified: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Raw close-to-close moves of 3x or more, after verified splits only."""
    factor = {r["ex_date"]: r["record_ratio"] for r in verified}
    out = []
    for a, b in itertools.pairwise(days):
        ca, cb = float(raw[a]["c"]), float(raw[b]["c"])
        if ca <= 0 or cb <= 0:
            continue
        r = cb / ca * factor.get(b, 1.0)
        if abs(math.log(r)) >= JUMP:
            out.append({"from": a, "to": b, "ratio": round(r, 4)})
    return out


def assemble(
    s: dict[str, Any],
    primary: dict[str, dict[str, Any]],
    primary_split: dict[str, dict[str, Any]],
    rec_docs: list[dict[str, Any]],
    acts: list[dict[str, Any]],
    divs: dict[str, list[dict[str, Any]]],
    v2_by_sec: dict[str, list[dict[str, Any]]],
    edges_by_sec: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    lo, end = s["window"][0], s["end"]
    filed: str | None = s.get("removal_filed")
    continues = filed is not None and lo < filed <= end and any(d > filed for d in primary)
    if filed is not None and continues:
        # The provider's series runs on past the removal or conversion filing: from that filing
        # it is another security's (a successor or the ticker's next holder), never the member's.
        end = [d for d in _sessions(lo, filed) if d < filed][-1]
    raw = {d: b for d, b in primary.items() if lo <= d <= end}
    spl = {d: b for d, b in primary_split.items() if lo <= d <= end}
    dropped_after = sorted(d for d in primary if d > end)
    source = {d: f"primary asof {s['formation']} ({s['ticker']})" for d in raw}
    recoveries = []
    # Ticker changes inside the window: the security's new ticker (same security per Ledger V2).
    tickers = {s["ticker"]}
    for e in v2_by_sec.get(s["ledger_key"], []):
        if (
            e["event_type"] == "TICKER_CHANGE"
            and e["resolution_status"] == CONFIRMED
            and ((e["not_before"] or "") <= end and (e["not_after"] or "9999") >= lo)
        ):
            tickers |= {e["from_ticker"], e["to_ticker"]}
    for doc in rec_docs:
        n = doc["need"]
        if n["slot"] != s["slot"] or doc["adjustment"] != "raw":
            continue
        rb = {d: b for d, b in doc["bars"].items() if lo <= d <= end}
        twin = next((x for x in rec_docs if x["need"] == n and x["adjustment"] == "split"), None)
        common = sorted(set(raw) & set(rb))
        same = [
            d
            for d in common
            if float(raw[d]["c"]) == float(rb[d]["c"]) and float(raw[d]["v"]) == float(rb[d]["v"])
        ]
        differ = [d for d in common if d not in set(same)]
        new = sorted(d for d in rb if d not in raw)
        held = _held(edges_by_sec, s, n["ticker"], lo, end)
        transfer = [
            e["code"]
            for e in v2_by_sec.get(s["ledger_key"], [])
            if e["event_type"] in ("EXCHANGE_TRANSFER", "TICKER_CHANGE")
            and e["resolution_status"] == CONFIRMED
            and (e["not_before"] or "") <= end
            and (e["not_after"] or "9999") >= lo
        ]
        last = max(raw) if raw else None
        adjacent = bool(new and last and len(_sessions(last, new[0])) == 2 and not common)
        if not held:
            verdict, why = (
                "REJECTED",
                "the requested ticker is not listed for this security then (J1)",
            )
        elif not new and not differ:
            verdict, why = "NOT_NEEDED", "the primary series already covers these sessions"
        elif (
            n["reason"] == "TICKER_CHANGE"
            and differ
            and not _shares_history(same, differ, v2_by_sec.get(s["ledger_key"], []), n)
        ):
            verdict, why = (
                "REJECTED",
                "the new ticker's series shares no identical history with the primary before "
                "the change: it is another security under that ticker at this mapping (J1, J2)",
            )
        elif n["reason"] == "TICKER_CHANGE" and differ:
            # The primary mapping followed the ticker to another security: the new ticker's
            # series (identical before the change) is this security's from the difference.
            cut = differ[0]
            for d in [d for d in raw if d >= cut]:
                raw.pop(d)
                spl.pop(d, None)
            new = sorted(d for d in rb if d not in raw)
            verdict, why = (
                "ACCEPTED_REPLACES_DIVERGENT_TAIL",
                (
                    f"the primary series differs from {n['ticker']}'s (the same security under "
                    f"its new ticker, {transfer}) from {cut}: the primary tail belonged to the "
                    "ticker's next holder and is dropped"
                ),
            )
        elif len(same) >= OVERLAP_MIN and not differ:
            verdict, why = "ACCEPTED_J2", f"{len(same)} identical overlapping sessions (J1, J2)"
        elif adjacent and transfer:
            verdict, why = (
                "ACCEPTED_J6",
                f"adjacent sessions at a documented seam {transfer} (J1, J6)",
            )
        else:
            verdict, why = (
                "REJECTED",
                (
                    f"{len(same)} identical and {len(differ)} differing overlapping sessions; "
                    "no join rule holds"
                ),
            )
        added = 0
        if verdict.startswith("ACCEPTED"):
            for d in new:
                raw[d] = rb[d]
                source[d] = f"recovery asof {n['asof']} ({n['ticker']})"
                if twin and d in twin["bars"]:
                    spl[d] = twin["bars"][d]
                added += 1
        recoveries.append(
            {
                "artifact": doc["rel"],
                "ticker": n["ticker"],
                "asof": n["asof"],
                "reason": n["reason"],
                "verdict": verdict,
                "why": why,
                "overlap_identical": len(same),
                "overlap_differing": len(differ),
                "bars_added": added,
            }
        )
    days = sorted(raw)
    expected = _sessions(lo, end)
    q = _quality(days, raw, expected)
    sc = split_checks(days, raw, spl, acts, tickers)
    verified = [r for r in sc["records"] if r["verdict"] == "VERIFIED_BY_RAW_PRICES"]
    jm = jumps(days, raw, verified)
    unusable = sorted(
        {r["ex_date"] for r in sc["records"] if r["verdict"] == "PROVIDER_ADJUSTMENT_ARTIFACT"}
        | {j["to"] for j in jm}
    )
    dv = sorted(
        (d for t in tickers for d in divs.get(t, []) if lo <= d["ex_date"] <= end),
        key=lambda d: (d["ex_date"], str(d.get("id"))),
    )
    classify_jumps(jm, raw, dv)
    # A provider factor change with no record, at a seam between two mapping segments, is the
    # segments' different adjustment bases (split-adjusted columns are not comparable across
    # mappings): the raw prices are unaffected and are what the verified factor uses.
    for c in sc["unrecorded_factor_changes"]:
        prev = max((x for x in days if x < c["date"]), default=None)
        c["cause"] = (
            "MAPPING_SEAM_ADJUSTMENT_BASE"
            if prev and source.get(prev) != source.get(c["date"])
            else "UNEXPLAINED"
        )
    status, last_traded = data_status(days, raw, q, s, recoveries)
    return {
        **{
            k: s[k]
            for k in (
                "slot",
                "pool",
                "formation",
                "ticker",
                "security_key",
                "ledger_key",
                "entity_key",
                "window",
                "exit_cut",
                "exit_reason",
                "flags",
                "identity_confidence",
            )
        },
        "end": end,
        "removal_filed": filed,
        "ends_before_removal_filing": continues,
        "tickers": sorted(tickers),
        "last_traded": last_traded,
        "status": status,
        "quality": q,
        "coverage": round(len(days) / len(expected), 6) if expected else 0.0,
        "dropped_after_end": len(dropped_after),
        "recoveries": recoveries,
        "splits": sc,
        "jumps": jm,
        "unusable_return_dates": unusable,
        "dividends": [
            {k: d.get(k) for k in ("symbol", "ex_date", "rate", "special", "id", "artifact")}
            for d in dv
        ],
        "bars": {
            "columns": [
                "session",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "vwap",
                "trades",
                "split_close",
                "source",
            ],
            "rows": [
                [
                    d,
                    raw[d]["o"],
                    raw[d]["h"],
                    raw[d]["l"],
                    raw[d]["c"],
                    raw[d]["v"],
                    raw[d].get("vw"),
                    raw[d].get("n"),
                    spl[d]["c"] if d in spl else None,
                    source[d],
                ]
                for d in days
            ],
        },
        "provenance": sorted({b["_artifact"] for b in raw.values()}),
    }


def _held(
    edges_by_sec: dict[str, list[dict[str, Any]]], s: dict[str, Any], ticker: str, lo: str, hi: str
) -> bool:
    """J1: the ticker is listed for this security (or its issuer, for a company-list key)
    around the window."""
    keys = {s["security_key"], s["ledger_key"]}
    for k in keys:
        for e in edges_by_sec.get(k, []):
            if (
                e["ticker"] == ticker
                and e["first_observed"] <= _plus(hi, 400)
                and (e["last_observed"] >= _plus(lo, -400))
            ):
                return True
    return False


def _plus(d: str, days: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


def data_status(
    days: list[str],
    raw: dict[str, dict[str, Any]],
    q: dict[str, Any],
    s: dict[str, Any],
    recoveries: list[dict[str, Any]],
) -> tuple[str, str | None]:
    """The slot's data status, and the last session with a recorded trade.

    COMPLETE: a bar for every expected session. COMPLETE_TO_EXIT: the missing sessions all
    follow the last bar and precede a recorded exit. STOPPED_TRADING(_NO_RECORDED_EXIT): the
    series ends in a run of zero-volume placeholder bars. STOPS_EARLY_NO_RECORDED_EXIT: the
    data ends early, no recorded exit, and recovery found no later bars. EXITED_AT_WINDOW_START:
    no bars, and the recorded exit is at the window's start. INCOMPLETE: sessions missing inside
    the series. NO_DATA: no bars and no recorded exit."""
    traded = [d for d in days if float(raw[d].get("v") or 0) > 0]
    last_traded = traded[-1] if traded else None
    tail_placeholders = len([d for d in days if last_traded is None or d > last_traded])
    if not days:
        return ("EXITED_AT_WINDOW_START" if s["exit_cut"] else "NO_DATA"), None
    if tail_placeholders >= GAP_SESSIONS:
        return (
            "STOPPED_TRADING" if s["exit_cut"] else "STOPPED_TRADING_NO_RECORDED_EXIT"
        ), last_traded
    if q["missing_sessions"] == 0:
        return "COMPLETE", last_traded
    if q["trailing_missing"] == q["missing_sessions"]:
        if s["exit_cut"]:
            return "COMPLETE_TO_EXIT", last_traded
        if recoveries:
            return "STOPS_EARLY_NO_RECORDED_EXIT", last_traded
    return "INCOMPLETE", last_traded


def recovery_needs(
    universe: dict[str, Any], store: Store, v2: dict[str, Any]
) -> list[dict[str, Any]]:
    """Slots whose primary data stops before the window's end with no recorded exit, and slots
    with a ticker change inside the window. Each need names the ticker and mapping to try."""
    br = bars_by_run(store)
    by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in v2["events"]:
        by_sec[e["security_key"]].append(e)
    out = []
    for s in slots(universe, v2):
        run = f"{s['pool']}_{s['formation']}"
        prim = br.get((run, "raw"), {}).get(s["ticker"], {})
        days = sorted(d for d in prim if s["window"][0] <= d <= s["end"])
        expected = _sessions(s["window"][0], s["end"])
        for e in by_sec.get(s["ledger_key"], []):
            if (
                e["event_type"] == "TICKER_CHANGE"
                and e["resolution_status"] == CONFIRMED
                and (
                    (e["not_before"] or "") <= s["end"]
                    and (e["not_after"] or "9999") >= s["window"][0]
                )
            ):
                out.append(
                    {
                        "slot": s["slot"],
                        "ticker": e["to_ticker"],
                        # mapped after the change, when the new ticker is this security's
                        "asof": max(s["end"], e["not_after"] or s["end"]),
                        "start": max(s["window"][0], e["not_before"] or s["window"][0]),
                        "end": s["end"],
                        "reason": "TICKER_CHANGE",
                        "event": e["code"],
                    }
                )
        if days and days[-1] < expected[-1] and not s["exit_cut"]:
            nxt = expected[expected.index(days[-1]) + 1] if days[-1] in expected else days[-1]
            start = (
                expected[max(0, expected.index(days[-1]) - 40)]
                if days[-1] in expected
                else s["window"][0]
            )
            for asof in sorted({nxt, s["end"]}):
                out.append(
                    {
                        "slot": s["slot"],
                        "ticker": s["ticker"],
                        "asof": asof,
                        "start": start,
                        "end": s["end"],
                        "reason": "TRUNCATED_HISTORY",
                    }
                )
        if not days:
            out.append(
                {
                    "slot": s["slot"],
                    "ticker": s["ticker"],
                    "asof": s["end"],
                    "start": s["window"][0],
                    "end": s["end"],
                    "reason": "NO_PRIMARY_DATA",
                }
            )
    seen, uniq = set(), []
    for n in out:
        k = (n["slot"], n["ticker"], n["asof"], n["start"], n["end"])
        if k not in seen:
            seen.add(k)
            uniq.append(n)
    return uniq


# ---------------------------------------------------------------------- build
KNOWN_CASES = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def build(
    store: Store,
    universe: dict[str, Any],
    v2: dict[str, Any],
    graph2: dict[str, Any],
    universe_sha256: str = UNIVERSE_SHA256,
) -> dict[str, Any]:
    sl = slots(universe, v2)
    br = bars_by_run(store)
    rec = recovery_docs(store)
    acts = actions(store)
    divs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    for a in acts:
        if a["type"] == "cash_dividends" and a.get("id") not in seen:
            seen.add(a.get("id"))
            divs[a["symbol"]].append(a)
    v2_by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in v2["events"]:
        v2_by_sec[e["security_key"]].append(e)
    edges_by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in graph2["ticker_edges"]:
        edges_by_sec[e["security_key"]].append(e)
        edges_by_sec[f"{e['entity_key']}|*"].append(e)
    out = []
    for s in sl:
        run = f"{s['pool']}_{s['formation']}"
        out.append(
            assemble(
                s,
                br.get((run, "raw"), {}).get(s["ticker"], {}),
                br.get((run, "split"), {}).get(s["ticker"], {}),
                rec,
                acts,
                divs,
                v2_by_sec,
                edges_by_sec,
            )
        )
    known = []
    for t, d in sorted(KNOWN_CASES.items()):
        hits = [x for x in out if t in x["tickers"] and x["window"][0] <= d <= x["end"]]
        known.append(
            {
                "ticker": t,
                "date": d,
                "member_slots": [x["slot"] for x in hits],
                "impact": "the return across the date is marked unusable"
                if any(d in x["unusable_return_dates"] for x in hits)
                else (
                    "inside a member window; not flagged"
                    if hits
                    else "outside every confirmed member's window: no member data affected"
                ),
            }
        )
    unresolved = [
        {"slot": f"{k}|{r['ticker']}", "decision": r["decision"]}
        for k, f in sorted(universe["formations"].items())
        for r in f["records"]
        if r["decision"] == "MEMBER_IDENTITY_UNRESOLVED"
    ]
    doc: dict[str, Any] = {
        "kind": "x45_member_data",
        "decision": "D-251",
        "dataset_code": "HISTORICAL_MEMBER_DATA_V1",
        "method": METHOD,
        "inputs": {
            "universe_sha256": universe_sha256,
            "universe_content_sha256": universe["content_sha256"],
            "x45_manifest_sha256": x4.sha256(store.manifest_path.read_bytes())
            if store.manifest_path.exists()
            else None,
            "ledger_v2_sha256": g2.LEDGER_V2_SHA256,
            "graph_v2_sha256": u.GRAPH2_SHA256,
        },
        "slots": out,
        "excluded_unresolved_slots": unresolved,
        "known_cases": known,
    }
    doc["summary"] = summarize(out, unresolved)
    doc["content_sha256"] = content_sha256(doc)
    return doc


def summarize(out: list[dict[str, Any]], unresolved: list[dict[str, Any]]) -> dict[str, Any]:
    by_f: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in out:
        by_f[f"{x['pool']}|{x['formation']}"].append(x)

    def agg(xs: list[dict[str, Any]]) -> dict[str, Any]:
        exp = sum(x["quality"]["expected_sessions"] for x in xs)
        have = sum(x["quality"]["bars"] for x in xs)
        return {
            "slots": len(xs),
            "bars": have,
            "expected_sessions": exp,
            "coverage": round(have / exp, 6) if exp else 0.0,
            "status": dict(sorted(Counter(x["status"] for x in xs).items())),
            "zero_volume_placeholders": sum(x["quality"]["zero_volume_placeholders"] for x in xs),
            "invalid_ohlc": sum(x["quality"]["invalid_ohlc_count"] for x in xs),
            "gaps": sum(len(x["quality"]["gaps"]) for x in xs),
            "dividends": sum(len(x["dividends"]) for x in xs),
            "split_records": dict(
                sorted(Counter(r["verdict"] for x in xs for r in x["splits"]["records"]).items())
            ),
            "unrecorded_provider_factor_changes": sum(
                len(x["splits"]["unrecorded_factor_changes"]) for x in xs
            ),
            "jumps": sum(len(x["jumps"]) for x in xs),
            "recoveries": dict(
                sorted(Counter(r["verdict"] for x in xs for r in x["recoveries"]).items())
            ),
            "bars_recovered": sum(r["bars_added"] for x in xs for r in x["recoveries"]),
        }

    return {
        "confirmed_slots": len(out),
        "excluded_unresolved_slots": len(unresolved),
        "securities": len({x["ledger_key"] for x in out}),
        "all": agg(out),
        "by_formation": {k: agg(v) for k, v in sorted(by_f.items())},
    }


def content_sha256(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "content_sha256"}
    return hashlib.sha256(x4.canonical(rs._plain(body))).hexdigest()


def write(doc: dict[str, Any], path: Path = DATA_PATH) -> str:
    if path.exists():
        raise FileExistsError(f"{path} exists: the member data is written once")
    data = x4.gz(x4.canonical(rs._plain(doc)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def verify(path: Path, store: Store) -> dict[str, Any]:
    stored = x4.read_json(path)
    bad = [
        rel
        for rel, e in sorted(store.entries.items())
        if x4.sha256(store.path(rel).read_bytes()) != e["sha256"]
    ]
    rebuilt = build(store, load_universe(), g2.load_ledger_v2(), u.load_graph_v2())
    return {
        "file_sha256": l2._sha_file(path),
        "evidence_artifacts": len(store.entries),
        "evidence_hash_failures": bad,
        "stored_content_sha256": stored["content_sha256"],
        "recomputed_content_sha256": content_sha256(stored),
        "rebuilt_content_sha256": rebuilt["content_sha256"],
        "ok": not bad
        and stored["content_sha256"] == content_sha256(stored) == rebuilt["content_sha256"],
    }


__all__ = [
    "DATA_PATH",
    "METHOD",
    "ROOT",
    "Store",
    "assemble",
    "build",
    "capture_actions",
    "capture_bars",
    "capture_recovery",
    "recovery_needs",
    "slots",
    "verify",
    "write",
]
