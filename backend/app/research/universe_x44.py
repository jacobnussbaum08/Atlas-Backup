"""X4.4 (D-250): historical point-in-time universe reconstruction (``HISTORICAL_UNIVERSE_V1``).

:func:`build` reconstructs the research equity and ETF universes at their formation sessions
from the preserved evidence only. It reads the X4.1, X4.3 and X4.3.2 stores, Ledger V2 and
graph V2 (verified by SHA-256), and writes nothing but its own file (:func:`write`, once).

**Rules** (:data:`RULE`): the V4 equity and V5 ETF rules' formations, windows, thresholds,
sizes and ranking, unchanged. What changes is where every input comes from:

* *Discovery pool*: every ticker the provider traded in the formation window, requested with
  ``asof`` = the formation (the X4.1 sweeps of all 1-4 letter tickers and share-class
  candidates). Today's provider asset list plays no part.
* *Eligibility and rank*: the window's traded bars only (no bar after the formation).
* *Identity*: the security that held the ticker on the formation session, from graph V2's
  listings (the nearest capture on each side, the same issuer on both); Ledger V2 and graph V2
  are the authority for lifecycle, successors and transfers. A capture after the formation is
  used only to establish who held the ticker on that day (a fact of the formation date), never
  for eligibility, rank or membership.
* *Type*: fund or stock from the identified security (SEC share class, unit trust, fund
  category) or, failing identity, from the formation-era directory's ETF flag. A ticker whose
  type cannot be established is ranked in neither pool; its shadow ranks are reported.

**Membership** is the top-N of each pool. An unresolved or conflicting identity inside the
top-N keeps its slot (the market's ranking is not changed by what this evidence lacks) but is
classified ``MEMBER_IDENTITY_UNRESOLVED``, never a confirmed member. Lifecycle events inside a
member's window (ends, conversions, transfers, ticker changes) are recorded; they end or
continue membership, never decide it.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from app.backtesting.data import sessions_between
from app.research import exclusions, exclusions_x2
from app.research import graph_v2 as g2
from app.research import ledger_v2 as l2
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import x4_evidence as x4
from app.research import x43_evidence as x43
from app.research import x432_evidence as x432
from app.research.universe import EXPLORATION_SYMBOLS, eligible_symbol

OUT = lc.OUT
UNIVERSE_PATH = OUT / "x44_historical_universe_v1.json.gz"
GRAPH2_SHA256 = "86ae132cc94f16c1352405d80e4d7021b362cce9e165cabce47933c33630aaf3"
CONFIRMED, UNRESOLVED, CONFLICTING = rs.CONFIRMED, rs.UNRESOLVED, rs.CONFLICTING
UNTIL = date(2019, 12, 31)
EQUITY_FORMATIONS = (date(2016, 12, 30), date(2017, 12, 29), date(2018, 12, 31))
ETF_FORMATIONS = (date(2016, 12, 30),)
HISTORY_WEEK = (date(2016, 1, 4), date(2016, 1, 8))
STOCK_CATS = rs.GENERIC | {"LP_UNIT", "BENEFICIAL_INTEREST", "OTHER"}
NON_COMMON = {"PREFERRED", "WARRANT", "UNIT", "RIGHT", "DEBT"}

RULE: dict[str, Any] = {
    "code": "HISTORICAL_UNIVERSE_V1",
    "equity": {
        "formations": [f.isoformat() for f in EQUITY_FORMATIONS],
        "until": UNTIL.isoformat(),
        "window_sessions": x4.WINDOW_SESSIONS,
        "bars": "traded sessions only (volume > 0)",
        "min_bars": 50,
        "min_close": "5",
        "min_median_dollar_volume": "1000000",
        "members": 500,
        "rank": "median daily dollar volume (close x volume), descending; ties by symbol",
        "excluded": "funds and exchange-traded notes; non-common classes (preferred, warrants, "
        "units, rights, debt); symbols other than plain or .A/.B/.C; the 21 exploration "
        "symbols and SPY",
        "effective": "from the session after the formation to the next formation, inclusive "
        "(the last to 'until')",
        "as_in": "RESEARCH_US_EQUITY_V4 (thresholds, sizes, ranking, formations)",
    },
    "etf": {
        "formations": [f.isoformat() for f in ETF_FORMATIONS],
        "until": UNTIL.isoformat(),
        "window_sessions": x4.WINDOW_SESSIONS,
        "bars": "traded sessions only (volume > 0)",
        "min_bars": 50,
        "min_close": "5",
        "min_median_dollar_volume": "1000000",
        "members": 150,
        "rank": "median daily dollar volume (close x volume), descending; ties by symbol",
        "history": "a traded bar in the provider's first week of history (2016-01-04..08)",
        "excluded": "the 21 exploration symbols; non-fund securities",
        "effective": "2017-01-03 to 2019-12-31",
        "as_in": "RESEARCH_ETF_CANDIDATES_V5",
    },
    "identity": "graph V2 listings around the formation session (nearest capture each side, "
    "same issuer); Ledger V2 lifecycle; graph V2 successors and joins",
    "discovery": "X4.1 provider sweeps as of each formation (all 1-4 letter tickers and share-"
    "class candidates); never today's provider list",
    "look_ahead": "eligibility, rank and membership use only bars up to the formation; a later "
    "capture only brackets who held the ticker on the formation session; lifecycle after the "
    "formation only ends or continues membership",
    "uncertainty": "unresolved or conflicting identities keep their rank slot but are "
    "MEMBER_IDENTITY_UNRESOLVED; tickers with no established type are ranked in neither pool",
}


# ---------------------------------------------------------------------- inputs
def load_graph_v2(path: Path = g2.GRAPH2_PATH, expect: str = GRAPH2_SHA256) -> dict[str, Any]:
    got = l2._sha_file(path)
    if got != expect:
        raise ValueError(f"{path}: sha256 {got} is not the graph V2 {expect}")
    doc: dict[str, Any] = x4.read_json(path)
    if rs.content_sha256(doc) != doc["content_sha256"]:
        raise ValueError(f"{path}: the graph does not hash to its content_sha256")
    return doc


def history_week(x4s: x4.Store, formation: date) -> set[str]:
    """Tickers with a traded bar in the provider's first week (the X4.1 history sweep)."""
    out: set[str] = set()
    for rel, doc in x4._iter_sweep(x4s, formation):
        if rel.split("/")[3] != "history":
            continue
        for req in doc["requests"]:
            for pg in req["pages"]:
                for sym, bars in ((pg.get("body") or {}).get("bars") or {}).items():
                    if any(float(b.get("v") or 0) > 0 for b in bars):
                        out.add(sym)
    return out


