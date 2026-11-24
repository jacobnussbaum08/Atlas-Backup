"""X4.3.2 (D-249): the identity graph V2, a revision of the X4.3 graph on Ledger V2.

:func:`build` reads the X4.1, X4.3 and X4.3.2 evidence stores, the X4.3 graph V1 and the
Ledger V2 (each graph and ledger verified by its SHA-256), and never writes to any of them. It
returns a deterministic document that :func:`write` stores once
(``config/simulation/lifecycle/x432_identity_graph_v2.json.gz``).

**What changes, and only where X4.3 left something unresolved.**

* *Revision rules* apply only to listing rows X4.3 could not resolve, and each needs independent
  support (:data:`REVISION_RULES`). A rule that finds a single candidate without that support
  records it as a rejected candidate; it is never applied.
* *Ledger V2* replaces V1 as the lifecycle input: notices without the exchange-index duplicates,
  the corrected class keys, the accepted transfers and ticker changes.
* *Successors.* A conversion whose ticker continues is split into predecessor and successor
  securities only with evidence of the successor (:data:`SUCCESSOR_RULES`). Listings after the
  conversion move to the successor; prices never cross.
* *Everything else* is rebuilt by the X4.3 functions unchanged, then compared with V1
  (``diff_vs_v1``) so that no change is silent.

Nothing here uses prices to establish identity. Prices only locate seams that the evidence has
already bounded, and they are reported, never relied on.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.backtesting.data import sessions_between
from app.research import ledger_v2 as l2
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import x4_evidence as x4
from app.research import x43_evidence as x43
from app.research import x432_evidence as x432

OUT = lc.OUT
GRAPH2_PATH = OUT / "x432_identity_graph_v2.json.gz"
LEDGER_V2_SHA256 = "42f5e7d49b9755a95e1f8f2a8d084fe12b5d75ea1285d3ce7cb60bd5a472b8dd"
CONFIRMED, UNRESOLVED, CONFLICTING = rs.CONFIRMED, rs.UNRESOLVED, rs.CONFLICTING
ACTIVE_BEFORE, ACTIVE_AFTER = timedelta(days=120), timedelta(days=31)
SUCCESSOR_FORMS = ("8-K12B", "8-K12G3", "8-K12B/A", "8-K12G3/A")
SUCCESSOR_WINDOW = timedelta(days=10)

REVISION_RULES: dict[str, str] = {
    "R1_NAME_FORM": "The listed issuer name equals one EDGAR filer name, after removing spacing, "
    "punctuation, state tags ('\\PA\\', '/ MA') and legal-form wording ('Public Limited "
    "Company' = 'plc', 'Aktiengesellschaft' = 'AG'), letters and word order unchanged. The "
    "filer must have used that name within 120 days before to 31 days after the capture, and be "
    "the only such filer. MEDIUM.",
    "R2_WORD_ORDER": "The same words in another order ('HUNT J B TRANSPORT' = 'J.B. Hunt "
    "Transport'), one active filer, AND the SEC's ticker list names the same CIK for the ticker "
    "(independent support). Without that support the candidate is rejected. MEDIUM.",
    "R3_TIEBREAK": "Several active filers share the name: accepted only when the SEC's ticker "
    "list names exactly one of them for the ticker. MEDIUM.",
    "R4_FUND_CLASS": "The SEC series/class files of that year (and the years either side) give "
    "the ticker to one share class only, AND the listed name carries the sponsor's name and at "
    "least three quarters of its distinctive words (abbreviations as prefixes) appear in the "
    "class's series, class or trust name. MEDIUM.",
    "R5_UNIT_TRUST": "A unit investment trust (no SEC share class): the listed name is the start "
    "of exactly one fund filer's EDGAR name, the rest being only 'ETF'/'Trust'/'Series', AND "
    "the SEC's ticker list names that filer for the ticker. MEDIUM.",
    "SUPPORT": "The SEC's current ticker list is support only: it never decides an identity on "
    "its own, because a ticker's current holder need not be its historical one.",
}
SUCCESSOR_RULES: dict[str, str] = {
    "S1_SUCCESSOR_REGISTRATION": "A successor-issuer notice (Form 8-K12B or 8-K12G3, filed when "
    "a class is deemed registered by succession) within 10 days of the conversion notice, by the "
    "issuer or by the filer the post-conversion listing names. The successor is that filer's "
    "class; it existed by the notice's filing date.",
    "S2_OLD_CLASS_AND_PROVIDER_RATIO": "The conversion notice names the class 'Old', the same "
    "issuer lists the ticker afterwards, and the provider records a share-ratio action on the "
    "ticker within 3 days: a new class of the same issuer, ratio known only from the provider.",
    "S3_FUND_CLASS_CHANGE": "The SEC series/class files give the ticker to one class before the "
    "conversion and to another class after it: the successor is the new class.",
    "NONE": "Otherwise the successor stays unidentified. Prices never pass from a predecessor to "
    "a successor in any case.",
}
LEGAL_FORMS = (
    (r"\bpublic limited company\b", "plc"),
    (r"\bpublic ltd co\b", "plc"),
    (r"\bpublic limited co\b", "plc"),
    (r"\baktiengesellschaft\b", "ag"),
    (r"\bsociedade anonima\b", "sa"),
    (r"\bsociedad anonima\b", "sa"),
    (r"\bnaamloze vennootschap\b", "nv"),
)
FUND_GENERIC = {"etf", "fund", "shares", "share", "index", "trust", "the", "and", "of", "series"}
UIT_EXTRA = {"etf", "trust", "series", "i", "1"}


# ---------------------------------------------------------------------- names
def tokens2(name: str | None) -> list[str]:
    """Name words after removing state tags and legal-form wording; consecutive single letters
    REDACTED_FOR_GITHUB"""
    s = html.unescape(name or "").lower()
    s = re.sub(r"[\\/]\s*[a-z]{2,3}\s*[\\/]?\s*$", " ", s)
    s = re.sub(r"[\\/][a-z]{2,3}[\\/]", " ", s)
    for pat, rep in LEGAL_FORMS:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\b(ny|new york) registry\b.*$", " ", s)  # a class wording, not the issuer
    toks: list[str] = []
    run = False
    for w in rs.words(s):
        if len(w) == 1 and w.isalpha():
            if run:
                toks[-1] += w
            else:
                toks.append(w)
            run = True
        else:
            toks.append(w)
            run = False
    while toks and toks[-1] in rs.SUFFIX:
        toks.pop()
    while toks and toks[0] == "the":
        toks.pop(0)
    return toks


def squash(name: str | None) -> str:
    return "".join(tokens2(name))


def bag(name: str | None) -> str:
    """Words in any order, legal-form words dropped wherever they stand."""
    return " ".join(sorted(t for t in tokens2(name) if t not in rs.SUFFIX))


class NameIndex:
    """EDGAR names (X4.1 full indexes) under the revised keys, with when each was used."""

    def __init__(self, ed: rs.Edgar) -> None:
        self.squash: dict[str, set[str]] = defaultdict(set)
        self.bag: dict[str, set[str]] = defaultdict(set)
        self.prefix: dict[str, list[tuple[str, str]]] = defaultdict(
            list
        )  # first word -> (key, cik)
        self.ed = ed
        for cik, names in ed.names.items():
            for n in names:
                k = squash(n)
                if k:
                    self.squash[k].add(cik)
                    self.bag[bag(n)].add(cik)
                    toks = tokens2(n)
                    self.prefix[toks[0]].append((" ".join(toks), cik))

    def active(self, cik: str, day: date, key_of: Any, key: str) -> bool:
        lo, hi = (day - ACTIVE_BEFORE).isoformat(), (day + ACTIVE_AFTER).isoformat()
        return any(
            key_of(n) == key and first <= hi and last >= lo
            for n, (first, last, _) in self.ed.names.get(cik, {}).items()
        )


def current_tickers(x4s: x4.Store) -> dict[str, set[str]]:
    """ticker -> CIKs, from the SEC's current company ticker list (support only)."""
    out: dict[str, set[str]] = defaultdict(set)
    body = x4.read_json(x4s.path("sec/current/company_tickers_exchange.json.gz"))
    for cik, _name, ticker, _exch in body.get("data", []):
        if ticker:
            out[str(ticker).upper().replace(".", "-")].add(f"{int(cik):010d}")
    return out


