"""X4.6.1 (D-253): the corrected universe (X4.4 V2), member data (X4.5 V2) and terminal values.

Every input is pinned by SHA-256; X4.4 V1, X4.5 V1 and X4.6 V1 are read, never written. Each
correction is evidence-based and recorded with its provenance:

* **Eligibility.** A candidate whose confirmed delisting was filed on or before the formation,
  and whose last traded bar precedes it, was not listed at the formation: not eligible. A
  ticker whose formation-window series is spliced across share classes (X2's registered splice
  CA-10) is measured on its own class's sessions only. The top N is then recomputed with X4.4's
  unchanged rule (:func:`universe_x44.rank_and_decide`); members it admits are labelled.
REDACTED_FOR_GITHUB
  the SEC class the SEC's own class files give the ticker before and after the formation.
REDACTED_FOR_GITHUB
  successor class is first evidenced on 2018-11-23 and the reorganisation date is not in the
  REDACTED_FOR_GITHUB
* **Dividends** under a ticker that changed hands are kept only after the documented handover;
  one on the handover session itself is not attributable.
* **Terminal values** are the terms the issuers' own filings state, quoted. Each quote is checked
  against the stored filing at build time; a number not in a quote is never recorded.
"""

# ruff: noqa: E501  (the evidence tables quote filings verbatim; quotes are not wrapped)
from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.backtesting.data import sessions_between
from app.research import exclusions_x2
from app.research import graph_v2 as g2
from app.research import ledger_v2 as l2
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import universe_x44 as u
from app.research import x4_evidence as x4
from app.research import x45_data as x45d
from app.research import x461_corrections as x461c

DECISION = "D-253"
UNIVERSE_V2_PATH = lc.OUT / "x44_historical_universe_v2.json.gz"
DATA_V2_PATH = lc.OUT / "x45_member_data_v2.json.gz"
UNIVERSE_V1_SHA256 = "6ad21763c8b5dc1ddaf4c07161f6fafcd5b74e2379491b2bfc10f57aabab26d7"
DATA_V1_SHA256 = "9778c5f2802abfe90d80bbb9d94880f79675131120853e87cad5e13c82a511f4"
SIZES = {"STOCK": 500, "FUND": 150}

# X2's registered splice (D-242, CA-10) names the tickers it touched: the UA ticker moved to
# class C on 2016-12-07 and class A became UAA. Their own-class series start at that session.
SPLICE_TICKERS = {"CA-10": ("UA", "UAA")}

# ---------------------------------------------------------------------- evidence tables
# Ticker handovers documented by the issuers' filings (stored in the X4.6.1 store).
HANDOVERS: dict[str, dict[str, Any]] = {
    "REDACTED_FOR_GITHUB": {
        "previous_holder": "CIK:0001175580|ADS",
        "previous_holder_name": "REDACTED_FOR_GITHUB",
        "new_holder_entity": "CIK:0000756894",
        "new_holder_name": "REDACTED_FOR_GITHUB",
        "handover_session": "2019-01-02",
        "evidence": [
            ["0001144204-18-066704",
             "REDACTED_FOR_GITHUB"
             "today."],
            ["0001144204-19-000112", "today, 2 January 2019"],
            ["0001144204-19-000112",
             "REDACTED_FOR_GITHUB"
             "(ET) today."],
            ["0001144204-19-000112", "at or around 9:30 a.m. (ET) today"],
            ["0001193125-19-000759",
             "REDACTED_FOR_GITHUB"
             "REDACTED_FOR_GITHUB"],
        ],
    },
}  # fmt: skip

# REDACTED_FOR_GITHUB
IDENTITY_CORRECTIONS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection

# REDACTED_FOR_GITHUB
SUCCESSOR_BOUNDARIES = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection

# Terminal consideration per exit, as the filings state it (sources: accession, quote).
CASH, CASH_STOCK, STOCK, CONTINUATION = "CASH", "CASH_AND_STOCK", "STOCK", "CONTINUATION_1_TO_1"
CVR, ELECTION, UNKNOWN, BOUNDARY = "CASH_STOCK_AND_CVR", "ELECTION_OR_TENDER", "UNKNOWN", "SUCCESSOR_BOUNDARY_UNDATED"  # fmt: skip
TERMS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
VALUE_STATUS = {
    CASH: "DOCUMENTED_CASH",
    CASH_STOCK: "DOCUMENTED_TERMS_NEED_ACQUIRER_PRICE",
    STOCK: "DOCUMENTED_TERMS_NEED_ACQUIRER_PRICE",
    CONTINUATION: "DOCUMENTED_TERMS_NEED_SUCCESSOR_PRICE",
    CVR: "PARTLY_DOCUMENTED_CVR_UNVALUED",
    ELECTION: "AMBIGUOUS",
    UNKNOWN: "UNKNOWN",
    BOUNDARY: "UNKNOWN",
}