def sweep_artifacts(x4s: x4.Store, formation: date) -> list[dict[str, str]]:
    return [
        {"artifact": x4s.root.as_posix() + "/" + rel, "sha256": x4s.entries[rel]["sha256"]}
        for rel in sorted(k for k in x4s.entries if k.startswith(f"provider/sweeps/{formation}/"))
    ]


# ---------------------------------------------------------------------- identity at a date
def rows_at(ix: rs.Index, ticker: str, when: date) -> list[rs.Obs]:
    """The listing rows of the nearest capture day on each side of ``when``."""
    rows = ix.by_ticker.get(ticker, [])
    days = ix.days.get(ticker, [])
    from bisect import bisect_left, bisect_right

    i = bisect_right(days, when)
    j = bisect_left(days, when)
    before = [o for o in rows[:i] if o.day == rows[i - 1].day] if i else []
    after = [o for o in rows[j:] if o.day == rows[j].day] if j < len(rows) else []
    return before + after


FUND_FORMS = ("485BPOS", "485APOS", "24F-2NT", "N-1A", "N-1A/A")


def fund_filers(x4s: x4.Store) -> set[str]:
    """CIKs that filed open-end fund or unit-trust forms (485BPOS, 485APOS, 24F-2NT, N-1A) in
    the captured 2015-2020 EDGAR indexes: registered investment companies of the kind the
    V4/V5 rules call registered funds (closed-end funds file none of these)."""
    import gzip
    import re

    if "fund_filers" in x4s.cache:
        cached: set[str] = x4s.cache["fund_filers"]
        return cached
    line = re.compile(r"(\S.*?)\s{2,}(.+?)\s{2,}(\d+)\s+(\d{4}-\d\d-\d\d)")
    out: set[str] = set()
    for rel in sorted(k for k in x4s.entries if k.startswith("sec/full-index/")):
        for ln in gzip.decompress(x4s.path(rel).read_bytes()).decode("latin-1").splitlines():
            m = line.match(ln)
            if m and m.group(1).strip() in FUND_FORMS:
                out.add(f"{int(m.group(3)):010d}")
    x4s.cache["fund_filers"] = out
    return out


def type_of(
    security: str | None,
    rows: list[rs.Obs],
    entity: str | None,
    funds: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, str]:
    """(STOCK | FUND | ETN | NON_COMMON | TYPE_UNRESOLVED, basis), by the V4/V5 definition
    applied at the formation: a registered fund (an SEC share class, or an issuer filing
    open-end fund or unit-trust forms) or a listed name with 'ETF' is a fund; 'ETN' in the
    listed name is a note; a preferred, warrant, unit, right or debt class is not common
    equity; any other listed security is a stock."""
    mine = [o for o in rows if entity and o.entity == entity] or rows
    names = " ".join(o.name for o in mine).upper()
    cats = {o.cat for o in mine if o.status == CONFIRMED} or {o.cat for o in mine}
    if security and (security.startswith("SECCLASS:") or security.endswith("|TRUST_UNITS")):
        return "FUND", "registered fund: SEC share class or unit trust"
    if entity and entity[4:] in funds and not cats & NON_COMMON:
        return "FUND", "registered fund: the issuer files open-end fund or unit-trust forms"
    if " ETF" in f" {names}":
        return "FUND", "'ETF' in the listed name at the formation"
    if " ETN" in f" {names}":
        return "ETN", "'ETN' in the listed name at the formation"
    if cats and cats <= NON_COMMON:
        return "NON_COMMON", "preferred, warrant, unit, right or debt class"
    if mine and entity:
        return "STOCK", "listed at the formation; not a registered fund, no ETF/ETN in the name"
    if mine:
        return "STOCK_PROVISIONAL", "identity unresolved: no ETF/ETN in the listed name"
    return "TYPE_UNRESOLVED", "no listing of the ticker near the formation"