def _base(ticker: str) -> str:
    return re.split(r"[.$\- ]", ticker, maxsplit=1)[0].upper()


def _distinct(name: str) -> list[str]:
    """Distinctive words of a fund name: two or more characters (numbers kept), generic fund
    words dropped."""
    return [t for t in rs.words(name) if len(t) >= 2 and t not in FUND_GENERIC]


def _covers(listing: list[str], target: set[str]) -> float:
    if not listing:
        return 0.0
    hit = sum(
        1 for t in listing if t in target or any(len(t) >= 3 and u.startswith(t) for u in target)
    )
    return hit / len(listing)


# ---------------------------------------------------------------------- revision of listings
def revise(
    obs: list[rs.Obs],
    ed: rs.Edgar,
    funds: dict[int, list[dict[str, str]]],
    tick: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Apply :data:`REVISION_RULES` to the rows X4.3 left unresolved; return every decision
    (applied or rejected), one per listing row."""
    ix = NameIndex(ed)
    by_ticker: dict[tuple[int, str], list[dict[str, str]]] = defaultdict(list)
    for y, rows in funds.items():
        for r in rows:
            if r["ticker"]:
                by_ticker[(y, r["ticker"])].append(r)
    fund_filers = {
        cik
        for cik, names in ed.names.items()
        for n in names
        if re.search(r"\b(etf|trust|fund)\b", n, re.I)
    }
    out = []
    for o in obs:
        if o.status == CONFIRMED:
            continue
        rec: dict[str, Any] = {
            "ticker": o.ticker,
            "venue": o.venue,
            "listed_name": o.name,
            "day": o.day.isoformat(),
            "family": o.family,
            "x43_method": o.method,
            "rule": None,
            "verdict": "REJECTED",
            "candidate": None,
            "support": None,
            "explanation": "",
        }
        current = tick.get(o.ticker.replace(".", "-").replace("$", "-"), set()) | tick.get(
            _base(o.ticker), set()
        )
        # R4 fund class (also for rows the directories do not flag as ETF).
        rows = [
            r
            for y in (o.day.year - 1, o.day.year, o.day.year + 1)
            for r in by_ticker.get((y, o.ticker), [])
        ]
        ids = {r["class_id"] for r in rows}
        if len(ids) == 1:
            r = rows[0]
            target: set[str] = set()
            for x in rows:
                target |= set(_distinct(x["series_name"])) | set(_distinct(x["class_name"]))
                target |= set(_distinct(x["entity"]))
            lst = _distinct(o.name)
            sponsor = lst[0] if lst else ""
            cover = _covers(lst, target)
            rec.update(rule="R4_FUND_CLASS", candidate=f"SECCLASS:{r['class_id']}")
            if sponsor and sponsor in target and cover >= 0.75 and len(lst) >= 2:
                o.entity, o.security, o.cat = f"CIK:{r['cik']}", f"SECCLASS:{r['class_id']}", "FUND"
                o.status, o.confidence, o.method = CONFIRMED, "MEDIUM", "REVISION:R4_FUND_CLASS"
                rec.update(
                    verdict="APPLIED",
                    support=f"SEC files {sorted({x['artifact'] for x in rows})}",
                    explanation=f"one SEC class holds {o.ticker} ({r['series_name']!r}); "
                    f"sponsor '{sponsor}' and {cover:.0%} of the listed name's words "
                    "agree",
                )
            else:
                rec["explanation"] = (
                    f"one SEC class holds {o.ticker} ({r['series_name']!r}) but the listed name "
                    f"does not agree (sponsor in names: {sponsor in target}, words {cover:.0%}, "
                    f"{len(lst)} distinctive words)"
                )
            out.append(rec)
            continue
        if len(ids) > 1:
            rec.update(rule="R4_FUND_CLASS", explanation="several SEC classes hold the ticker")
            out.append(rec)
            continue
        # R1 / R3 / R2 / R5 on the issuer name.
        issuer = o.issuer_text
        key = squash(issuer)
        found = {c for c in ix.squash.get(key, set()) if ix.active(c, o.day, squash, key)}
        if len(found) == 1:
            cik = next(iter(found))
            _apply(o, cik, "REVISION:R1_NAME_FORM")
            rec.update(
                rule="R1_NAME_FORM",
                verdict="APPLIED",
                candidate=f"CIK:{cik}",
                explanation=f"the listed issuer name equals an EDGAR name of CIK {cik} "
                "in use then, apart from spacing, punctuation, state tag or legal form",
            )
            out.append(rec)
            continue
        if len(found) > 1:
            pick = found & current
            rec.update(rule="R3_TIEBREAK", candidate=sorted(f"CIK:{c}" for c in found))
            if len(pick) == 1:
                cik = next(iter(pick))
                _apply(o, cik, "REVISION:R3_TIEBREAK")
                rec.update(
                    verdict="APPLIED",
                    candidate=f"CIK:{cik}",
                    support="SEC current ticker list",
                    explanation=f"{len(found)} active filers share the name; the SEC's "
                    f"ticker list names CIK {cik} for {o.ticker}",
                )
            else:
                rec["explanation"] = (
                    "several active filers share the name; no independent tie-break"
                )
            out.append(rec)
            continue
        bkey = bag(issuer)
        bfound = {c for c in ix.bag.get(bkey, set()) if ix.active(c, o.day, bag, bkey)}
        if len(bfound) > 1:
            pick = bfound & current
            rec.update(rule="R2_WORD_ORDER", candidate=sorted(f"CIK:{c}" for c in bfound))
            if len(pick) == 1:
                cik = next(iter(pick))
                _apply(o, cik, "REVISION:R2_WORD_ORDER")
                rec.update(
                    verdict="APPLIED",
                    candidate=f"CIK:{cik}",
                    support="SEC current ticker list",
                    explanation=f"{len(bfound)} active filers have these words; the SEC's "
                    f"ticker list names CIK {cik} for {o.ticker}",
                )
            else:
                rec["explanation"] = (
                    f"a word-order match shared by {len(bfound)} active filers, none (or several) "
                    "named for the ticker by the SEC's list: not applied"
                )
            out.append(rec)
            continue
        if len(bfound) == 1:
            cik = next(iter(bfound))
            rec.update(rule="R2_WORD_ORDER", candidate=f"CIK:{cik}")
            if cik in current:
                _apply(o, cik, "REVISION:R2_WORD_ORDER")
                rec.update(
                    verdict="APPLIED",
                    support="SEC current ticker list",
                    explanation=f"the same words in another order as CIK {cik}'s EDGAR "
                    f"name, and the SEC's ticker list names CIK {cik} for {o.ticker}",
                )
            else:
                rec["explanation"] = (
                    "a word-order match without independent support (the SEC's ticker list "
                    "does not name this CIK for the ticker)"
                )
            out.append(rec)
            continue
        toks = tokens2(issuer)
        if toks:
            starts = {
                cik
                for k, cik in ix.prefix.get(toks[0], [])
                if cik in fund_filers
                and k.split()[: len(toks)] == toks
                and set(k.split()[len(toks) :]) <= UIT_EXTRA
                and len(k.split()) > len(toks)
            }
            if len(starts) == 1:
                cik = next(iter(starts))
                rec.update(rule="R5_UNIT_TRUST", candidate=f"CIK:{cik}")
                if cik in current:
                    o.entity, o.security = f"CIK:{cik}", f"CIK:{cik}|TRUST_UNITS"
                    o.cat, o.status, o.confidence = "FUND", CONFIRMED, "MEDIUM"
                    o.method = "REVISION:R5_UNIT_TRUST"
                    rec.update(
                        verdict="APPLIED",
                        support="SEC current ticker list",
                        explanation=f"the listed name begins the EDGAR name of fund filer "
                        f"CIK {cik}, and the SEC's ticker list names it for {o.ticker}",
                    )
                else:
                    rec["explanation"] = "a unit-trust name match without independent support"
                out.append(rec)
                continue
        rec["explanation"] = "no revised rule finds a candidate"
        out.append(rec)
    _propagate(obs)
    return out


def _apply(o: rs.Obs, cik: str, method: str) -> None:
    o.entity = f"CIK:{cik}"
    o.security = f"CIK:{cik}|{rs.security_label(o.cat, o.class_text)}"
    o.status, o.confidence, o.method = CONFIRMED, "MEDIUM", method
    o.candidates = []


def _propagate(obs: list[rs.Obs]) -> None:
    """Company-list rows take the class the symbol directories show (as in X4.3)."""
    sym: dict[tuple[str | None, str], dict[str | None, str]] = defaultdict(dict)
    for o in obs:
        if o.source == "symdir" and o.status == CONFIRMED and o.security:
            sym[(o.entity, o.ticker)][o.security] = o.cat
    for o in obs:
        if (
            o.source == "companylist"
            and o.status == CONFIRMED
            and o.method.startswith("REVISION")
            and (o.security or "").endswith("|UNSPECIFIED")
        ):
            known = sym.get((o.entity, o.ticker), {})
            if len(known) == 1:
                o.security, o.cat = next(iter(known.items()))


# ---------------------------------------------------------------------- successors
def successor_filings(x4s: x4.Store) -> list[dict[str, str]]:
    """Successor-issuer notices (8-K12B, 8-K12G3) in the captured EDGAR full indexes."""
    line = re.compile(r"(\S.*?)\s{2,}(.+?)\s{2,}(\d+)\s+(\d{4}-\d\d-\d\d)")
    out = []
    for rel in sorted(k for k in x4s.entries if k.startswith("sec/full-index/")):
        for ln in gzip.decompress(x4s.path(rel).read_bytes()).decode("latin-1").splitlines():
            m = line.match(ln)
            if m and m.group(1).strip() in SUCCESSOR_FORMS:
                out.append(
                    {
                        "form": m.group(1).strip(),
                        "name": m.group(2).strip(),
                        "cik": f"{int(m.group(3)):010d}",
                        "filed": m.group(4),
                        "artifact": rel,
                    }
                )
    return out


def successors(
    v2: dict[str, Any],
    obs: list[rs.Obs],
    filings: list[dict[str, str]],
    actions: list[dict[str, Any]],
    funds: dict[int, list[dict[str, str]]],
    x4s: x4.Store,
) -> list[dict[str, Any]]:
    """Each Ledger V2 successor, identified under :data:`SUCCESSOR_RULES` or not. Identified
    successors take over the ticker's listing rows after the conversion."""
    out = []
    for s in v2["successors"]:
        pred, t, filed = s["predecessor"], s["ticker"], s["filed"]
        f = date.fromisoformat(filed)
        issuer = pred.split("|", 1)[0] if pred.startswith("CIK:") else None
        before = [o for o in obs if o.ticker == t and o.security == pred and o.day <= f]
        after = sorted(
            (o for o in obs if o.ticker == t and o.day > f and o.status == CONFIRMED),
            key=lambda o: o.day,
        )
        after_names = {squash(o.issuer_text) for o in after}
        rec: dict[str, Any] = {
            "predecessor": pred,
            "ticker": t,
            "conversion_notice": s["notice"],
            "filed": filed,
            "x431_relationship": s["relationship"],
            "rule": "NONE",
            "successor_security": None,
            "status": UNRESOLVED,
            "confidence": "LOW",
            "start_not_before": None,
            "start_not_after": None,
            "evidence": list(s["evidence"]),
            "price_inheritance": "FORBIDDEN",
            "explanation": "",
        }
        if s["relationship"] == "SUCCESSOR_CLASS":  # REDACTED_FOR_GITHUB
            rec.update(
                rule="X431_SUCCESSOR_CLASS",
                successor_security=s["successor_security"],
                status=CONFIRMED,
                confidence="MEDIUM",
                explanation=s["explanation"],
            )
            out.append(rec)
            continue
        regs = [
            r
            for r in filings
            if abs((date.fromisoformat(r["filed"]) - f).days) <= SUCCESSOR_WINDOW.days
            and (f"CIK:{r['cik']}" == issuer or squash(r["name"]) in after_names)
        ]
        old = bool(re.search(r"\bold\b", lc.norm(_class_of(v2, pred, s))))
        ratio = [
            a
            for a in actions
            if a.get("symbol") == t
            and a["type"] in ("reverse_splits", "forward_splits")
            and abs((date.fromisoformat(a["ex_date"]) - f).days) <= 3
        ]
        fund_new, fund_seq = _fund_class_change(funds, t, f)
        succ_entity = None
        if regs:
            r = sorted(regs, key=lambda r: (r["filed"], r["cik"]))[0]
            succ_entity = f"CIK:{r['cik']}"
            rec["rule"] = "S1_SUCCESSOR_REGISTRATION"
            rec["evidence"].append(
                {
                    "artifact": x4s.root.as_posix() + "/" + r["artifact"],
                    "sha256": x4s.entries[r["artifact"]]["sha256"],
                    "locator": f"{r['form']} {r['name']} CIK {r['cik']} filed {r['filed']}",
                }
            )
            rec["start_not_after"] = min([r["filed"]] + [o.day.isoformat() for o in after[:1]])
            rec["registration"] = {k: r[k] for k in ("form", "name", "cik", "filed")}
        elif old and after and ratio:
            succ_entity = issuer
            rec["rule"] = "S2_OLD_CLASS_AND_PROVIDER_RATIO"
            rec["provider_action"] = {
                k: ratio[0].get(k) for k in ("type", "ex_date", "old_rate", "new_rate")
            }
            rec["evidence"].append(
                {
                    "artifact": x432.ROOT.as_posix() + "/" + ratio[0]["artifact"],
                    "locator": f"{ratio[0]['type']}[{ratio[0]['index']}]",
                }
            )
            rec["start_not_after"] = after[0].day.isoformat()
        elif fund_new:
            rec["rule"] = "S3_FUND_CLASS_CHANGE"
            rec["successor_security"] = f"SECCLASS:{fund_new['class_id']}"
            rec["fund_class"] = fund_new
        if before:
            rec["start_not_before"] = max(o.day for o in before).isoformat()
        if rec["rule"] in ("S1_SUCCESSOR_REGISTRATION", "S2_OLD_CLASS_AND_PROVIDER_RATIO"):
            label = pred.split("|", 1)[1] if "|" in pred else "CLASS"
            rec["successor_security"] = (
                f"{pred}#SUCCESSOR@{filed}" if succ_entity == issuer else f"{succ_entity}|{label}"
            )
        if rec["successor_security"]:
            rec.update(status=CONFIRMED, confidence="MEDIUM")
            rec["explanation"] = (
                f"{SUCCESSOR_RULES[rec['rule']]} The successor is {rec['successor_security']}; "
                f"it started no earlier than {rec['start_not_before']} (the predecessor's last "
                f"listing) and no later than {rec['start_not_after']}. A separate security."
            )
            moved = 0
            for o in after:
                if o.ticker == t and (o.security == pred or o.entity == succ_entity):
                    o.security = rec["successor_security"]
                    o.method += "+SUCCESSOR_SPLIT"
                    moved += 1
            rec["listing_rows_moved"] = moved
        else:
            if fund_seq:
                rec["sec_fund_files"] = fund_seq
            rec["explanation"] = (
                "no successor-issuer notice within 10 days, no 'Old' class with a provider ratio "
                "action, and no SEC fund class change in the next year's file: the successor "
                "stays unidentified"
                + (f" (SEC files by year: {fund_seq})" if fund_seq else "")
                + (
                    f" (a provider {ratio[0]['type']} on {ratio[0]['ex_date']} is recorded, but "
                    "the notice does not name an old class)"
                    if ratio
                    else ""
                )
            )
        out.append(rec)
    return out


def _class_of(v2: dict[str, Any], key: str, s: dict[str, Any]) -> str:
    sec = next((x for x in v2["securities"] if x["security_key"] == key), None)
    return (sec or {}).get("class_description") or ""


def _fund_class_change(
    funds: dict[int, list[dict[str, str]]], ticker: str, f: date
) -> tuple[dict[str, Any] | None, dict[int, list[str]]]:
    """S3: the ticker's SEC class in the conversion year's file and in the next year's file.
    A change counts only if the next file already shows a new class and not the old one."""
    seq: dict[int, list[str]] = {
        y: sorted({r["class_id"] for r in rows if r["ticker"] == ticker})
        for y, rows in sorted(funds.items())
        if any(r["ticker"] == ticker for r in rows)
    }
    old, nxt = set(seq.get(f.year, [])), set(seq.get(f.year + 1, []))
    if old and len(nxt) == 1 and not nxt & old:
        cid = next(iter(nxt))
        r = next(r for r in funds[f.year + 1] if r["class_id"] == cid)
        return {
            "class_id": cid,
            "series": r["series_name"],
            "cik": r["cik"],
            "first_year": f.year + 1,
        }, seq
    return None, seq


# REDACTED_FOR_GITHUB
def csj_ciu(*_args: object, **_kwargs: object) -> object:
    raise NotImplementedError("REDACTED_FOR_GITHUB")


J6 = (
    "J6_ADJACENT_DOCUMENTED_SEAM: two provider segments of one security (J1) that meet at "
    "consecutive sessions with no gap and no overlap may be joined when the seam is a confirmed "
    "transfer of that security (J3, J4) and the provider breaks sibling securities of the same "
    "action at the same seam with identical overlapping bars. MEDIUM; prices are never compared."
)


# ---------------------------------------------------------------------- price jumps
def jumps(
    v2: dict[str, Any],
    actions: list[dict[str, Any]],
    dividends: dict[tuple[str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """The X4.3.1 jumps reassessed with the provider's corporate-action records."""
    tickers_of: dict[str, set[str]] = defaultdict(set)
    for e in v2["events"]:
        for k in ("ticker", "from_ticker", "to_ticker"):
            if e[k]:
                tickers_of[e["security_key"]].add(e[k])
    ident = {i["asset_id"]: i for i in v2["provider_histories"]["identities"]}
    out = []
    for j in v2["provider_histories"]["jumps"]:
        i = ident[j["asset_id"]]
        syms = {i["request"]["ticker"], i["request"]["label"].split("~")[0].rstrip("#")}
        for s in i["segments"]:
            syms |= tickers_of.get(s["security_key"] or "", set())
        acts = [a for a in actions if a.get("symbol") in syms and a.get("ex_date") == j["to"]]
        rec = {
            **{
                k: j[k]
                for k in (
                    "asset_id",
                    "label",
                    "from",
                    "to",
                    "raw_close",
                    "split_close",
                    "raw_ratio",
                    "adjustment_factor_before",
                )
            },
            "x431_finding": j["finding"],
            "symbols": sorted(syms),
            "provider_actions": [
                {k: a.get(k) for k in ("type", "symbol", "ex_date", "old_rate", "new_rate")}
                for a in acts
            ],
        }
        split = next((a for a in acts if a["type"] in ("reverse_splits", "forward_splits")), None)
        if j["finding"] == "SECURITY_BOUNDARY":
            rec.update(
                finding="SECURITY_CHANGE",
                status=CONFIRMED,
                explanation="the series passes to another security here (X4.3.1)"
                + (
                    f"; the provider also records a {split['type'][:-1]} "
                    f"({split['old_rate']}->{split['new_rate']}) on {split['symbol']}"
                    if split
                    else ""
                ),
            )
        elif split:
            ratio = float(split["new_rate"]) / float(split["old_rate"])
            implied = 1 / ratio if ratio else 0
            raw_like = abs(j["raw_ratio"] * ratio - 1) < 0.25 if ratio else False
            rec.update(
                finding="PROVIDER_ADJUSTMENT_ARTIFACT" if not raw_like else "SPLIT",
                status=CONFIRMED,
                explanation=(
                    f"the provider records a {split['type'][:-1]} on {split['symbol']} "
                    f"({split['old_rate']}->{split['new_rate']}, an adjustment factor of "
                    f"{implied:g}) on {j['to']}, but the raw close moves only "
                    f"{j['raw_ratio']:.2f}x: the provider encodes an action the raw prices do not "
                    "show as a split. The underlying action (consistent with a spin-off) is not "
                    "in the SEC evidence and stays unresolved as an action"
                    if not raw_like
                    else "a split the raw prices show"
                ),
                corporate_action=UNRESOLVED if not raw_like else CONFIRMED,
            )
        elif j["finding"] == "CASH_DISTRIBUTION":
            rec.update(finding="CASH_DISTRIBUTION", status=CONFIRMED, explanation=j["explanation"])
        else:
            near = [
                a
                for a in actions
                if a.get("symbol") in syms
                and a.get("ex_date")
                and abs((date.fromisoformat(a["ex_date"]) - date.fromisoformat(j["to"])).days) <= 30
            ]
            rec.update(
                finding="RAW_MOVE_UNEXPLAINED",
                status=UNRESOLVED,
                explanation=f"the raw close moves {j['raw_ratio']:.2f}x; the provider records no "
                "corporate action on the date"
                + (
                    f" (only {[(a['type'], a['ex_date']) for a in near]} within 30 days)"
                    if near
                    else " or within 30 days"
                )
                + ", and no distribution is captured. Not explained by the evidence; a market move "
                "is possible and nothing is assumed",
            )
        out.append(rec)
    return out


# ---------------------------------------------------------------------- notices triage
EQUITY_LIKE = rs.GENERIC | {"FUND", "BENEFICIAL_INTEREST", "LP_UNIT", "ETN", "OTHER"}


def triage(graph2: dict[str, Any]) -> dict[str, Any]:
    """Unlinked notices by whether they can bear on universe membership (an equity or fund
    class) or only on the lifecycle of other classes, and by why they are unlinked."""
    issuers = {e["entity_key"] for e in graph2["ticker_edges"]}
    rows = []
    for n in graph2["notices"]:
        if n["status"] == CONFIRMED:
            continue
        tier = "UNIVERSE_RELEVANT" if n["class_category"] in EQUITY_LIKE else "LIFECYCLE_ONLY"
        terminal = bool(n["terminal_events"])
        rows.append(
            {
                "accession": n["accession"],
                "security_key": n["security_key"],
                "venue": n["venue"],
                "filed": n["filed"],
                "category": n["class_category"],
                "terminal": terminal,
                "tier": tier,
                "reason": n["method"],
                "issuer_listed_in_captures": n["issuer"] in issuers,
                "status": n["status"],
            }
        )
    by = Counter((r["tier"], r["reason"]) for r in rows)
    priority = [
        r
        for r in rows
        if r["tier"] == "UNIVERSE_RELEVANT" and r["terminal"] and r["issuer_listed_in_captures"]
    ]
    return {
        "unlinked": len(rows),
        "by_tier_and_reason": {f"{a}|{b}": c for (a, b), c in sorted(by.items())},
        "by_tier": dict(sorted(Counter(r["tier"] for r in rows).items())),
        "universe_relevant_terminal": sum(
            1 for r in rows if r["tier"] == "UNIVERSE_RELEVANT" and r["terminal"]
        ),
        "priority_for_x44": priority,
        "rows": rows,
    }


# ---------------------------------------------------------------------- build
def load_ledger_v2(path: Path = l2.V2_PATH, expect: str = LEDGER_V2_SHA256) -> dict[str, Any]:
    got = l2._sha_file(path)
    if got != expect:
        raise ValueError(f"{path}: sha256 {got} is not the Ledger V2 {expect}")
    doc: dict[str, Any] = x4.read_json(path)
    if lc.content_sha256(doc["events"]) != doc["content_sha256"]:
        raise ValueError(f"{path}: events do not hash to the recorded content")
    return doc


def observations_v2(
    x4s: x4.Store,
    x432s: x432.Store,
    v2: dict[str, Any],
    funds: dict[int, list[dict[str, str]]] | None = None,
) -> tuple[
    list[rs.Obs],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    """Graph V2's listing rows: X4.3's resolution, then the revision rules, then the successor
    splits. Returns (rows, revisions, successors, successor filings, provider actions)."""
    funds = funds if funds is not None else lc.fund_classes(x4s)
    ed = rs.edgar(x4s)
    obs = rs.observations(x4s)
    rs.resolve_observations(obs, ed, funds)
    tick = current_tickers(x4s) if x4s.has("sec/current/company_tickers_exchange.json.gz") else {}
    revisions = revise(obs, ed, funds, tick)
    actions = x432.corporate_actions(x432s)
    filings = successor_filings(x4s)
    succ = successors(v2, obs, filings, actions, funds, x4s)
    return obs, revisions, succ, filings, actions


def build(
    x4s: x4.Store,
    x43s: x43.Store,
    x432s: x432.Store,
    graph: dict[str, Any],
    v2: dict[str, Any],
    graph_sha256: str = l2.GRAPH_SHA256,
    v2_sha256: str = LEDGER_V2_SHA256,
) -> dict[str, Any]:
    funds = lc.fund_classes(x4s)
    obs, revisions, succ, filings, actions = observations_v2(x4s, x432s, v2, funds)
    ix = rs.Index.of(obs)
    edges, loose = rs.ticker_edges(obs)
    notes, nstats = rs.notices(x4s, v2, obs, ix)
    reuse = rs.ticker_reuse(edges, funds, x4s)
    prov = (
        rs.provider_links(x4s, x43s, ix, v2, edges, notes)
        if x43s.has("db/provider_identities.json.gz")
        else []
    )
    divs = rs.dividend_links(x4s, ix, v2)
    doc: dict[str, Any] = {
        "kind": "x432_identity_graph",
        "decision": "D-249",
        "graph_code": "SECURITY_IDENTITY_GRAPH_V2",
        "supersedes": {
            "graph_code": graph["graph_code"],
            "graph_sha256": graph_sha256,
            "content_sha256": graph["content_sha256"],
        },
        "inputs": {
            "x41_manifest_sha256": x4.sha256(x4s.manifest_path.read_bytes()),
            "x43_manifest_sha256": x4.sha256(x43s.manifest_path.read_bytes())
            if x43s.manifest_path.exists()
            else None,
            "x432_manifest_sha256": x4.sha256(x432s.manifest_path.read_bytes())
            if x432s.manifest_path.exists()
            else None,
            "x43_graph_sha256": graph_sha256,
            "ledger_v2_sha256": v2_sha256,
            "ledger_v2_content_sha256": v2["content_sha256"],
        },
        "rules": {
            "revision": REVISION_RULES,
            "successor": SUCCESSOR_RULES,
            "join": {**l2.JOIN_RULES, "J6_ADJACENT_DOCUMENTED_SEAM": J6},
        },
        "notices": notes,
        "ticker_edges": edges,
        "unresolved_listings": loose,
        "ticker_reuse": reuse,
        "provider_links": prov,
        "dividends": divs,
        "probes": graph["probes"],
        "revisions": revisions,
        "successors": succ,
        "csj_ciu": csj_ciu(graph, v2, funds),
        "jumps": jumps(v2, actions, l2._dividends(x4s)),
    }
    doc["ledger_proposals"] = rs.ledger_proposals(edges, notes, v2)
    doc["notice_triage"] = triage(doc)
    doc["successor_registrations_in_histories"] = registration_guard(doc, filings)
    doc["histories"] = histories(doc, graph, v2, succ)
    doc["candidates_80"] = candidates(v2, revisions)
    doc["diff_vs_v1"] = diff(graph, doc)
    doc["summary"] = rs.summarize(doc, nstats) | summary(doc)
    doc["content_sha256"] = rs.content_sha256(doc)
    return doc


def histories(
    doc: dict[str, Any], graph: dict[str, Any], v2: dict[str, Any], succ: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """REDACTED_FOR_GITHUB"""
    links = {p["asset_id"]: p for p in doc["provider_links"]}
    rev_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in doc["revisions"]:
        rev_by_ticker[r["ticker"]].append(r)
    regs = {s["successor_security"]: s for s in succ if s["status"] == CONFIRMED}
    guard = {g["asset_id"]: g for g in doc.get("successor_registrations_in_histories", [])}
    out = []
    for i in v2["provider_histories"]["identities"]:
        if i["verdict"] != "UNATTRIBUTED":
            continue
        p = links[i["asset_id"]]
        tickers = sorted({t for f in p["formations"] for t in f["tickers"]})
        rules = sorted(
            {r["rule"] for t in tickers for r in rev_by_ticker[t] if r["verdict"] == "APPLIED"}
        )
        rejected = sorted(
            {
                f"{r['rule']}: {r['explanation']}"
                for t in tickers
                for r in rev_by_ticker[t]
                if r["verdict"] == "REJECTED"
            }
        )
        if p["status"] == CONFIRMED and i["asset_id"] in guard:
            disp = "RESOLVED_FROM_SUCCESSION"
        elif p["status"] == CONFIRMED:
            disp = "RESOLVED"
        elif p["status"] == CONFLICTING:
            disp = "CONFLICTING"
        else:
            disp = "STILL_UNRESOLVED"
        out.append(
            {
                "asset_id": i["asset_id"],
                "label": i["request"]["label"],
                "tickers": tickers,
                "research_type": p["research_type"],
                "disposition": disp,
                "security_key": p["security_key"],
                "status": p["status"],
                "confidence": p["confidence"],
                "rules_applied": rules,
                "rejected": rejected[:4],
                "conflicts": p["conflicts"],
                "findings": [
                    f["kind"]
                    for f in p["findings"]
                    if f["kind"] not in ("PARTIAL_HOLDER_EVIDENCE", "BAR_GAP")
                ],
                "successor_split": regs.get(p["security_key"] or "", {}).get("filed"),
                "attributed_from": guard.get(i["asset_id"], {}).get("attributed_from"),
                "explanation": p["explanation"],
            }
        )
    return out


def registration_guard(doc: dict[str, Any], filings: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Provider histories whose linked issuer registered as a successor issuer (8-K12B,
    8-K12G3) inside the history's bars: the bars before that registration may belong to the
    predecessor and are not attributed to the linked security."""
    by_cik: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in filings:
        by_cik[f"CIK:{r['cik']}"].append(r)
    out = []
    for p in doc["provider_links"]:
        if p["status"] != CONFIRMED or not p["entity_key"]:
            continue
        inside = [
            r
            for r in by_cik.get(p["entity_key"], [])
            if (p["first_bar"] or "") < r["filed"] <= (p["last_bar"] or "")
        ]
        if inside:
            r = min(inside, key=lambda r: r["filed"])
            out.append(
                {
                    "asset_id": p["asset_id"],
                    "label": p["request"]["label"],
                    "security_key": p["security_key"],
                    "registration": r,
                    "attributed_from": r["filed"],
                    "status": UNRESOLVED,
                    "explanation": f"{p['entity_key']} filed {r['form']} on {r['filed']} as a "
                    "successor issuer; bars before it are not attributed to "
                    f"{p['security_key']} (the predecessor is not identified here)",
                }
            )
    return out


def candidates(v2: dict[str, Any], revisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """REDACTED_FOR_GITHUB"""
    by = {(r["ticker"], r["venue"], r["listed_name"]): r for r in revisions}
    out = []
    for c in v2["unresolved"]["listing_name_candidates"]:
        r = by.get((c["ticker"], c["venue"], c["listed_name"]))
        verdict = "NOT_REVISED"
        if r and r["verdict"] == "APPLIED":
            verdict = "APPLIED" if r["candidate"] == c["candidate"] else "APPLIED_OTHER_CIK"
        elif r:
            verdict = "REJECTED"
        out.append(
            {
                **{k: c[k] for k in ("ticker", "venue", "listed_name", "candidate")},
                "verdict": verdict,
                "rule": r["rule"] if r else None,
                "explanation": r["explanation"] if r else "no revision record",
            }
        )
    return out


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    def ekey(e: dict[str, Any]) -> tuple[str, str, str]:
        return (e["security_key"], e["ticker"], e["venue"])

    oe, ne = {ekey(e) for e in old["ticker_edges"]}, {ekey(e) for e in new["ticker_edges"]}
    op = {p["asset_id"]: p for p in old["provider_links"]}
    changes = []
    for p in new["provider_links"]:
        q = op.get(p["asset_id"])
        if q and (q["status"], q["security_key"]) != (p["status"], p["security_key"]):
            changes.append(
                {
                    "asset_id": p["asset_id"],
                    "label": p["request"]["label"],
                    "v1": [q["status"], q["security_key"]],
                    "v2": [p["status"], p["security_key"]],
                }
            )

    def nkey(n: dict[str, Any]) -> tuple[str, str]:
        return (n["accession"], n["class_description"] or "")

    on = {nkey(n): n for n in old["notices"]}
    nchg: Counter[str] = Counter()
    for n in new["notices"]:
        o = on.get(nkey(n))
        if o is None:
            nchg["new"] += 1
        elif o["status"] != n["status"]:
            nchg[f"{o['status']}->{n['status']}"] += 1
    return {
        "ticker_edges": {
            "v1": len(oe),
            "v2": len(ne),
            "added": len(ne - oe),
            "removed": len(oe - ne),
        },
        "unresolved_listings": {
            "v1": len(old["unresolved_listings"]),
            "v2": len(new["unresolved_listings"]),
        },
        "provider_link_changes": changes,
        "provider_links_by_status": {
            "v1": dict(Counter(p["status"] for p in old["provider_links"])),
            "v2": dict(Counter(p["status"] for p in new["provider_links"])),
        },
        "notice_status_changes": dict(sorted(nchg.items())),
        "notices": {"v1": len(old["notices"]), "v2": len(new["notices"])},
        "ticker_reuse": {"v1": len(old["ticker_reuse"]), "v2": len(new["ticker_reuse"])},
        "removed_edges_sample": sorted([list(k) for k in oe - ne])[:50],
    }


def summary(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "revisions": dict(
            sorted(Counter(f"{r['rule']}|{r['verdict']}" for r in doc["revisions"]).items())
        ),
        "histories_70": dict(sorted(Counter(h["disposition"] for h in doc["histories"]).items())),
        "successor_registrations_in_histories": len(doc["successor_registrations_in_histories"]),
        "candidates_80": dict(sorted(Counter(c["verdict"] for c in doc["candidates_80"]).items())),
        "successors": dict(
            sorted(Counter(f"{s['rule']}|{s['status']}" for s in doc["successors"]).items())
        ),
        "csj_ciu": {c["ticker"]: c["decision"] for c in doc["csj_ciu"]},
        "jumps": dict(sorted(Counter(j["finding"] for j in doc["jumps"]).items())),
        "notice_triage": doc["notice_triage"]["by_tier"],
    }


def write(doc: dict[str, Any], path: Path = GRAPH2_PATH) -> str:
    if path.exists():
        raise FileExistsError(f"{path} exists: the graph V2 is written once")
    data = x4.gz(x4.canonical(rs._plain(doc)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def verify(path: Path, x4s: x4.Store, x43s: x43.Store, x432s: x432.Store) -> dict[str, Any]:
    stored = x4.read_json(path)
    rebuilt = build(x4s, x43s, x432s, l2.load_graph(), load_ledger_v2())
    return {
        "file_sha256": l2._sha_file(path),
        "stored_content_sha256": stored["content_sha256"],
        "recomputed_content_sha256": rs.content_sha256(stored),
        "rebuilt_content_sha256": rebuilt["content_sha256"],
        "ok": stored["content_sha256"] == rs.content_sha256(stored) == rebuilt["content_sha256"],
    }


__all__ = [
    "GRAPH2_PATH",
    "REVISION_RULES",
    "SUCCESSOR_RULES",
    "build",
    "revise",
    "squash",
    "tokens2",
    "verify",
    "write",
]