# ---------------------------------------------------------------------- helpers
def _day(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def _sessions(lo: str, hi: str) -> list[str]:
    if hi < lo:
        return []
    days = sessions_between(date.fromisoformat(lo), date.fromisoformat(hi))
    return [s.day.isoformat() for s in days]


def _norm(text: str) -> str:
    text = re.sub(r"<[^>]{0,3000}>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text))


def load_pinned(path: Path, expect: str) -> dict[str, Any]:
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != expect:
        raise ValueError(f"{path}: sha256 {got} is not the reported {expect}")
    doc: dict[str, Any] = x4.read_json(path)
    return doc


class Documents:
    """Stored filing texts (X4.6.1 filings; X4.1 removal notices and SEC class files), each
    hash-checked against its manifest once."""

    def __init__(self, x4s: x4.Store, x461s: x4.Store) -> None:
        self.x4s, self.x461s = x4s, x461s
        self._texts: dict[str, str] = {}
        self._notices: dict[str, str] | None = None
        self.read: dict[str, str] = {}
        self.failures: list[str] = []

    def _bytes(self, store: x4.Store, rel: str) -> bytes:
        raw = store.path(rel).read_bytes()
        got = hashlib.sha256(raw).hexdigest()
        if got != store.entries[rel]["sha256"]:
            self.failures.append(rel)
        self.read[f"{store.root.as_posix()}/{rel}"] = got
        return raw

    def filing(self, acc: str) -> str:
        if acc in self._texts:
            return self._texts[acc]
        rel = f"sec/filings/{acc}.txt.gz"
        if self.x461s.has(rel):
            text = gzip.decompress(self._bytes(self.x461s, rel)).decode("latin-1")
        else:
            text = self.notices().get(acc, "")
        self._texts[acc] = _norm(text)
        return self._texts[acc]

    def notices(self) -> dict[str, str]:
        if self._notices is None:
            self._notices = {}
            for rel in sorted(k for k in self.x4s.entries if k.startswith("sec/form25/")):
                data = gzip.decompress(self._bytes(self.x4s, rel)).decode("utf-8")
                for line in data.splitlines():
                    if line.strip():
                        r = json.loads(line)
                        acc = r["path"].rsplit("/", 1)[1].removesuffix(".txt")
                        self._notices.setdefault(acc, r.get("text") or "")
        return self._notices

    def store_text(self, store: str, rel: str) -> str:
        key = f"{store}:{rel}"
        if key not in self._texts:
            s = self.x4s if store == "x4" else self.x461s
            raw = self._bytes(s, rel)
            self._texts[key] = (gzip.decompress(raw) if rel.endswith(".gz") else raw).decode(
                "latin-1"
            )
        return self._texts[key]

    def quote_ok(self, acc: str, quote: str) -> bool:
        return re.sub(r"\s+", " ", quote) in self.filing(acc)


# ---------------------------------------------------------------------- universe V2
def own_series_metrics(
    bars: dict[str, tuple[float, float]], start: str, rule: dict[str, Any], formation: str
) -> dict[str, Any]:
    """A ticker's formation metrics from its own-class sessions only: from ``start`` to the
    formation, never after it (no look-ahead)."""
    keep = sorted((d, c, v) for d, (c, v) in bars.items() if start <= d <= formation)
    mdv = statistics.median(c * v for _, c, v in keep) if keep else 0.0
    return {
        "traded_bars": len(keep),
        "first_bar": keep[0][0] if keep else None,
        "last_bar": keep[-1][0] if keep else None,
        "last_close": keep[-1][1] if keep else None,
        "median_dollar_volume": round(mdv, 2),
    }


def removed_before_formation(
    r: dict[str, Any], formation: str, by_key: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """A confirmed delisting of the candidate's security filed on or before the formation (in
    the year before it), while its last traded bar precedes the formation: not listed at the
    formation, so not investable."""
    keys = {r["security_key"], (r.get("lifecycle") or {}).get("ledger_key")}
    for k in sorted(x for x in keys if x):
        for e in by_key.get(k, []):
            if (
                e["event_type"] in lc.TERMINAL
                and e["resolution_status"] == rs.CONFIRMED
                and e["filing_date"]
                and _day(formation, -366) <= e["filing_date"] <= formation
                and r["last_bar"]
                and r["last_bar"] < formation
            ):
                return {"removal": e["code"], "filed": e["filing_date"], "last_bar": r["last_bar"]}
    return None


def _apply_reasons(r: dict[str, Any], rule: dict[str, Any]) -> None:
    reasons = [x for x in r["reasons"] if x not in ("FEW_TRADED_BARS", "LOW_DOLLAR_VOLUME",
                                                   "LOW_CLOSE")]  # fmt: skip
    if r["traded_bars"] < rule["min_bars"]:
        reasons.append("FEW_TRADED_BARS")
    if (r["last_close"] or 0) < float(rule["min_close"]):
        reasons.append("LOW_CLOSE")
    if r["median_dollar_volume"] < float(rule["min_median_dollar_volume"]):
        reasons.append("LOW_DOLLAR_VOLUME")
    r["reasons"] = reasons


def build_universe_v2(
    v1: dict[str, Any],
    ledger: dict[str, Any],
    graph2: dict[str, Any],
    sweep_bars: Callable[[str, set[str]], dict[str, dict[str, tuple[float, float]]]],
    docs: Documents | None = None,
) -> dict[str, Any]:
    """The X4.4 revision. ``sweep_bars(formation, tickers)`` returns the formation-window traded
    bars X4.4 read (X4.1 sweeps)."""
    doc: dict[str, Any] = json.loads(json.dumps(v1))
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in ledger["events"]:
        by_key[e["security_key"]].append(e)
    life = u.Lifecycle(ledger, graph2)
    corrections: list[dict[str, Any]] = []
    reproduced: dict[str, bool] = {}
    for key, f in sorted(doc["formations"].items()):
        pool, formation = f["pool"], f["formation"]
        recs = f["records"]
        before = {r["ticker"]: (r["decision"], r["rank"]) for r in recs}
        trial = json.loads(json.dumps(recs))
        u.rank_and_decide(trial, pool, SIZES[pool])
        reproduced[key] = all((t["decision"], t["rank"]) == before[t["ticker"]] for t in trial)
        changed: dict[str, dict[str, Any]] = {}
        for r in recs:
            hit = removed_before_formation(r, formation, by_key) if not r["reasons"] else None
            if hit:
                r["reasons"] = [*r["reasons"], "REMOVED_BEFORE_FORMATION"]
                changed[r["ticker"]] = {"rule": "REMOVED_BEFORE_FORMATION", "evidence": hit}
        for e in exclusions_x2.EVENTS:
            tickers = SPLICE_TICKERS.get(e.ref)
            d = e.session.isoformat()
            hits = [r for r in recs if tickers and r["ticker"] in tickers and r["first_bar"]
                    and r["first_bar"] <= d <= formation]  # fmt: skip
            if not hits:
                continue
            bars = sweep_bars(formation, {r["ticker"] for r in hits})
            for r in hits:
                fields = ("traded_bars", "median_dollar_volume", "first_bar", "last_bar",
                          "last_close")  # fmt: skip
                prev = {k: r[k] for k in fields}
                m = own_series_metrics(bars.get(r["ticker"], {}), d, f["rule"], formation)
                for k in fields:
                    r[k] = m[k]
                _apply_reasons(r, f["rule"])
                changed[r["ticker"]] = {
                    "rule": "OWN_CLASS_SERIES_AFTER_SPLICE",
                    "evidence": {"x2_event": e.ref, "splice_session": d,
                                 "metrics_recorded_v1": prev, "metrics_own_class": m},
                }  # fmt: skip
        for slot, c in IDENTITY_CORRECTIONS.items():
            if not slot.startswith(key + "|"):
                continue
            r = next(x for x in recs if x["ticker"] == c["ticker"])
            if r["security_key"] != c["from_security_key"]:
                raise ValueError(f"{slot}: identity {r['security_key']} is not the one corrected")
            ev = [_check_class_row(docs, *x) for x in c["evidence"]] if docs else []
            r["security_key"], r["entity_key"] = c["security_key"], c["entity_key"]
            r["identity_confidence"] = c["identity_confidence"]
            r["flags"] = [x for x in r["flags"] if x != "IDENTITY_MEDIUM"]
            if r.get("lifecycle"):
                r["lifecycle"]["ledger_key"] = c["security_key"]
            changed[r["ticker"]] = {"rule": "IDENTITY_CORRECTED", "evidence": {
                "from": c["from_security_key"], "to": c["security_key"], "why": c["why"],
                "sec_class_rows": ev}}  # fmt: skip
        cut = u.rank_and_decide(recs, pool, SIZES[pool])
        f["counts"]["decisions"] = dict(sorted(Counter(r["decision"] for r in recs).items()))
        f["counts"]["cutoff_median_dollar_volume"] = cut["cutoff_median_dollar_volume"]
        admitted = [r for r in recs
                    if r["decision"] == "MEMBER" and before[r["ticker"]][0] != "MEMBER"]  # fmt: skip
        lo, hi = (date.fromisoformat(x) for x in f["window"])
        for r in admitted:
            r["flags"] = []
        u.flag_members(admitted, life, (lo, hi), date.fromisoformat(formation))
        for r in recs:
            was = before[r["ticker"]]
            ch = changed.get(r["ticker"])
            if (r["decision"], r["rank"]) == was and not ch:
                continue
            rule = (ch or {}).get("rule") or (
                "ADMITTED_BY_RECOMPUTED_TOP_N" if r in admitted else "RANK_SHIFT_ONLY"
            )
            r["x461"] = {"previous_decision": was[0], "previous_rank": was[1], "rule": rule,
                         **({"evidence": ch["evidence"]} if ch else {})}  # fmt: skip
            if ch or was[0] != r["decision"]:
                corrections.append({"formation": key, "ticker": r["ticker"],
                                    "previous_decision": was[0], "decision": r["decision"],
                                    "previous_rank": was[1], "rank": r["rank"], "rule": rule,
                                    **({"evidence": ch["evidence"]} if ch else {})})  # fmt: skip
    doc["universe_code"] = "HISTORICAL_UNIVERSE_V2"
    doc["decision"] = DECISION
    doc["supersedes"] = {"universe_code": v1["universe_code"], "sha256": UNIVERSE_V1_SHA256,
                         "content_sha256": v1["content_sha256"]}  # fmt: skip
    doc["corrections"] = sorted(corrections, key=lambda c: (c["formation"], c["ticker"]))
    doc["top_n_reproduced_before_correction"] = reproduced
    doc.pop("content_sha256", None)
    doc["content_sha256"] = u.content_sha256(doc)
    return doc


def _check_class_row(docs: Documents | None, store: str, rel: str, needle: str) -> dict[str, Any]:
    found = bool(docs) and needle in docs.store_text(store, rel)  # type: ignore[union-attr]
    if not found:
        raise ValueError(f"{rel}: '{needle}' not found")
    return {"artifact": f"{store}/{rel}", "row_contains": needle}


def class_history_c(bars: dict[str, dict[str, tuple[float, float]]], splice: str) -> dict[str, Any]:
    """Information only: class C's own history across its two tickers (UA.C before the splice,
    UA from it), the methodology's per-ticker ranking does not join them."""
    rows = [v for d, v in bars.get("UA.C", {}).items() if d < splice]
    rows += [v for d, v in bars.get("UA", {}).items() if d >= splice]
    dv = [c * v for c, v in rows]
    return {"sessions": len(rows), "median_dollar_volume": round(statistics.median(dv), 2) if dv else 0.0}  # fmt: skip


# ---------------------------------------------------------------------- data V2
def _rows(s: dict[str, Any]) -> list[dict[str, Any]]:
    cols = s["bars"]["columns"]
    return [dict(zip(cols, r, strict=True)) for r in s["bars"]["rows"]]


def dividend_owner(d: dict[str, Any], s: dict[str, Any]) -> tuple[str, str | None]:
    """A dividend under a ticker that changed hands: before the documented handover it is the
    previous holder's; on the handover session it is not attributable (two holders that day,
    no declaration by the new holder in its filings); after it, the new holder's."""
    h = HANDOVERS.get(d["symbol"])
    if not h:
        return "SLOT_SECURITY", None
    own = s["entity_key"] == h["new_holder_entity"]
    if d["ex_date"] < h["handover_session"]:
        return ("PREVIOUS_HOLDER" if own else "SLOT_SECURITY"), h["previous_holder"]
    if d["ex_date"] == h["handover_session"]:
        return "OWNERSHIP_NOT_ESTABLISHED", None
    return ("SLOT_SECURITY" if own else "NEW_HOLDER"), h["new_holder_entity"]


def _requality(s: dict[str, Any]) -> None:
    rows = _rows(s)
    raw = {r["session"]: {"o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"],
                          "v": r["volume"]} for r in rows}  # fmt: skip
    days = sorted(raw)
    expected = _sessions(s["window"][0], s["end"])
    s["quality"] = x45d._quality(days, raw, expected)
    s["coverage"] = round(len(days) / len(expected), 6) if expected else 0.0


def terminal_value(s: dict[str, Any], docs: Documents) -> dict[str, Any]:
    t = TERMS.get(s["slot"])
    if t is None:
        return {"kind": UNKNOWN, "value_status": "UNKNOWN", "why": "not investigated"}
    for acc, quote in t["sources"]:
        if not docs.quote_ok(acc, quote):
            raise ValueError(f"{s['slot']}: quote not in {acc}: {quote!r}")
    quotes = " ".join(q for _, q in t["sources"])
    numbers = ([t["cash"]] if t.get("cash") else []) + [x[0] for x in t.get("stock", [])]
    for n in numbers:
        if n != "1" and n not in quotes:
            raise ValueError(f"{s['slot']}: {n} is not in the quoted terms")
    rows = [r for r in _rows(s) if float(r["volume"] or 0) > 0]
    last = rows[-1] if rows else None
    out: dict[str, Any] = {
        "kind": t["kind"],
        "value_status": VALUE_STATUS[t["kind"]],
        "cash_per_share": t.get("cash"),
        "stock_per_share": [{"ratio": a, "security": b} for a, b in t.get("stock", [])],
        "note": t.get("note"),
        "sources": [{"accession": a, "quote": q} for a, q in t["sources"]],
        "last_trade": last["session"] if last else None,
        "last_close": last["close"] if last else None,
    }
    if t["kind"] == CASH and last:
        out["cash_vs_last_close"] = round(float(t["cash"]) / float(last["close"]) - 1, 6)
    return out


def exit_slots(slots: list[dict[str, Any]]) -> list[str]:
    """Slots whose usable data ends before their window: an exit, a stop in trading or an
    undated successor boundary."""
    out = []
    for s in slots:
        traded = [r["session"] for r in _rows(s) if float(r["volume"] or 0) > 0]
        if not traded or traded[-1] < s["window"][1] or s["end"] < s["window"][1]:
            out.append(s["slot"])
    return sorted(out)


def apply_successor_boundary(s: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """End a slot's data at the original security's last evidenced session: later bars and
    dividends belong to the successor or to an undated changeover, never to the member."""
    keep = [r for r in s["bars"]["rows"] if r[0] <= b["data_end"]]
    dropped = len(s["bars"]["rows"]) - len(keep)
    s["bars"]["rows"] = keep
    s["end"] = b["data_end"]
    s["dropped_after_end"] += dropped
    gone = [d for d in s["dividends"] if d["ex_date"] > b["data_end"]]
    s["dividends"] = [d for d in s["dividends"] if d["ex_date"] <= b["data_end"]]
    s["excluded_dividends"] = [{**d, "reason": "AFTER_SUCCESSOR_BOUNDARY"} for d in gone]
    s["successor_boundary"] = {k: b[k] for k in ("original_security", "successor_security",
                               "original_last_evidence", "successor_first_evidence",
                               "data_end", "why")}  # fmt: skip
    s["status"] = "COMPLETE_TO_UNDATED_SUCCESSOR_BOUNDARY"
    traded = [r[0] for r in keep if r[5] and float(r[5]) > 0]
    s["last_traded"] = traded[-1] if traded else None
    _requality(s)
    return {"rule": "SUCCESSOR_BOUNDARY", "bars_removed": dropped, "dividends_removed": len(gone)}


def build_data_v2(
    data_v1: dict[str, Any],
    universe_v2: dict[str, Any],
    new_slots: list[dict[str, Any]],
    docs: Documents,
) -> dict[str, Any]:
    """The X4.5 revision for universe V2: V1 slots carried (corrected where named), slots of
    members no longer in the universe superseded, admitted members' slots added."""
    members = {f"{k}|{r['ticker']}": r for k, f in universe_v2["formations"].items()
               for r in f["records"] if r["decision"] == "MEMBER"}  # fmt: skip
    v1 = {s["slot"]: s for s in data_v1["slots"]}
    superseded = []
    out = []
    for slot, s in sorted(v1.items()):
        if slot not in members:
            r = next(x for f in universe_v2["formations"].values() for x in f["records"]
                     if f"{f['pool']}|{f['formation']}|{x['ticker']}" == slot)  # fmt: skip
            superseded.append({"slot": slot, "decision_v2": r["decision"],
                               "rule": (r.get("x461") or {}).get("rule")})  # fmt: skip
            continue
        s = json.loads(json.dumps(s))
        corrections: list[dict[str, Any]] = []
        # Identity.
        if slot in IDENTITY_CORRECTIONS:
            c = IDENTITY_CORRECTIONS[slot]
            corrections.append({"rule": "IDENTITY_CORRECTED", "from": s["security_key"],
                                "to": c["security_key"], "why": c["why"]})  # fmt: skip
            s["security_key"] = s["ledger_key"] = c["security_key"]
            s["entity_key"] = c["entity_key"]
            s["identity_confidence"] = c["identity_confidence"]
            s["flags"] = [f for f in s["flags"] if f != "IDENTITY_MEDIUM"]
        # Successor boundary.
        if slot in SUCCESSOR_BOUNDARIES:
            b = SUCCESSOR_BOUNDARIES[slot]
            for store, rel, needle in b["evidence"]:
                _check_class_row(docs, store, rel, needle)
            corrections.append(apply_successor_boundary(s, b))
        # Dividends under a ticker that changed hands.
        kept, excluded = [], []
        for d in s["dividends"]:
            owner, who = dividend_owner(d, s)
            if owner == "SLOT_SECURITY":
                kept.append(d)
            else:
                excluded.append({**d, "reason": owner, "holder": who,
                                 "handover": HANDOVERS[d["symbol"]]["handover_session"]})  # fmt: skip
        if excluded:
            for acc, q in HANDOVERS[excluded[0]["symbol"]]["evidence"]:
                if not docs.quote_ok(acc, q):
                    raise ValueError(f"handover evidence not in {acc}: {q!r}")
            s["dividends"] = kept
            s["excluded_dividends"] = [*s.get("excluded_dividends", []), *excluded]
            corrections.append({"rule": "DIVIDEND_ATTRIBUTION",
                                "excluded": [(x["ex_date"], x["rate"], x["reason"]) for x in excluded]})  # fmt: skip
        if corrections:
            s["x461_corrections"] = corrections
        out.append(s)
    for s in new_slots:
        s = json.loads(json.dumps(s))
        s["x461_corrections"] = [{"rule": "ADMITTED_BY_RECOMPUTED_TOP_N",
                                  "data": "captured into the X4.6.1 store"}]  # fmt: skip
        out.append(s)
    out.sort(key=lambda s: s["slot"])
    exits = exit_slots(out)
    for s in out:
        if s["slot"] in exits:
            s["terminal"] = terminal_value(s, docs)
    unresolved = [{"slot": f"{k}|{r['ticker']}", "decision": r["decision"]}
                  for k, f in sorted(universe_v2["formations"].items()) for r in f["records"]
                  if r["decision"] == "MEMBER_IDENTITY_UNRESOLVED"]  # fmt: skip
    doc: dict[str, Any] = {
        "kind": "x45_member_data",
        "decision": DECISION,
        "dataset_code": "HISTORICAL_MEMBER_DATA_V2",
        "method": {
            **data_v1["method"],
            "terminal": "Terminal consideration as the issuer's or exchange's filing "
            "states it, quoted and checked against the stored filing; values that depend "
            "on another security's price, an election or proration are not computed.",
        },
        "inputs": {
            "universe_v2_content_sha256": universe_v2["content_sha256"],
            "data_v1_sha256": DATA_V1_SHA256,
            "data_v1_content_sha256": data_v1["content_sha256"],
            "x461_manifest_sha256": hashlib.sha256(
                docs.x461s.manifest_path.read_bytes()
            ).hexdigest(),
            "ledger_v2_sha256": g2.LEDGER_V2_SHA256,
            "graph_v2_sha256": u.GRAPH2_SHA256,
        },
        "supersedes": {
            "dataset_code": data_v1["dataset_code"],
            "sha256": DATA_V1_SHA256,
            "superseded_slots": superseded,
        },
        "slots": out,
        "excluded_unresolved_slots": unresolved,
        "known_cases": data_v1["known_cases"],
        "handovers": HANDOVERS,
    }
    doc["summary"] = x45d.summarize(out, unresolved)
    doc["summary"]["terminal_values"] = terminal_summary(out)
    doc["content_sha256"] = x45d.content_sha256(doc)
    return doc


def terminal_summary(slots: list[dict[str, Any]]) -> dict[str, Any]:
    t = [s for s in slots if s.get("terminal")]
    spreads = [s["terminal"]["cash_vs_last_close"] for s in t
               if s["terminal"].get("cash_vs_last_close") is not None]  # fmt: skip
    return {
        "exits": len(t),
        "by_kind": dict(sorted(Counter(s["terminal"]["kind"] for s in t).items())),
        "by_value_status": dict(sorted(Counter(s["terminal"]["value_status"] for s in t).items())),
        "unresolved": sorted(
            s["slot"] for s in t if s["terminal"]["value_status"] in ("AMBIGUOUS", "UNKNOWN")
        ),
        "cash_vs_last_close": {
            "deals": len(spreads),
            "min": min(spreads) if spreads else None,
            "median": statistics.median(spreads) if spreads else None,
            "max": max(spreads) if spreads else None,
        },
    }


def write(doc: dict[str, Any], path: Path) -> str:
    if path.exists():
        raise FileExistsError(f"{path} exists: revisions are written once")
    data = x4.gz(x4.canonical(rs._plain(doc)))
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def new_member_universe(universe_v2: dict[str, Any]) -> dict[str, Any]:
    """Universe V2 restricted to the members V1 did not have (their data is captured anew)."""
    doc: dict[str, Any] = json.loads(json.dumps(universe_v2))
    for f in doc["formations"].values():
        f["records"] = [r for r in f["records"] if r["decision"] == "MEMBER"
                        and (r.get("x461") or {}).get("rule") == "ADMITTED_BY_RECOMPUTED_TOP_N"]  # fmt: skip
    return doc


# ---------------------------------------------------------------------- pipeline and validation
VALIDATION_V2_PATH = lc.OUT / "x46_validation_v2.json.gz"
STATUS_TABLE_V2 = Path("docs/phase-reports/x461-slot-status.csv")
REPORTED: dict[str, str] = {
    # X4.6.1 artifacts, as built and reported (pinned for the dependent steps).
    UNIVERSE_V2_PATH.as_posix(): "28de49ecddc09284dbd97f3e982697a03343fa5e6a0c0e4879ba855308726080",
    DATA_V2_PATH.as_posix(): "a2f7ef379a4c831bba4d6a52855dd9bbaf0cb43f17fe3e424052119ccb100159",
    "config/simulation/evidence/x461/manifest.json": "",  # set from the store at build
}
DATA_V2_CONTENT_SHA256 = "00a2fa168dce60b72779a2d499a1735eef76b90e1863c11f8272724c8ee22e74"
VALIDATION_V1 = (
    "config/simulation/lifecycle/x46_validation_v1.json.gz",
    "9cc0023f62f177a1ea59657b21e6b8b523320f720c1cf41821b5cb0a0508b165",
)


def sweep_bars_from(
    x4s: x4.Store,
) -> Callable[[str, set[str]], dict[str, dict[str, tuple[float, float]]]]:
    from app.research import x46_validation as x46

    ev = x46.Evidence({"x4": x4s})
    return lambda formation, tickers: x46.formation_bars(ev, formation, tickers)


def rebuild_universe_v2(x4s: x4.Store, x461s: x4.Store) -> dict[str, Any]:
    v1 = load_pinned(u.UNIVERSE_PATH, UNIVERSE_V1_SHA256)
    return build_universe_v2(
        v1, g2.load_ledger_v2(), u.load_graph_v2(), sweep_bars_from(x4s), Documents(x4s, x461s)
    )


def rebuild_data_v2(x4s: x4.Store, x461s: x4.Store, universe_v2: dict[str, Any]) -> dict[str, Any]:
    ledger, graph2 = g2.load_ledger_v2(), u.load_graph_v2()
    uv2_sha = REPORTED[UNIVERSE_V2_PATH.as_posix()]
    mini = new_member_universe(universe_v2)
    new = x45d.build(x461s, mini, ledger, graph2, universe_sha256=uv2_sha)  # type: ignore[arg-type]
    d1 = load_pinned(x45d.DATA_PATH, DATA_V1_SHA256)
    return build_data_v2(d1, universe_v2, new["slots"], Documents(x4s, x461s))


def pinned_v2(x461s: x4.Store) -> dict[str, str]:
    from app.research import x46_validation as x46

    pins = {**x46.PINNED, VALIDATION_V1[0]: VALIDATION_V1[1], **REPORTED}
    pins["config/simulation/evidence/x461/manifest.json"] = hashlib.sha256(
        x461s.manifest_path.read_bytes()
    ).hexdigest()
    return pins


def build_validation_v2(x461s: x4.Store) -> dict[str, Any]:
    from app.research import x43_evidence as x43e
    from app.research import x46_validation as x46

    pins = pinned_v2(x461s)
    got = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in pins}
    pre = {"artifacts": {p: {"expected": pins[p], "found": got[p]} for p in pins},
           "ok": all(got[p] == pins[p] for p in pins)}  # fmt: skip
    if not pre["ok"]:
        raise ValueError(f"pinned inputs differ: {pre}")
    dataset = load_pinned(DATA_V2_PATH, REPORTED[DATA_V2_PATH.as_posix()])
    universe = load_pinned(UNIVERSE_V2_PATH, REPORTED[UNIVERSE_V2_PATH.as_posix()])
    stores = {"x4": x4.Store(), "x43": x43e.Store(), "x45": x45d.Store(), "x461": x461s}
    edges = [{"ticker": c["ticker"], "from_security_key": c["from_security_key"],
              "security_key": c["security_key"], "entity_key": c["entity_key"]}
             for c in IDENTITY_CORRECTIONS.values()]  # fmt: skip
    return x46.build(
        dataset, universe, g2.load_ledger_v2(), stores, pre, u.load_graph_v2(),
        pinned=pins, data_content_sha256=DATA_V2_CONTENT_SHA256, edge_corrections=edges,
        code="X46_VALIDATION_V2", decision=DECISION,
    )  # fmt: skip


def verify(x4s: x4.Store, x461s: x4.Store) -> dict[str, Any]:
    """Every X4.6.1 artifact rebuilt from the stores and compared, and every pin rechecked."""
    from app.research import x46_validation as x46

    out: dict[str, Any] = {"evidence_hash_failures": []}
    for rel, e in sorted(x461s.entries.items()):
        if hashlib.sha256(x461s.path(rel).read_bytes()).hexdigest() != e["sha256"]:
            out["evidence_hash_failures"].append(rel)
    uv2 = rebuild_universe_v2(x4s, x461s)
    stored_u = x4.read_json(UNIVERSE_V2_PATH)
    dv2 = rebuild_data_v2(x4s, x461s, stored_u)
    stored_d = x4.read_json(DATA_V2_PATH)
    val = build_validation_v2(x461s)
    stored_v = x4.read_json(VALIDATION_V2_PATH)
    out.update({
        "universe_v2": [stored_u["content_sha256"], uv2["content_sha256"]],
        "data_v2": [stored_d["content_sha256"], dv2["content_sha256"]],
        "validation_v2": [stored_v["content_sha256"], x46.content_sha256(stored_v), val["content_sha256"]],
        "files": {Path(p).as_posix(): hashlib.sha256(Path(p).read_bytes()).hexdigest()
                  for p in (UNIVERSE_V2_PATH, DATA_V2_PATH, VALIDATION_V2_PATH)},
    })  # fmt: skip
    out["ok"] = (
        not out["evidence_hash_failures"]
        and len(set(out["universe_v2"])) == 1
        and len(set(out["data_v2"])) == 1
        and len(set(out["validation_v2"])) == 1
    )
    return out


__all__ = [
    "DATA_V2_PATH",
    "UNIVERSE_V2_PATH",
    "VALIDATION_V2_PATH",
    "Documents",
    "build_data_v2",
    "build_universe_v2",
    "build_validation_v2",
    "l2",
    "verify",
    "x461c",
]