# ---------------------------------------------------------------------- lifecycle in a window
def windows(formations: tuple[date, ...]) -> dict[date, tuple[date, date]]:
    out = {}
    for i, f in enumerate(formations):
        nxt = formations[i + 1] if i + 1 < len(formations) else UNTIL
        sess = sessions_between(f, nxt)
        out[f] = (sess[1].day if len(sess) > 1 else nxt, nxt)
    return out


class Lifecycle:
    def __init__(self, v2: dict[str, Any], graph2: dict[str, Any]) -> None:
        self.by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in v2["events"]:
            self.by_sec[e["security_key"]].append(e)
        self.plain_by_entity: dict[str, set[str]] = defaultdict(set)
        for s in v2["securities"]:
            if s["entity_key"] and s["class_label"] in rs.GENERIC:
                self.plain_by_entity[s["entity_key"]].add(s["security_key"])
        self.successors = [s for s in graph2["successors"]]

    def ends_any(self, key: str | None) -> str | None:
        """The earliest confirmed terminal event of a ledger security, at any date."""
        ends = sorted(
            (e["filing_date"] or e["not_before"] or "", e["code"])
            for e in self.by_sec.get(key or "", [])
            if e["event_type"] in lc.TERMINAL and e["resolution_status"] == CONFIRMED
        )
        return ends[0][1] if ends else None

    def resolve_key(self, security: str | None, entity: str | None) -> str | None:
        if security and security.endswith("|UNSPECIFIED") and entity:
            plain = self.plain_by_entity.get(entity, set())
            return next(iter(plain)) if len(plain) == 1 else security
        return security

    def in_window(
        self, security: str | None, entity: str | None, lo: date, hi: date
    ) -> dict[str, Any]:
        key = self.resolve_key(security, entity)
        out: dict[str, Any] = {"ledger_key": key, "ends": [], "continuity": [], "successor": None}
        if not key:
            return out
        for e in self.by_sec.get(key, []):
            when = e["filing_date"] or e["not_before"] or e["effective_date"]
            nb, na = e["not_before"], e["not_after"]
            if e["event_type"] in lc.TERMINAL and e["resolution_status"] == CONFIRMED:
                inside = (when and lo.isoformat() <= when <= hi.isoformat()) or (
                    nb and na and nb <= hi.isoformat() and na >= lo.isoformat()
                )
                before = bool(na and na < lo.isoformat()) or bool(
                    e["filing_date"] and e["filing_date"] < lo.isoformat() and not nb
                )
                if inside or before:
                    out["ends"].append(
                        {
                            "code": e["code"],
                            "filing_date": e["filing_date"],
                            "not_before": nb,
                            "not_after": na,
                            "precision": e["effective_precision"],
                            "before_window": before and not inside,
                        }
                    )
            elif (
                e["event_type"] in ("EXCHANGE_TRANSFER", "TICKER_CHANGE")
                and nb
                and na
                and (nb <= hi.isoformat() and na >= lo.isoformat())
            ):
                out["continuity"].append(e["code"])
        for s in self.successors:
            if s["predecessor"] == key and lo.isoformat() <= s["filed"] <= hi.isoformat():
                out["successor"] = {
                    "filed": s["filed"],
                    "successor": s["successor_security"],
                    "status": s["status"],
                    "rule": s["rule"],
                }
        return out


# ---------------------------------------------------------------------- the build
def candidates(
    x4s: x4.Store,
    ix: rs.Index,
    life: Lifecycle,
    formation: date,
    window: tuple[date, date],
    pool_rule: dict[str, Any],
    pool: str,
    hist: set[str],
) -> list[dict[str, Any]]:
    """One decision per ticker the provider traded in the window."""
    metrics = x4.formation_metrics(x4s, formation)
    excluded = set(EXPLORATION_SYMBOLS) | ({"SPY"} if pool == "STOCK" else set())
    out = []
    for t, m in sorted(metrics.items()):
        if not m["traded_bars"]:
            continue
        h = rs.holder(ix, t, formation)
        rows = rows_at(ix, t, formation)
        typ, basis = type_of(h["security"], rows, h["entity"], fund_filers(x4s))
        rec: dict[str, Any] = {
            "ticker": t,
            "traded_bars": m["traded_bars"],
            "last_close": m["last_close"],
            "median_dollar_volume": round(m["mdv"], 2),
            "first_bar": m["first"],
            "last_bar": m["last"],
            "identity_status": h["status"],
            "identity_confidence": h["confidence"],
            "entity_key": h["entity"],
            "security_key": h["security"],
            "identity_explanation": h["explanation"],
            "venue": next((o.venue for o in rows if o.entity == h["entity"]), None)
            if h["entity"]
            else None,
            "type": typ,
            "type_basis": basis,
            "rank": None,
            "decision": None,
            "reasons": [],
            "listed_names": sorted({o.name for o in rows}),
            "flags": [],
            "lifecycle": None,
            "evidence": h["evidence"],
        }
        reasons = []
        if m["last"] and m["last"] > formation.isoformat():
            reasons.append("BARS_AFTER_FORMATION")  # never used: no look-ahead
        if not eligible_symbol(t):
            reasons.append("SYMBOL_RULE")
        if t in excluded:
            reasons.append("RULE_EXCLUSION")
        if m["traded_bars"] < pool_rule["min_bars"]:
            reasons.append("FEW_TRADED_BARS")
        if (m["last_close"] or 0) < float(pool_rule["min_close"]):
            reasons.append("LOW_CLOSE")
        if m["mdv"] < float(pool_rule["min_median_dollar_volume"]):
            reasons.append("LOW_DOLLAR_VOLUME")
        if pool == "FUND" and t not in hist:
            reasons.append("NO_FIRST_WEEK_BAR")
        rec["reasons"] = reasons
        out.append(rec)
    return out


def provisional_types(
    by_run: dict[tuple[str, date], list[dict[str, Any]]], funds: dict[int, list[dict[str, str]]]
) -> None:
    """A provisional type for unresolved identities, from evidence only: the ticker in that
    year's SEC series/class file (a registered fund class), or the same listed name
    identified at another formation; otherwise a stock (no fund evidence, no ETF/ETN)."""
    from app.research.lifecycle import alnum

    known: dict[tuple[str, str], str] = {}
    for (_pool, f), recs in by_run.items():
        for r in recs:
            if r["identity_status"] == CONFIRMED and r["type"] in ("STOCK", "FUND", "ETN"):
                for n in r["listed_names"]:
                    known[(r["ticker"], alnum(n))] = f"{r['type']}@{f}"
    fund_tickers = {(y, x["ticker"]) for y, rows in funds.items() for x in rows if x["ticker"]}
    for (_pool, f), recs in by_run.items():
        for r in recs:
            if r["type"] != "STOCK_PROVISIONAL":
                continue
            if (f.year, r["ticker"]) in fund_tickers:
                r["type"], r["type_basis"] = (
                    "FUND",
                    (
                        f"provisional: the ticker is a share class in the SEC's {f.year} "
                        "series/class file (identity unresolved)"
                    ),
                )
                continue
            other = sorted(
                {
                    known[(r["ticker"], alnum(n))]
                    for n in r["listed_names"]
                    if (r["ticker"], alnum(n)) in known
                }
            )
            if other:
                r["type"] = other[0].split("@")[0]
                r["type_basis"] = (
                    f"provisional: the same listed name is identified as {other[0]} "
                    "(identity unresolved here)"
                )
            else:
                r["type"] = "STOCK"
                r["type_basis"] = (
                    "provisional: identity unresolved; no fund evidence and no ETF/ETN in the "
                    "listed name"
                )


def rank_and_decide(recs: list[dict[str, Any]], pool: str, size: int) -> dict[str, Any]:
    pool_recs = [r for r in recs if not r["reasons"] and r["type"] == pool]
    ranked = sorted(pool_recs, key=lambda r: (-r["median_dollar_volume"], r["ticker"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    shadow = sorted(
        (r for r in recs if not r["reasons"] and r["type"] == "TYPE_UNRESOLVED"),
        key=lambda r: (-r["median_dollar_volume"], r["ticker"]),
    )
    cut = ranked[size - 1]["median_dollar_volume"] if len(ranked) >= size else 0.0
    for r in recs:
        if r["reasons"]:
            r["decision"] = "NOT_ELIGIBLE"
        elif r["type"] == "TYPE_UNRESOLVED":
            r["decision"] = "EXCLUDED_TYPE_UNRESOLVED"
            r["shadow_in_top"] = r["median_dollar_volume"] >= cut
        elif r["type"] != pool:
            r["decision"] = f"OTHER_POOL_{r['type']}"
        elif r["rank"] and r["rank"] <= size:
            r["decision"] = (
                "MEMBER" if r["identity_status"] == CONFIRMED else "MEMBER_IDENTITY_UNRESOLVED"
            )
        else:
            r["decision"] = "BELOW_CUTOFF"
    return {
        "ranked": len(ranked),
        "cutoff_median_dollar_volume": cut,
        "type_unresolved_in_top": sum(1 for r in shadow if r.get("shadow_in_top")),
    }


def flag_members(
    recs: list[dict[str, Any]], life: Lifecycle, window: tuple[date, date], formation: date
) -> None:
    lo, hi = window
    x1 = [(t, d) for t, d, _ in exclusions.EXCLUDED]
    x2 = [(e.label, e.session) for e in exclusions_x2.EVENTS]
    for r in recs:
        if not r["decision"] or not r["decision"].startswith("MEMBER"):
            continue
        lf = life.in_window(r["security_key"], r["entity_key"], lo, hi)
        r["lifecycle"] = lf
        # For reporting only (never membership): does Ledger V2 end this security at all?
        r["ledger_end_any_date"] = life.ends_any(lf["ledger_key"])
        if any(e["before_window"] for e in lf["ends"]):
            r["flags"].append("LEDGER_END_BEFORE_FORMATION")
        if any(not e["before_window"] for e in lf["ends"]):
            r["flags"].append("ENDS_IN_WINDOW")
        if lf["successor"]:
            r["flags"].append("CONVERTS_IN_WINDOW")
        if lf["continuity"]:
            r["flags"].append("TRANSFER_OR_TICKER_CHANGE_IN_WINDOW")
        for t, d in x1:
            if t == r["ticker"] and lo <= d <= hi:
                r["flags"].append(f"X1_EXCLUSION_IN_WINDOW:{d}")
        for t, d in x2:
            if t == r["ticker"] and lo <= d <= hi:
                r["flags"].append(f"X2_EXCLUSION_IN_WINDOW:{d}")
        if r["identity_confidence"] == "MEDIUM":
            r["flags"].append("IDENTITY_MEDIUM")
        if r["decision"] == "MEMBER" and "LEDGER_END_BEFORE_FORMATION" in r["flags"]:
            r["decision"] = "MEMBER_ELIGIBILITY_UNCERTAIN"
        _ = formation


def priority_notices(
    graph2: dict[str, Any],
    formations: dict[str, dict[date, list[dict[str, Any]]]],
    wins: dict[str, dict[date, tuple[date, date]]],
) -> list[dict[str, Any]]:
    """REDACTED_FOR_GITHUB"""
    out = []
    for n in graph2["notice_triage"]["priority_for_x44"]:
        sec = n["security_key"]
        ent = sec.split("|", 1)[0] if sec.startswith("CIK:") else None
        filed = date.fromisoformat(n["filed"])
        hits = []
        for pool, by_f in formations.items():
            for f, recs in by_f.items():
                lo, hi = wins[pool][f]
                for r in recs:
                    same = (
                        (r["security_key"] == sec)
                        if sec.startswith("SECCLASS:")
                        else (ent is not None and r["entity_key"] == ent)
                    )
                    if not same:
                        continue
                    compatible = _same_class(sec, r) and (
                        n["venue"] is None or r["venue"] is None or r["venue"] == n["venue"]
                    )
                    when = (
                        "BEFORE_FORMATION"
                        if filed <= f
                        else "IN_WINDOW"
                        if lo <= filed <= hi
                        else "AFTER_WINDOW"
                    )
                    hits.append(
                        {
                            "pool": pool,
                            "formation": f.isoformat(),
                            "ticker": r["ticker"],
                            "decision": r["decision"],
                            "compatible": compatible,
                            "timing": when,
                        }
                    )
        members_in = [
            h
            for h in hits
            if h["decision"].startswith("MEMBER") and h["timing"] == "IN_WINDOW" and h["compatible"]
        ]
        before = [
            h
            for h in hits
            if h["decision"].startswith("MEMBER")
            and h["timing"] == "BEFORE_FORMATION"
            and h["compatible"]
            and (date.fromisoformat(h["formation"]) - filed).days <= 365
        ]
        tickers_in = {(h["pool"], h["formation"], h["ticker"]) for h in members_in}
        if not hits:
            impact = "NO_CANDIDATE"
        elif len(tickers_in) == 1:
            impact = "MEMBER_END_EVIDENCE"
        elif len(tickers_in) > 1:
            impact = "MEMBER_END_AMBIGUOUS"
        elif before:
            impact = "MEMBER_CLASS_REMOVED_BEFORE_FORMATION"
        elif any(h["decision"].startswith("MEMBER") for h in hits):
            impact = "MEMBER_OTHER_CLASS_OR_TIMING"
        else:
            impact = "CANDIDATE_NOT_MEMBER"
        out.append(
            {
                **{
                    k: n[k]
                    for k in ("accession", "security_key", "venue", "filed", "category", "reason")
                },
                "impact": impact,
                "members_in_window": sorted(tickers_in),
                "hits": hits[:12],
            }
        )
    return out


def _same_class(notice_key: str, r: dict[str, Any]) -> bool:
    """A notice bears on a member only through the member's own class: the same ledger key,
    or the member's class is unspecified (a company-list row) and the notice's class is a
    plain equity class of the same issuer. Another class of the issuer (a tracking stock, a
    preferred, a note) never ends the member."""
    mine = r.get("security_key") or ""
    if notice_key == mine:
        return True
    if not notice_key.startswith("CIK:") or not mine.startswith("CIK:"):
        return False
    n_ent, n_label = notice_key.split("|", 1)
    m_ent, m_label = mine.split("|", 1)
    return (
        n_ent == m_ent
        and m_label == "UNSPECIFIED"
        and (n_label in rs.GENERIC or n_label.startswith("LP_UNIT"))
    )


def apply_notice_impacts(
    formations: dict[str, dict[date, list[dict[str, Any]]]], impacts: list[dict[str, Any]]
) -> None:
    by = defaultdict(list)
    for n in impacts:
        for pool, f, t in n["members_in_window"]:
            by[(pool, f, t)].append(n)
        if n["impact"] == "MEMBER_CLASS_REMOVED_BEFORE_FORMATION":
            for h in n["hits"]:
                if h["timing"] == "BEFORE_FORMATION" and h["compatible"]:
                    by[(h["pool"], h["formation"], h["ticker"])].append(n)
    for pool, by_f in formations.items():
        for f, recs in by_f.items():
            for r in recs:
                ns = by.get((pool, f.isoformat(), r["ticker"]), [])
                if not ns:
                    continue
                r["priority_notices"] = [
                    {"accession": n["accession"], "filed": n["filed"], "impact": n["impact"]}
                    for n in ns
                ]
                if any(n["impact"] == "MEMBER_END_EVIDENCE" for n in ns):
                    r["flags"].append("PRIORITY_NOTICE_END_IN_WINDOW")
                if any(n["impact"] == "MEMBER_END_AMBIGUOUS" for n in ns):
                    r["flags"].append("PRIORITY_NOTICE_END_AMBIGUOUS")
                if any(n["impact"] == "MEMBER_CLASS_REMOVED_BEFORE_FORMATION" for n in ns) and (
                    r["decision"] == "MEMBER"
                ):
                    r["decision"] = "MEMBER_ELIGIBILITY_UNCERTAIN"
                    r["flags"].append("PRIORITY_NOTICE_BEFORE_FORMATION")


def coverage(x4s: x4.Store, formation: date) -> dict[str, Any]:
    caps: dict[str, list[str]] = defaultdict(list)
    for rel, e in x4s.entries.items():
        if rel.startswith("archive/") and rel.endswith(".raw") and e.get("digest_verified"):
            caps[rel.split("/")[1]].append(lc._day(e["source"]["timestamp"]).isoformat())
    out = {}
    for fam, days in sorted(caps.items()):
        ds = sorted(days)
        before = [d for d in ds if d <= formation.isoformat()]
        after = [d for d in ds if d > formation.isoformat()]
        out[fam] = {"before": before[-1] if before else None, "after": after[0] if after else None}
    nyse = out.get("symdir_otherlisted", {})
    limited = []
    if not nyse.get("before"):
        limited.append(
            "NYSE-family symbol directory: no capture before the formation (company "
            "lists only; identity at most MEDIUM on that side)"
        )
    if not any(v["after"] for k, v in out.items() if k.startswith("symdir")):
        limited.append("no symbol-directory capture after the formation")
    return {
        "captures": out,
        "limitations": limited,
        "sweeps": _sweep_summary(x4s, formation),
    }


def _sweep_summary(x4s: x4.Store, formation: date) -> dict[str, dict[str, int]]:
    """Requests, requested tickers and failed requests per sweep source."""
    by: dict[str, Counter[str]] = defaultdict(Counter)
    for rel, doc in x4._iter_sweep(x4s, formation):
        src = rel.split("/")[3]
        for req in doc["requests"]:
            by[src]["requests"] += 1
            by[src]["tickers"] += len(req.get("symbols") or [])
            by[src]["failed_requests"] += 0 if req.get("ok", True) else 1
    return {k: dict(v) for k, v in sorted(by.items())}


def _why_new(t: str, today: set[str], by_t: dict[str, dict[str, Any]]) -> str:
    """Why V4/V5 missed a member: absent from the provider's 2026 list with a recorded end
    (survivorship), absent without one (a gap in the provider's list), or present today (a
    rank, type or identity difference)."""
    if t in today:
        return "present_today:rank_type_or_identity_difference"
    if by_t.get(t, {}).get("ledger_end_any_date"):
        return "absent_today:ended_per_ledger_v2"
    return "absent_today:no_end_in_evidence"


def comparison(
    x4s: x4.Store, formations: dict[str, dict[date, list[dict[str, Any]]]], graph2: dict[str, Any]
) -> dict[str, Any]:
    """Against the V4/V5 baselines (the X4.1 read-only snapshot); nothing is changed."""
    if not x4s.has("db/research_snapshot.json.gz"):
        return {"available": False, "reason": "no V4/V5 snapshot in the evidence store"}
    snap = x4._snapshot(x4s)
    today: set[str] = set()
    for status in ("active", "inactive"):
        rel = f"provider/assets/assets_{status}.json.gz"
        if x4s.has(rel):
            today |= {a.get("symbol") for a in (x4.read_json(x4s.path(rel)).get("body") or [])}
    # A baseline member is matched by its identity's ticker AT the formation (graph V2's
    # price fingerprint of the research identity), not by its label (V4 labels renamed
    # companies by today's ticker).
    at_f: dict[tuple[str, str], list[str]] = {}
    for p in graph2["provider_links"]:
        for fm in p["formations"]:
            at_f[(p["asset_id"], fm["formation"])] = fm["tickers"]
    base: dict[tuple[str, str], set[str]] = defaultdict(set)
    relabelled: Counter[str] = Counter()
    for code, f, sym, aid, _lc, _mdv, _elig, member in snap["candidates"]:
        if member:
            pool = "STOCK" if "EQUITY" in code else "FUND"
            ts = at_f.get((aid, f)) if aid else None
            label = sym.split("~")[0].rstrip("#")
            if ts and len(ts) == 1:
                if ts[0] != label:
                    relabelled[f"{pool}|{f}"] += 1
                base[(pool, f)].add(ts[0])
            else:
                base[(pool, f)].add(label)
    out = {}
    for pool, by_f in formations.items():
        for f, recs in by_f.items():
            mine = {
                r["ticker"] for r in recs if r["decision"] and r["decision"].startswith("MEMBER")
            }
            conf = {r["ticker"] for r in recs if r["decision"] == "MEMBER"}
            b = base.get((pool, f.isoformat()), set())
            by_t = {r["ticker"]: r for r in recs}
            only_new = sorted(mine - b)
            only_old = sorted(b - mine)
            out[f"{pool}|{f}"] = {
                "x44_members": len(mine),
                "x44_confirmed": len(conf),
                "baseline_members": len(b),
                "baseline_matched_by_formation_ticker": relabelled.get(f"{pool}|{f}", 0),
                "both": len(mine & b),
                "x44_only": len(only_new),
                "baseline_only": len(only_old),
                "x44_only_absent_from_todays_provider_list": sorted(
                    t for t in only_new if t not in today
                ),
                "x44_only_reasons": dict(Counter(_why_new(t, today, by_t) for t in only_new)),
                "x44_only_by_reason": {
                    why: sorted(t for t in only_new if _why_new(t, today, by_t) == why)
                    for why in sorted({_why_new(t, today, by_t) for t in only_new})
                },
                "baseline_only_reasons": dict(
                    Counter(
                        (by_t[t]["decision"] if t in by_t else "NO_TRADED_BARS_AS_OF_FORMATION")
                        for t in only_old
                    )
                ),
                "x44_only_sample": only_new[:60],
                "baseline_only_sample": only_old[:60],
            }
    return out


def build(
    x4s: x4.Store,
    x43s: x43.Store,
    x432s: x432.Store,
    v2: dict[str, Any],
    graph2: dict[str, Any],
    v2_sha256: str = g2.LEDGER_V2_SHA256,
    graph2_sha256: str = GRAPH2_SHA256,
    check_graph: bool = True,
) -> dict[str, Any]:
    obs, _rev, _succ, _fil, _act = g2.observations_v2(x4s, x432s, v2)
    edges, _ = rs.ticker_edges(obs)
    if check_graph:
        mine = {(e["security_key"], e["ticker"], e["venue"]) for e in edges}
        theirs = {(e["security_key"], e["ticker"], e["venue"]) for e in graph2["ticker_edges"]}
        if mine != theirs:
            raise ValueError("the reconstructed listings differ from graph V2's ticker edges")
    ix = rs.Index.of(obs)
    life = Lifecycle(v2, graph2)
    hist = history_week(x4s, ETF_FORMATIONS[0])
    plan = {"STOCK": (EQUITY_FORMATIONS, RULE["equity"]), "FUND": (ETF_FORMATIONS, RULE["etf"])}
    formations: dict[str, dict[date, list[dict[str, Any]]]] = {}
    wins: dict[str, dict[date, tuple[date, date]]] = {}
    summary: dict[str, Any] = {}
    by_run: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for pool, (fs, prule) in plan.items():
        wins[pool] = windows(fs) if pool == "STOCK" else {fs[0]: (date(2017, 1, 3), UNTIL)}
        formations[pool] = {}
        for f in fs:
            by_run[(pool, f)] = candidates(x4s, ix, life, f, wins[pool][f], prule, pool, hist)
    provisional_types(by_run, lc.fund_classes(x4s))
    for pool, (fs, prule) in plan.items():
        for f in fs:
            recs = by_run[(pool, f)]
            stats = rank_and_decide(recs, pool, prule["members"])
            flag_members(recs, life, wins[pool][f], f)
            formations[pool][f] = recs
            summary[f"{pool}|{f}"] = stats
    impacts = priority_notices(graph2, formations, wins)
    apply_notice_impacts(formations, impacts)
    out_f = {}
    for pool, by_f in formations.items():
        for f, recs in by_f.items():
            key = f"{pool}|{f}"
            decisions = Counter(r["decision"] for r in recs)
            members = [r for r in recs if r["decision"] and r["decision"].startswith("MEMBER")]
            out_f[key] = {
                "pool": pool,
                "formation": f.isoformat(),
                "window": [d.isoformat() for d in wins[pool][f]],
                "rule": RULE["equity" if pool == "STOCK" else "etf"],
                "sweep_artifacts": sweep_artifacts(x4s, f),
                "coverage": coverage(x4s, f),
                "counts": {
                    "traded_tickers": len(recs),
                    **summary[key],
                    "decisions": dict(sorted(decisions.items())),
                    "members_by_identity_confidence": dict(
                        sorted(
                            Counter(
                                f"{r['decision']}|{r['identity_confidence']}" for r in members
                            ).items()
                        )
                    ),
                    "member_flags": dict(
                        sorted(
                            Counter(fl.split(":")[0] for r in members for fl in r["flags"]).items()
                        )
                    ),
                },
                "records": recs,
            }
    doc: dict[str, Any] = {
        "kind": "x44_historical_universe",
        "decision": "D-250",
        "universe_code": RULE["code"],
        "rule": RULE,
        "inputs": {
            "x41_manifest_sha256": x4.sha256(x4s.manifest_path.read_bytes()),
            "x43_manifest_sha256": x4.sha256(x43s.manifest_path.read_bytes())
            if x43s.manifest_path.exists()
            else None,
            "x432_manifest_sha256": x4.sha256(x432s.manifest_path.read_bytes())
            if x432s.manifest_path.exists()
            else None,
            "ledger_v2_sha256": v2_sha256,
            "graph_v2_sha256": graph2_sha256,
            "graph_v2_content_sha256": graph2["content_sha256"],
        },
        "formations": out_f,
        "priority_notices": impacts,
        "priority_notice_impacts": dict(sorted(Counter(n["impact"] for n in impacts).items())),
        "comparison_with_v4_v5": comparison(x4s, formations, graph2),
        "history_reconciliation": reconcile_histories(graph2, v2),
    }
    doc["content_sha256"] = content_sha256(doc)
    return doc


def reconcile_histories(graph2: dict[str, Any], v2: dict[str, Any]) -> dict[str, Any]:
    """REDACTED_FOR_GITHUB"""
    ver = {i["asset_id"]: i["verdict"] for i in v2["provider_histories"]["identities"]}
    guard = {g["asset_id"] for g in graph2["successor_registrations_in_histories"]}
    status: dict[str, str] = {}
    for p in graph2["provider_links"]:
        a = p["asset_id"]
        if p["status"] == UNRESOLVED:
            status[a] = "UNRESOLVED"
        elif p["status"] == CONFLICTING:
            status[a] = "CONFLICTING_SPLIT"
        elif ver.get(a, "").startswith("SPLIT"):
            status[a] = "IDENTIFIED_SEGMENT_LIMITED"
        elif a in guard:
            status[a] = "IDENTIFIED_FROM_SUCCESSION"
        else:
            status[a] = "IDENTIFIED"
    labels = {p["asset_id"]: p["request"]["label"] for p in graph2["provider_links"]}
    return {
        "total": len(status),
        "by_status": dict(sorted(Counter(status.values()).items())),
        "lists": {
            s: sorted(labels[a] for a, x in status.items() if x == s)
            for s in sorted(set(status.values()))
            if s != "IDENTIFIED"
        },
        "notes": [
            "graph V2 CONFIRMED 741 = IDENTIFIED + IDENTIFIED_SEGMENT_LIMITED + "
            "REDACTED_FOR_GITHUB"
            "REDACTED_FOR_GITHUB",
            "REDACTED_FOR_GITHUB"
            "attributed to its last listing, the rest unattributed)",
            "REDACTED_FOR_GITHUB"
            "REDACTED_FOR_GITHUB",
        ],
    }


def content_sha256(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "content_sha256"}
    return hashlib.sha256(x4.canonical(rs._plain(body))).hexdigest()


def write(doc: dict[str, Any], path: Path = UNIVERSE_PATH) -> str:
    if path.exists():
        raise FileExistsError(f"{path} exists: the historical universe is written once")
    data = x4.gz(x4.canonical(rs._plain(doc)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def verify(path: Path, x4s: x4.Store, x43s: x43.Store, x432s: x432.Store) -> dict[str, Any]:
    stored = x4.read_json(path)
    rebuilt = build(x4s, x43s, x432s, g2.load_ledger_v2(), load_graph_v2())
    return {
        "file_sha256": l2._sha_file(path),
        "stored_content_sha256": stored["content_sha256"],
        "recomputed_content_sha256": content_sha256(stored),
        "rebuilt_content_sha256": rebuilt["content_sha256"],
        "ok": stored["content_sha256"] == content_sha256(stored) == rebuilt["content_sha256"],
    }


__all__ = ["RULE", "UNIVERSE_PATH", "build", "load_graph_v2", "type_of", "verify", "write"]
