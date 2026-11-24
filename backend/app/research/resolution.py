"""X4.3 (D-247): historical entity resolution, a point-in-time identity graph.

:func:`build` reads the X4.1 evidence store, the X4.2 lifecycle ledger (verified by its
SHA-256) and the X4.3 evidence store, and never writes to any of them. It returns a
deterministic graph document that :func:`write` stores once
(``config/simulation/lifecycle/x43_identity_graph_v1.json.gz``).

**Outcomes.** Every relationship carries ``status`` (``CONFIRMED``, ``UNRESOLVED`` or
``CONFLICTING``), ``confidence`` (``HIGH``, ``MEDIUM``, ``LOW``), the ``method`` it was made
by, its ``evidence`` (artifact, SHA-256, locator), an ``explanation`` and its ``conflicts``. A
``LOW`` match is never ``CONFIRMED``: it stays ``UNRESOLVED`` with its candidate recorded.

**Issuers of listed securities.** A listing row (an archived symbol directory or company list
row: ticker, venue, listed name, capture date) is linked to an SEC issuer by its *listed name
at that time*: the issuer part of the name, normalised (case, punctuation, corporate suffixes
removed), must equal the name under which a CIK filed with EDGAR in 2015-2020 (the captured
full indexes: historical names, not today's). An exchange-traded fund row is linked to its
SEC share class by its ticker *and* its listed name, both equal to the SEC series/class file
of that year. A name used by several CIKs is narrowed to those filing periodic reports; still
ambiguous, the row stays unresolved. A ticker never identifies an issuer.

**Notices.** A Form 25 / 25-NSE is one document (an accession number). EDGAR indexes each 25-NSE
under the issuer *and* under the exchange, and X4.1 captured both: X4.3 keeps the issuer copy
and records the other as a duplicate. The issuer is the CIK inside the notice (the filer for a
Form 25). A notice is linked to a ticker only through listing rows of the same issuer, the
same venue and a compatible class (category, series, coupon and maturity), listed before the
filing; the next capture of that directory must not list it on that venue any more
(corroboration). Listed again well after the filing, the link is ``CONFLICTING``.

**Provider identities.** A provider identity (a research security: ticker, ``asof`` mapping,
bars) is linked to a security by its *prices*, not its ticker: its bars must equal, session for
session (raw close and volume), the provider's bars of a ticker in the X4.1 formation sweeps;
the holder of that ticker at the formation is then read from the listings captured before and
after it. The linked security's lifecycle is checked against the bars: bars after the
security's end (contamination), bars ending while the security is still listed (truncation),
and price jumps.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import itertools
import math
import re
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.research import lifecycle as lc
from app.research import x4_evidence as x4
from app.research import x43_evidence as x43

OUT = lc.OUT
GRAPH_CODE = "SECURITY_IDENTITY_GRAPH_V1"
LEDGER = OUT / "x42_ledger_v1.json"
GRAPH_PATH = OUT / "x43_identity_graph_v1.json.gz"
LEDGER_SHA256 = "a6b93d376d0de3700ad9ee1df89c8af1f3d5b26a5885614b750bb02bc67d10fe"
CONFIRMED, UNRESOLVED, CONFLICTING = "CONFIRMED", "UNRESOLVED", "CONFLICTING"
RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
PENDING_DAYS = 45  # a removal is effective about ten days after filing; allow for that
STALE_DAYS = 400  # a listing older than this before a filing identifies it less firmly
JUMP = math.log(3)  # a split-adjusted close-to-close move of 3x or more is flagged
MIN_FINGERPRINT = 20  # sessions that must be identical for two bar series to be one
BARS_END = date(2019, 12, 31)
ACCESSION = re.compile(r"(\d{10}-\d{2}-\d{6})")

# EDGAR forms whose filer is an issuer of listed securities (names indexed for matching).
ISSUER_FORMS = {
    "10-K",
    "10-Q",
    "20-F",
    "40-F",
    "8-K",
    "6-K",
    "DEF 14A",
    "N-CSR",
    "N-CSRS",
    "N-Q",
    "485BPOS",
    "25",
    "25-NSE",
    "S-1",
    "S-3",
    "S-4",
    "F-1",
    "F-3",
    "F-4",
    "424B2",
    "424B3",
    "424B4",
    "424B5",
    "10-12B",
    "8-A12B",
    "10-KT",
    "20-F/A",
    "10-K/A",
    "15-12B",
    "15-12G",
}
PERIODIC = {"10-K", "10-Q", "20-F", "40-F", "N-CSR", "N-CSRS", "N-Q", "10-KT"}
SUFFIX = {
    "inc",
    "corp",
    "co",
    "ltd",
    "plc",
    "llc",
    "lp",
    "sa",
    "nv",
    "ag",
    "se",
    "the",
    "new",
    "et",
    "al",
    "de",
    "md",
    "sab",
    "cv",
    "spa",
    "asa",
    "ab",
    "oyj",
    "bv",
    "pte",
    "pty",
    "lllp",
}
CORP_END = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "plc",
    "llc",
    "lp",
    "sa",
    "nv",
    "ag",
    "se",
    "trust",
    "fund",
}
CLASS_START = re.compile(
    r"\s(?:common stock|common shares|common stk|common units|common|class [a-z]\b|cl [a-z]\b|"
    r"american depositary|american depository|sponsored adr|dep shs|depositary|ordinary|"
    r"preferred|pfd|perp|\d+(?:\.\d+)?\s?%|notes?\b|debentures?|units?\b|warrants?\b|rights?\b|"
    r"shares of beneficial|beneficial interest|subordinate|senior|series [a-z0-9]+\b|"
    r"limited partner|operating partnership|corporate units|etns?\b|exchange traded|"
    r"each representing|shares\b)",
    re.I,
)
CHECKED = {"x", "X", "ý", "þ", "☒", "■", "✓", "✔", "☑", "[x]", "[X]"}
UNCHECKED = {"o", "¨", "☐", "□", "q", "[ ]", "[]"}
PROVISIONS = ("a1", "a2", "a3", "a4", "b", "c")


# ---------------------------------------------------------------------- text helpers
ABBREV = {"corporation": "corp", "company": "co", "incorporated": "inc", "limited": "ltd"}


def words(name: str | None) -> list[str]:
    """Lower-case words of a name: entities decoded, '&' as 'and', parentheses and state
    REDACTED_FOR_GITHUB
    'kohls'), common legal words abbreviated as EDGAR does ('corporation' = 'corp')."""
    t = html.unescape(name or "").lower().replace("&", " and ")
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"/[a-z]{2,3}/?", " ", t)
    t = re.sub("[.'\u2019`]", "", t)
    return [ABBREV.get(w, w) for w in re.findall(r"[a-z0-9]+", t)]


def nkey(name: str | None) -> str:
    """An issuer name for matching: :func:`words`, leading 'the' and trailing corporate
    suffixes dropped."""
    toks = words(name)
    while toks and toks[-1] in SUFFIX:
        toks.pop()
    while toks and toks[0] == "the":
        toks.pop(0)
    return " ".join(toks)


def sorted_key(name: str | None) -> str:
    """Word order ignored ('PRICE T ROWE GROUP' = 'T. Rowe Price Group'): weaker."""
    return " ".join(sorted(nkey(name).split()))


def split_listing(family: str, name: str) -> tuple[str, str | None]:
    """(issuer part, class part) of a listed name. Company lists name the issuer only."""
    if not family.startswith("symdir"):
        return name, None
    if family == "symdir_nasdaqlisted" and " - " in name:
        a, b = name.split(" - ", 1)
        return a.strip(), b.strip()
    m = CLASS_START.search(" " + name)
    if m and m.start() > 0:
        return name[: m.start() - 1].strip(), name[m.start() :].strip()
    return name.strip(), None


def issuer_candidates(issuer: str) -> list[tuple[str, bool]]:
    """Name keys to try: the whole issuer part, then each prefix ending in a corporate suffix
    REDACTED_FOR_GITHUB
    The flag says whether the key is a prefix (weaker)."""
    out = [(nkey(issuer), False)]
    words = re.findall(r"[A-Za-z0-9&]+", issuer)
    for i, w in enumerate(words[:-1]):
        if w.lower() in CORP_END:
            k = nkey(" ".join(words[: i + 1]))
            if k and (k, True) not in out and k != out[0][0]:
                out.append((k, True))
    return [(k, p) for k, p in out if k]


def category(text: str | None, etf: bool | None = None) -> str:
    """The kind of security a class description or listed name describes."""
    t = lc.norm(text)
    if etf:
        return "FUND"
    if not t:
        return "UNSPECIFIED"
    if re.search(r"\betns?\b|exchange traded notes?|linked notes", t):
        return "ETN"
    if re.match(r"units?\b", t) or re.search(r"\bunits?,? each consisting", t):
        return "UNIT"
    if re.search(r"\bwarrants?\b", t):
        return "WARRANT"
    if re.search(r"\brights?\b", t) and "rights agreement" not in t:
        return "RIGHT"
    if re.search(r"preferred|preference|\bpfd\b|\bperp", t):
        return "PREFERRED"
    if re.search(
        r"\bnotes?\b|debentures?|\bbonds?\b|\bdue \w*\s?\d|senior|subordinated|"
        r"capital securities|trust certificates|\bctf\b",
        t,
    ):
        return "DEBT"
    if re.search(r"limited partner|common units|partnership units|\blp units", t):
        return "LP_UNIT"
    if re.search(r"depositary|depository|\bads\b|\badrs?\b", t):
        return "ADS"
    if re.search(r"\bunits?\b", t):
        return "UNIT"
    m = re.search(r"\bclass ([a-z])\b", t)
    if m and re.search(r"common|ordinary|shares", t):
        return f"CLASS_{m.group(1).upper()}_COMMON"
    if "common" in t:
        return "COMMON"
    if "ordinary" in t:
        return "ORDINARY"
    if "beneficial interest" in t:
        return "BENEFICIAL_INTEREST"
    return "OTHER"


GENERIC = {"COMMON", "ORDINARY", "ADS", "UNSPECIFIED"} | {
    f"CLASS_{c}_COMMON" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
}


def tokens(text: str | None) -> dict[str, set[str]]:
    """Distinguishing tokens of a class: series letters, coupons, maturity years."""
    t = lc.norm(text)
    series = set(re.findall(r"\bser(?:ies)?\.? ([a-z]{1,2}|\d{1,2})\b", t))
    coupon = {str(Decimal(c).normalize()) for c in re.findall(r"(\d{1,2}(?:\.\d{1,4})?)\s?%", t)}
    years = set(re.findall(r"\bdue (?:[a-z]+ \d{1,2}, )?(\d{4})\b", t)) | set(
        re.findall(r"\bdue (\d{4})\b", t)
    )
    return {"series": series, "coupon": coupon, "year": years}


def compatible(
    notice_text: str | None, listing_cat: str, listing_text: str | None, symbol: str
) -> bool:
    """Whether a listed security can be the class a notice removes."""
    ncat = category(notice_text)
    if listing_cat == "UNSPECIFIED":  # a company-list row: the plain class only
        return (
            ncat in GENERIC
            and not re.search(r"[-.$^ ]", symbol)
            and not (len(symbol) == 5 and symbol[-1] in "WURZ")  # Nasdaq warrant/unit/right
        )
    if ncat in GENERIC and listing_cat in GENERIC:
        return ncat == listing_cat or "UNSPECIFIED" in (ncat, listing_cat)
    if ncat != listing_cat:
        return False
    nt, lt = tokens(notice_text), tokens(listing_text)
    for k in ("series", "coupon", "year"):
        if nt[k] and not nt[k] <= lt[k]:
            return False
        if k == "series" and lt[k] and nt[k] != lt[k]:
            return False
    return bool(nt["series"] or nt["coupon"] or nt["year"]) or not (
        lt["series"] or lt["coupon"] or lt["year"]
    )


def checkbox_rule(textbody: str) -> tuple[str | None, list[str]]:
    """The rule provision marked in an issuer-filed Form 25, read from its checkbox glyphs
    (Wingdings 'o'/'x', '¨'/'ý', '[ ]'/'[x]', '☐'/'☒'). Exactly one provision must be checked
    and every other provision unchecked; otherwise None. Returns (rule, marks seen)."""
    plain = lc._plain(textbody)
    marks: dict[str, str] = {}
    for m in re.finditer(
        r"(\[\s*[xX]?\s*\]|\S)\s*(?:Pursuant to\s+)?17\s*CFR\s*240\.12d2-2\(([abc])\)(?:\((\d)\))?",
        plain,
    ):
        prov = m.group(2) + (m.group(3) or "")
        mark = re.sub(r"\s+", "", m.group(1))
        if prov in PROVISIONS and prov not in marks:
            marks[prov] = mark
    seen = [f"{p}:{marks.get(p, '?')}" for p in PROVISIONS]
    if set(marks) != set(PROVISIONS):
        return None, seen
    checked = [p for p, mk in marks.items() if mk in CHECKED]
    unchecked = [p for p, mk in marks.items() if mk in UNCHECKED]
    if len(checked) == 1 and len(unchecked) == len(PROVISIONS) - 1:
        return checked[0], seen
    return None, seen


def _ref(store: x4.Store, rel: str, locator: str) -> dict[str, Any]:
    return {
        "artifact": store.root.as_posix() + "/" + rel,
        "sha256": store.entries[rel]["sha256"],
        "locator": locator,
    }


def _cap(conf: str, limit: str) -> str:
    return conf if RANK[conf] <= RANK[limit] else limit


def _status(conf: str) -> str:
    return CONFIRMED if RANK[conf] >= RANK["MEDIUM"] else UNRESOLVED


# ---------------------------------------------------------------------- EDGAR names
@dataclass
class Edgar:
    by_key: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_sorted: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # cik -> name -> (first filing, last filing, index artifact of the first)
    names: dict[str, dict[str, tuple[str, str, str]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    reporters: set[str] = field(default_factory=set)


def edgar(store: x4.Store) -> Edgar:
    """Names each CIK filed under in 2015-2020 (first filing date and its index artifact)."""
    if "edgar" in store.cache:
        cached: Edgar = store.cache["edgar"]
        return cached
    e = Edgar()
    line = re.compile(r"(\S.*?)\s{2,}(.+?)\s{2,}(\d+)\s+(\d{4}-\d\d-\d\d)")
    for rel in sorted(k for k in store.entries if k.startswith("sec/full-index/")):
        for ln in gzip.decompress(store.path(rel).read_bytes()).decode("latin-1").splitlines():
            m = line.match(ln)
            if not m or not m.group(3).isdigit():
                continue
            form, name, cik, filed = m.group(1).strip(), m.group(2).strip(), m.group(3), m.group(4)
            cik = f"{int(cik):010d}"
            base = form.split("/")[0] if form.endswith("/A") and form != "20-F/A" else form
            if base not in ISSUER_FORMS and form not in ISSUER_FORMS:
                continue
            if base in PERIODIC:
                e.reporters.add(cik)
            prev = e.names[cik].get(name)
            if prev is None:
                e.names[cik][name] = (filed, filed, rel)
            else:
                first, last, src = prev
                e.names[cik][name] = (
                    min(first, filed),
                    max(last, filed),
                    rel if filed < first else src,
                )
            e.by_key[nkey(name)].add(cik)
            e.by_sorted[sorted_key(name)].add(cik)
    e.by_key.pop("", None)
    e.by_sorted.pop("", None)
    store.cache["edgar"] = e
    return e


# ---------------------------------------------------------------------- listings
@dataclass
class Obs:
    """One archived listing row, resolved (or not) to an issuer and a security."""

    ticker: str
    name: str
    venue: str
    family: str
    day: date
    source: str  # symdir | companylist
    ref: dict[str, Any]
    etf: bool | None
    issuer_text: str
    class_text: str | None
    cat: str
    entity: str | None = None
    security: str | None = None
    status: str = UNRESOLVED
    confidence: str = "LOW"
    method: str = ""
    candidates: list[str] = field(default_factory=list)
    note: str = ""


def _venue(family: str, row: dict[str, Any]) -> str:
    if family == "symdir_otherlisted":
        v = lc.OTHERLISTED_VENUE.get(row["exchange"] or "", row["exchange"] or "UNKNOWN")
    elif family == "symdir_nasdaqlisted":
        v = "NASDAQ"
    else:
        v = family.rsplit("_", 1)[-1].upper()
    return "NYSE_AMERICAN" if v == "AMEX" else v


def observations(store: x4.Store) -> list[Obs]:
    out = []
    for rel, e in sorted(store.entries.items()):
        if not (rel.startswith("archive/") and rel.endswith(".raw") and e.get("digest_verified")):
            continue
        family = rel.split("/")[1]
        day = lc._day(e["source"]["timestamp"])
        for i, row in enumerate(x4.parse_symbol_list(family, store.path(rel).read_bytes())):
            issuer, cls = split_listing(family, row["name"])
            source = "symdir" if family.startswith("symdir") else "companylist"
            cat = category(cls if source == "symdir" else None, row["etf"])
            if source == "symdir" and cls is None and not row["etf"]:
                cat = category(row["name"])
                if cat == "OTHER":
                    cat = "UNSPECIFIED"
            out.append(
                Obs(
                    ticker=row["symbol"],
                    name=row["name"],
                    venue=_venue(family, row),
                    family=family,
                    day=day,
                    source=source,
                    ref=_ref(store, rel, f"row {i} ({row['symbol']})"),
                    etf=row["etf"],
                    issuer_text=issuer,
                    class_text=cls,
                    cat=cat,
                )
            )
    return out


def fname(text: str | None) -> str:
    """A fund name compared on letters and digits, without trademark marks ('(R)', '(TM)')
    or a leading 'The'. Letters are never altered."""
    t = re.sub(r"\((?:r|tm|sm)\)|[®™℠]", "", html.unescape(text or ""), flags=re.I)
    return re.sub(r"^the", "", lc.alnum(t.strip()))


def security_label(cat: str, class_text: str | None) -> str:
    if cat in GENERIC:
        return cat
    return f"{cat}-" + hashlib.sha256(lc.norm(class_text).encode()).hexdigest()[:10]


def _fund_index(
    funds: dict[int, list[dict[str, str]]],
) -> tuple[dict[tuple[int, str], list[dict[str, str]]], dict[str, set[str]]]:
    by_ticker: dict[tuple[int, str], list[dict[str, str]]] = defaultdict(list)
    names: dict[str, set[str]] = defaultdict(set)
    for y, rows in funds.items():
        for r in rows:
            if r["ticker"]:
                by_ticker[(y, r["ticker"])].append(r)
            names[r["class_id"]].add(fname(r["series_name"]))
            names[r["class_id"]].add(fname(r["class_name"]))
    return by_ticker, names


def resolve_observations(obs: list[Obs], ed: Edgar, funds: dict[int, list[dict[str, str]]]) -> None:
    """Link each listing row to an issuer (and, for funds, an SEC share class)."""
    by_ticker, class_names = _fund_index(funds)
    for o in obs:
        # A fund share class: the SEC class of that ticker that year, with the listed name.
        hits = []
        listed = fname(o.name)
        for y in (o.day.year, o.day.year - 1, o.day.year + 1):
            for r in by_ticker.get((y, o.ticker), []):
                trust = fname(r["entity"])
                if listed in class_names[r["class_id"]] or (
                    listed.startswith(trust) and listed[len(trust) :] in class_names[r["class_id"]]
                ):
                    hits.append(r)
            if hits:
                break
        ids = {r["class_id"] for r in hits}
        if len(ids) == 1:
            r = hits[0]
            o.entity, o.security = f"CIK:{r['cik']}", f"SECCLASS:{r['class_id']}"
            o.cat = "FUND"
            o.confidence = "HIGH" if o.source == "symdir" else "MEDIUM"
            o.status, o.method = CONFIRMED, "SEC_CLASS_TICKER_AND_NAME"
            o.note = f"SEC series/class file {r['artifact']}: {r['series_name']}"
            continue
        if len(ids) > 1:
            o.candidates = sorted(f"SECCLASS:{i}" for i in ids)
            o.method, o.note = "SEC_CLASS_AMBIGUOUS", "several SEC classes share ticker and name"
            continue
        if o.cat == "FUND":
            o.method = "NO_SEC_CLASS"
            o.note = "an exchange-traded product with no SEC share class of this ticker and name"
            # ETNs and grantor trusts are not registered fund classes: try the issuer name.
        # An issuer: the listed issuer name equal to a name it filed under.
        found: set[str] = set()
        how, conf = "", "HIGH" if o.source == "symdir" else "MEDIUM"
        for key, prefix in issuer_candidates(o.issuer_text):
            found = set(ed.by_key.get(key, ()))
            if found:
                how = "EDGAR_NAME_PREFIX" if prefix else "EDGAR_NAME"
                if prefix:
                    conf = _cap(conf, "MEDIUM")
                break
        if not found:
            found = set(ed.by_sorted.get(sorted_key(o.issuer_text), ()))
            if found:
                how, conf = "EDGAR_NAME_WORD_ORDER", _cap(conf, "MEDIUM")
        if len(found) > 1:
            # Names active then: filings under the name from 31 days after to 120 days before
            # the capture (a predecessor and its successor can share a name).
            lo, hi = (
                (o.day - timedelta(days=120)).isoformat(),
                (o.day + timedelta(days=31)).isoformat(),
            )
            keys = {k for k, _ in issuer_candidates(o.issuer_text)}
            active = {
                c
                for c in found
                if any(
                    nkey(n) in keys and v[0] <= hi and v[1] >= lo for n, v in ed.names[c].items()
                )
            }
            if len(active) == 1:
                found, how = active, how + "+NAME_ACTIVE_THEN"
        if len(found) > 1:
            full = " ".join(words(o.issuer_text))
            exact = {c for c in found if any(" ".join(words(n)) == full for n in ed.names[c])}
            periodic = found & ed.reporters
            if len(exact) == 1:
                found, how = exact, how + "+EXACT_LEGAL_NAME"
            elif len(periodic) == 1:
                found, how, conf = periodic, how + "+PERIODIC_FILER", _cap(conf, "MEDIUM")
        if len(found) == 1:
            cik = next(iter(found))
            o.entity = f"CIK:{cik}"
            o.security = f"CIK:{cik}|{security_label(o.cat, o.class_text)}"
            o.status, o.confidence, o.method = _status(conf), conf, how
        elif found:
            o.candidates = sorted(f"CIK:{c}" for c in found)
            o.method = how + "_AMBIGUOUS"
            o.note = "the listed issuer name is the name of several SEC filers"
        elif not o.method:
            o.method = "NO_EDGAR_NAME"
            o.note = "no SEC filer used this issuer name in 2015-2020"
    # One class listed as "Common Stock" on one venue and "Class A Common Stock" on another
    # REDACTED_FOR_GITHUB
    # never listed together in one capture are unified to the most specific one.
    per_day: dict[str, dict[date, set[str]]] = defaultdict(lambda: defaultdict(set))
    for o in obs:
        if (
            o.status == CONFIRMED
            and o.source == "symdir"
            and o.entity
            and o.cat in GENERIC
            and o.cat not in ("UNSPECIFIED", "ADS")
        ):
            per_day[o.entity][o.day].add(o.cat)
    unify: dict[str, str] = {}
    for ent, days in per_day.items():
        labels = set().union(*days.values())
        specific = sorted(c for c in labels if c.startswith("CLASS_"))
        if len(labels) == 2 and len(specific) == 1 and all(len(v) == 1 for v in days.values()):
            unify[ent] = specific[0]
    for o in obs:
        if o.entity in unify and o.cat in ("COMMON", unify[o.entity]) and o.source == "symdir":
            o.cat = unify[o.entity]
            o.security = f"{o.entity}|{o.cat}"
            o.method += "+CLASS_LABEL_UNIFIED" if "UNIFIED" not in o.method else ""
    # A company-list row names the issuer only: its security is the class the symbol
    # directories show for that issuer and ticker, when there is exactly one.
    sym: dict[tuple[str | None, str], dict[str | None, str]] = defaultdict(dict)
    for o in obs:
        if o.source == "symdir" and o.status == CONFIRMED:
            sym[(o.entity, o.ticker)][o.security] = o.cat
    for o in obs:
        if o.source == "companylist" and o.status == CONFIRMED and o.cat == "UNSPECIFIED":
            known = sym.get((o.entity, o.ticker), {})
            if len(known) == 1:
                o.security, o.cat = next(iter(known.items()))
                o.method += "+CLASS_FROM_SYMBOL_DIRECTORY"
            else:
                o.security = f"{o.entity}|UNSPECIFIED"


@dataclass
class Index:
    by_ticker: dict[str, list[Obs]]
    days: dict[str, list[date]]
    span: tuple[date, date]

    @classmethod
    def of(cls, obs: list[Obs]) -> Index:
        by: dict[str, list[Obs]] = defaultdict(list)
        for o in obs:
            by[o.ticker].append(o)
        for v in by.values():
            v.sort(key=lambda o: (o.day, o.family))
        span = (min(o.day for o in obs), max(o.day for o in obs)) if obs else (date.min, date.min)
        return cls(by, {t: [o.day for o in v] for t, v in by.items()}, span)


def holder(ix: Index, ticker: str, when: date) -> dict[str, Any]:
    """Who held ``ticker`` on ``when``: the security listed under it in the nearest capture
    on or before and on or after that date, when both are the same issuer."""
    rows = ix.by_ticker.get(ticker, [])
    days = ix.days.get(ticker, [])
    i = bisect_right(days, when)
    before = [o for o in rows[:i] if o.day == rows[i - 1].day] if i else []
    j = bisect_left(days, when)
    after = [o for o in rows[j:] if o.day == rows[j].day] if j < len(rows) else []
    ents_b = {o.entity for o in before if o.status == CONFIRMED}
    ents_a = {o.entity for o in after if o.status == CONFIRMED}
    ev = [o.ref for o in before[:1] + after[:1]]
    base = {"ticker": ticker, "on": when.isoformat(), "evidence": ev}
    if len(ents_b) > 1 or len(ents_a) > 1:
        return {
            **base,
            "status": CONFLICTING,
            "confidence": "LOW",
            "entity": None,
            "security": None,
            "explanation": f"{ticker} is listed for two issuers in one capture",
        }
    if before and after and ents_b and ents_b == ents_a:
        ent = next(iter(ents_b))
        secs = [o.security for o in before + after if o.status == CONFIRMED and o.security]
        sec: str | None = next((s for s in secs if not s.endswith("|UNSPECIFIED")), secs[0])
        conf = (
            "HIGH"
            if {"symdir"} <= {o.source for o in before} | {o.source for o in after}
            and all(o.source == "symdir" for o in before[:1] + after[:1])
            else "MEDIUM"
        )
        if (after[0].day - before[0].day).days > STALE_DAYS:
            conf = _cap(conf, "MEDIUM")
        conf = min(
            (conf, *(o.confidence for o in before + after if o.status == CONFIRMED)),
            key=lambda c: RANK[c],
        )
        return {
            **base,
            "status": _status(conf),
            "confidence": conf,
            "entity": ent,
            "security": sec,
            "explanation": f"listed as {ticker} for {ent} on {before[0].day} and {after[0].day}",
        }
    if before and after and ents_b and ents_a:
        return {
            **base,
            "status": UNRESOLVED,
            "confidence": "LOW",
            "entity": None,
            "security": None,
            "candidates": sorted(e for e in ents_b | ents_a if e),
            "explanation": f"{ticker} changed issuer between {before[0].day} and {after[0].day}",
        }
    cand = sorted(e for e in ents_b | ents_a if e)
    side = "before" if before else "after" if after else "on either side"
    if before and after and not (ents_b and ents_a):
        side = "with an issuer on one side only (the other row is unresolved)"
        if not cand:
            side = "but neither row is resolved to an issuer"
    one = before or after
    inside = ix.span[0] <= when <= ix.span[1]  # never projected past the evidence
    if one and inside and len(cand) == 1 and len(ents_b | ents_a) == 1:
        near = [o for o in rows if abs((o.day - when).days) <= STALE_DAYS and o.status == CONFIRMED]
        rivals = {o.entity for o in near} - {cand[0]}
        names = {lc.alnum(o.name) for o in near}
        unknown = [
            o
            for o in rows
            if abs((o.day - when).days) <= STALE_DAYS
            and o.status != CONFIRMED
            and lc.alnum(o.name) not in names
        ]
        if abs((one[0].day - when).days) <= STALE_DAYS and not rivals and not unknown:
            known = [o.security for o in near if o.security]
            sec = next(
                (k for k in known if not k.endswith("|UNSPECIFIED")),
                known[0] if known else None,
            )
            return {
                **base,
                "status": CONFIRMED,
                "confidence": "MEDIUM",
                "entity": cand[0],
                "security": sec,
                "method": "ONE_SIDED_NO_RIVAL",
                "explanation": f"{ticker} is captured only {side} of {when} "
                f"({one[0].day}), for {cand[0]}, and for no other issuer within "
                f"{STALE_DAYS} days",
            }
    return {
        **base,
        "status": UNRESOLVED,
        "confidence": "LOW",
        "entity": None,
        "security": None,
        "candidates": cand,
        "explanation": f"{ticker} is captured only {side} of {when}",
    }


# ---------------------------------------------------------------------- notices
Listed = dict[tuple[str, date], set[tuple[str, str, str | None, str | None]]]


def _accession(ref: dict[str, Any]) -> str | None:
    m = ACCESSION.search(ref.get("locator", ""))
    return m.group(1) if m else None


def notices(
    store: x4.Store, ledger: dict[str, Any], obs: list[Obs], ix: Index
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One record per notice target (accession x security), duplicates collapsed."""
    docs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in lc.removal_documents(store):
        m = ACCESSION.search(d["path"])
        docs[m.group(1) if m else d["path"]].append(d)
    secs = {s["security_key"]: s for s in ledger["securities"]}
    removals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in ledger["events"]:
        if e["event_type"] == "VENUE_REMOVAL":
            for r in e["evidence"]:
                acc = _accession(r)
                if acc and all(x is not e for x in removals[acc]):
                    removals[acc].append(e)
    terminal = defaultdict(list)
    for e in ledger["events"]:
        if e["event_type"] in lc.TERMINAL:
            terminal[e["security_key"]].append(e)
    by_entity_venue: dict[tuple[str, str], list[Obs]] = defaultdict(list)
    by_candidate: dict[tuple[str, str], list[Obs]] = defaultdict(list)
    for o in obs:
        if o.entity:
            by_entity_venue[(o.entity, o.venue)].append(o)
        for c in o.candidates:
            by_candidate[(c, o.venue)].append(o)
    captures: dict[str, list[date]] = defaultdict(list)
    listed: Listed = defaultdict(set)
    for o in obs:
        if o.day not in captures[o.family]:
            captures[o.family].append(o.day)
        listed[(o.family, o.day)].add((o.ticker, o.venue, o.entity, lc.norm(o.class_text) or None))
    for v in captures.values():
        v.sort()
    fam_venues: dict[str, set[str]] = defaultdict(set)
    for o in obs:
        fam_venues[o.family].add(o.venue)
    out: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for acc in sorted(docs):
        copies = docs[acc]
        main = copies[0]
        parsed = x4.parse_notice(main.get("text") or "")
        nse = main["form"].startswith("25-NSE")
        issuer_cik = (
            f"{int(parsed['issuer_cik']):010d}"
            if nse and (parsed.get("issuer_cik") or "").strip().isdigit()
            else (None if nse else main["cik"])
        )
        copy_ciks = sorted({d["cik"] for d in copies})
        events = sorted(removals.get(acc, []), key=lambda e: e["code"])
        own = [
            e
            for e in events
            if issuer_cik and secs[e["security_key"]]["entity_key"] == f"CIK:{issuer_cik}"
        ]
        dups = [e for e in events if e not in own]
        stats["documents"] += 1
        stats["index_copies"] += len(copies)
        stats["duplicate_events"] += len(dups)
        x43_rule: str | None = None
        marks: list[str] = []
        if not nse:
            x43_rule, marks = checkbox_rule(main.get("text") or "")
        ref = lc.evidence_ref(store, main["artifact"], f"line {main['line']} ({main['path']})")
        ref = {**ref, "artifact": store.root.as_posix() + "/" + ref["artifact"]}
        if issuer_cik is None:
            stats["missing_issuer_cik"] += 1
        for e in own or ([] if issuer_cik else events[:1]):
            sec = secs[e["security_key"]]
            rec = _resolve_notice(
                e,
                sec,
                issuer_cik,
                main,
                dups,
                by_entity_venue,
                by_candidate,
                captures,
                listed,
                fam_venues,
                ref,
                terminal,
            )
            rec["rule_x43"] = None
            if e["reason"].startswith("UNPARSED_RULE"):
                rec["rule_x43"] = {
                    "rule": x43_rule,
                    "reason": lc.RULES[x43_rule][0] if x43_rule else None,
                    "terminal": lc.RULES[x43_rule][1] if x43_rule else None,
                    "status": CONFIRMED if x43_rule else UNRESOLVED,
                    "confidence": "HIGH" if x43_rule else "LOW",
                    "method": "CHECKBOX_GLYPHS",
                    "marks": marks,
                    "explanation": (
                        f"exactly one provision is checked: 12d2-2({x43_rule[0]})"
                        + (f"({x43_rule[1:]})" if x43_rule[1:] else "")
                        if x43_rule
                        else "the provision marks could not be read unambiguously"
                    ),
                }
            rec["accession"] = acc
            rec["copies"] = copy_ciks
            out.append(rec)
    # Amendments: a /A notice for the same security and venue filed within 60 days after.
    orig: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for r in out:
        if not r["form"].endswith("/A"):
            orig[(r["security_key"], r["venue"])].append(r)
    for r in out:
        r["amends"] = None
        if r["form"].endswith("/A"):
            prior = [
                p
                for p in orig.get((r["security_key"], r["venue"]), [])
                if p["filed"] <= r["filed"]
                and (date.fromisoformat(r["filed"]) - date.fromisoformat(p["filed"])).days <= 60
            ]
            if prior:
                r["amends"] = max(prior, key=lambda p: p["filed"])["accession"]
    return out, dict(stats)


def _resolve_notice(
    e: dict[str, Any],
    sec: dict[str, Any],
    issuer_cik: str | None,
    main: dict[str, Any],
    dups: list[dict[str, Any]],
    by_entity_venue: dict[tuple[str, str], list[Obs]],
    by_candidate: dict[tuple[str, str], list[Obs]],
    captures: dict[str, list[date]],
    listed: Listed,
    fam_venues: dict[str, set[str]],
    ref: dict[str, Any],
    terminal: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    filed = date.fromisoformat(e["filing_date"])
    venue = e["venue"]
    cls = sec["class_description"]
    is_fund = sec["security_key"].startswith("SECCLASS:")
    ncat = "FUND" if is_fund else category(cls)
    rec: dict[str, Any] = {
        "form": main["form"],
        "filed": e["filing_date"],
        "issuer": f"CIK:{issuer_cik}" if issuer_cik else None,
        "issuer_name": main["company"] if issuer_cik == main["cik"] else None,
        "venue": venue,
        "reason": e["reason"],
        "reason_rule": next((k for k, v in lc.RULES.items() if v[0] == e["reason"]), None),
        "security_key": sec["security_key"],
        "class_description": cls,
        "class_category": ncat,
        "ledger_event": e["code"],
        "duplicate_events": sorted(d["code"] for d in dups),
        "terminal_events": sorted(t["code"] for t in terminal.get(sec["security_key"], [])),
        "label_note": None,
        "ticker": None,
        "status": UNRESOLVED,
        "confidence": "LOW",
        "method": None,
        "observed": None,
        "candidates": [],
        "conflicts": [],
        "evidence": [ref],
        "explanation": "",
    }
    if sec["class_label"] in GENERIC and ncat not in GENERIC and ncat != "FUND":
        rec["label_note"] = (
            f"X4.2 labelled this class {sec['class_label']}; its description is a "
            f"{ncat.lower().replace('_', ' ')} class, so the X4.2 key joins it with the "
            "issuer's plain equity"
        )
    if issuer_cik is None:
        rec["explanation"] = "the notice names no issuer CIK: not linked (missing CIK)"
        rec["method"] = "MISSING_CIK"
        return rec
    if venue is None:
        rec["explanation"] = "the notice's exchange could not be read: not linked"
        rec["method"] = "NO_VENUE"
        return rec
    ent = f"CIK:{issuer_cik}"
    pool = by_entity_venue.get((ent, venue), [])
    weak = by_candidate.get((ent, venue), [])
    match: list[tuple[Obs, bool]] = []
    for o, ambiguous in [(o, False) for o in pool] + [(o, True) for o in weak]:
        if o.day > filed + timedelta(days=3):
            continue
        if is_fund:
            if o.security == sec["security_key"]:
                match.append((o, ambiguous))
            continue
        if sec["class_label"].startswith("SERIES-"):
            if lc.alnum(o.name) == lc.alnum(cls) or lc.alnum(o.class_text) == lc.alnum(cls):
                match.append((o, ambiguous))
            continue
        if compatible(cls, o.cat, o.class_text, o.ticker):
            match.append((o, ambiguous))
    cover = sorted(f for f, ds in captures.items() if ds[0] <= filed and venue in fam_venues[f])
    if not match:
        rec["method"] = "NO_LISTING_MATCH" if cover else "NO_CAPTURE_BEFORE_FILING"
        rec["explanation"] = (
            f"no listing row of {ent} on {venue} with a compatible class "
            f"({ncat}) was captured before {filed}"
            if cover
            else f"no captured listing of {venue} precedes the filing ({filed})"
        )
        return rec
    latest = max(o.day for o, _ in match)
    at_latest = {o.ticker for o, _ in match if o.day == latest}
    by_t: dict[str, list[tuple[Obs, bool]]] = defaultdict(list)
    for o, amb in match:
        by_t[o.ticker].append((o, amb))
    if len(at_latest) > 1:
        rec["method"] = "AMBIGUOUS_CLASS"
        rec["candidates"] = sorted(at_latest)
        rec["explanation"] = (
            f"several listed securities of {ent} on {venue} are compatible with "
            f"{cls!r}: {sorted(at_latest)}"
        )
        return rec
    ticker = next(iter(at_latest))
    mine = sorted(by_t[ticker], key=lambda p: (p[0].day, p[0].family))
    o_last = mine[-1][0]
    conf = "HIGH" if o_last.source == "symdir" else "MEDIUM"
    conf = min(conf, o_last.confidence, key=lambda c: RANK[c])
    if any(amb for _, amb in mine):
        conf = _cap(conf, "MEDIUM")
    if (filed - o_last.day).days > STALE_DAYS:
        conf = _cap(conf, "MEDIUM")
    # Corroboration: the next capture of that directory after the filing.
    nxt = [d for d in captures[o_last.family] if d > filed]
    after = "NO_LATER_CAPTURE"
    if nxt:
        rows = listed[(o_last.family, nxt[0])]
        mine_cls = lc.norm(o_last.class_text) or None
        here = [c for t, v, en, c in rows if t == ticker and en == ent and v == venue]
        if here and (nxt[0] - filed).days <= PENDING_DAYS:
            after = "PENDING"
        elif here and (mine_cls is None or any(c is None for c in here)):
            after = "LISTED_CLASS_UNKNOWN"  # a company list cannot tell a successor class
        elif here and mine_cls in here:
            after = "STILL_LISTED"
        elif here:
            after = "SUCCESSOR_CLASS_LISTED"
        elif any(t == ticker and en == ent for t, _, en, _ in rows):
            after = "LISTED_ON_OTHER_VENUE"
        else:
            after = "ABSENT"
    method = "LISTED_NAME_CLASS_VENUE" + ("_COMPANY_LIST" if o_last.source != "symdir" else "")
    rec.update(
        ticker=ticker,
        method=method,
        observed={
            "first": mine[0][0].day.isoformat(),
            "last": o_last.day.isoformat(),
            "after_filing": after,
            "next_capture": nxt[0].isoformat() if nxt else None,
        },
        evidence=[ref] + [o.ref for o, _ in mine[-2:]],
    )
    if other := sorted({t for t in by_t if t != ticker}):
        rec["earlier_tickers"] = other
    if after in ("STILL_LISTED", "LISTED_CLASS_UNKNOWN") and e["reason"] == lc.RULES["a3"][0]:
        after = "SUCCESSOR_UNDER_SAME_TICKER"
        rec["observed"]["after_filing"] = after
    if after == "STILL_LISTED":
        rec.update(status=CONFLICTING, confidence="LOW")
        rec["conflicts"] = [
            f"{ticker} is still listed on {venue} for {ent} on {nxt[0]}, "
            f"{(nxt[0] - filed).days} days after the removal was filed"
        ]
        rec["explanation"] = "the listing outlives the removal notice"
        return rec
    if after in ("ABSENT", "LISTED_ON_OTHER_VENUE"):
        corroboration = f"not listed on {venue} in the {o_last.family} capture of {nxt[0]}" + (
            " (listed on another venue)" if after == "LISTED_ON_OTHER_VENUE" else ""
        )
    elif after == "SUCCESSOR_CLASS_LISTED":
        corroboration = (
            f"the {o_last.family} capture of {nxt[0]} lists {ticker} for another class of "
            f"{ent}: a successor class under the same ticker"
        )
        rec["successor"] = {
            "ticker": ticker,
            "on": nxt[0].isoformat(),
            "detail": "a different class of the issuer trades under the ticker after the "
            "removal: a separate security",
        }
    elif after == "SUCCESSOR_UNDER_SAME_TICKER":
        conf = _cap(conf, "MEDIUM")
        corroboration = (
            f"{ticker} is still listed for {ent} in the {o_last.family} capture of {nxt[0]}: "
            "the class was converted (12d2-2(a)(3)), so a successor security plausibly trades "
            "under the same ticker; it is not identified here"
        )
        rec["successor"] = {
            "ticker": ticker,
            "on": nxt[0].isoformat(),
            "detail": "after a conversion the ticker is still listed: the successor is a "
            "separate security, not yet identified",
        }
    elif after == "LISTED_CLASS_UNKNOWN":
        conf = _cap(conf, "LOW")
        corroboration = (
            f"{ticker} is still listed for {ent} in the {o_last.family} capture of {nxt[0]}, "
            "which does not show the class"
        )
    else:
        conf = _cap(conf, "MEDIUM" if o_last.source == "symdir" else "LOW")
        corroboration = (
            "no later capture exists"
            if after == "NO_LATER_CAPTURE"
            else f"the next capture ({nxt[0]}) is within {PENDING_DAYS} days of the filing"
        )
    rec.update(status=_status(conf), confidence=conf)
    rec["explanation"] = (
        f"{ent} listed {ticker} on {venue} ({o_last.name!r}, {o_last.family} {o_last.day}), "
        f"class compatible with {cls!r}; {corroboration}."
        + ("" if rec["status"] == CONFIRMED else " Too weak to confirm: candidate only.")
    )
    if rec["status"] != CONFIRMED:
        rec["candidates"] = [ticker]
        rec["ticker"] = None
    return rec


# ---------------------------------------------------------------------- ticker edges
def ticker_edges(obs: list[Obs]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Security <-> (ticker, venue) relationships, and unresolved listings."""
    groups: dict[tuple[str, str, str], list[Obs]] = defaultdict(list)
    loose: dict[tuple[str, str, str], list[Obs]] = defaultdict(list)
    for o in obs:
        if o.status == CONFIRMED and o.security:
            groups[(o.security, o.ticker, o.venue)].append(o)
        else:
            loose[(o.ticker, o.venue, o.name)].append(o)
    edges = []
    for (sec, t, v), rows in sorted(groups.items()):
        rows.sort(key=lambda o: (o.day, o.family))
        conf = max((o.confidence for o in rows), key=lambda c: RANK[c])
        edges.append(
            {
                "security_key": sec,
                "entity_key": rows[0].entity,
                "ticker": t,
                "venue": v,
                "first_observed": rows[0].day.isoformat(),
                "last_observed": rows[-1].day.isoformat(),
                "captures": len(rows),
                "listed_names": sorted({o.name for o in rows}),
                "category": rows[-1].cat,
                "status": CONFIRMED,
                "confidence": conf,
                "method": sorted({o.method for o in rows}),
                "evidence": [o.ref for o in rows],
                "conflicts": [],
                "explanation": f"{t} on {v} is listed for {rows[0].entity} in {len(rows)} captures "
                f"({rows[0].day}..{rows[-1].day}); the issuer is identified by the listed name, "
                "not by the ticker. The dates are observations, not the listing's start or end.",
            }
        )
    unresolved = []
    for (t, v, name), rows in sorted(loose.items()):
        rows.sort(key=lambda o: (o.day, o.family))
        unresolved.append(
            {
                "ticker": t,
                "venue": v,
                "listed_name": name,
                "first_observed": rows[0].day.isoformat(),
                "last_observed": rows[-1].day.isoformat(),
                "captures": len(rows),
                "status": UNRESOLVED,
                "confidence": "LOW",
                "method": rows[-1].method,
                "candidates": rows[-1].candidates,
                "evidence": [o.ref for o in rows],
                "explanation": rows[-1].note or "no issuer evidence",
            }
        )
    return edges, unresolved


def ticker_reuse(
    edges: list[dict[str, Any]], funds: dict[int, list[dict[str, str]]], x4s: x4.Store
) -> list[dict[str, Any]]:
    """Tickers held by two or more issuers (or SEC share classes) over time."""
    holders: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    def add(t: str, key: str, first: str, last: str, source: str, ev: dict[str, Any]) -> None:
        h = holders[t].setdefault(
            key,
            {
                "holder": key,
                "first": first,
                "last": last,
                "listed_first": None,
                "listed_last": None,
                "sources": set(),
                "evidence": [],
            },
        )
        h["first"], h["last"] = min(h["first"], first), max(h["last"], last)
        if source == "listings":
            h["listed_first"] = min(h["listed_first"] or first, first)
            h["listed_last"] = max(h["listed_last"] or last, last)
        h["sources"].add(source)
        if len(h["evidence"]) < 3:
            h["evidence"].append(ev)

    for e in edges:
        key = e["security_key"] if e["security_key"].startswith("SECCLASS:") else e["entity_key"]
        add(e["ticker"], key, e["first_observed"], e["last_observed"], "listings", e["evidence"][0])
    for y, rows in sorted(funds.items()):
        for r in rows:
            if r["ticker"]:
                add(
                    r["ticker"],
                    f"SECCLASS:{r['class_id']}",
                    f"{y}-01-01",
                    f"{y}-12-31",
                    "sec_fund_file",
                    _ref(x4s, r["artifact"], f"class {r['class_id']}"),
                )
    trust = {
        f"SECCLASS:{r['class_id']}": f"CIK:{r['cik']}" for rows in funds.values() for r in rows
    }
    out = []
    for t, hs in sorted(holders.items()):
        if not any("listings" in h["sources"] for h in hs.values()):
            continue  # never listed in a capture: not an exchange ticker in this evidence
        for key in [k for k in hs if k.startswith("SECCLASS:")]:
            c = trust.get(key)
            if c in hs and key in hs:  # the trust listed under the class's own ticker
                h, other = hs[key], hs.pop(c)
                h["first"], h["last"] = (
                    min(h["first"], other["first"]),
                    max(h["last"], other["last"]),
                )
                for k, f in (("listed_first", min), ("listed_last", max)):
                    vals = [v for v in (h[k], other[k]) if v]
                    h[k] = f(vals) if vals else None
                h["sources"] |= other["sources"]
        if len(hs) < 2:
            continue
        # One fund class's SEC file years and its listings are one holder already; a fund
        # (SECCLASS) listed as such is not a second holder of its own trust (CIK).
        rows = sorted(hs.values(), key=lambda h: (h["first"], h["holder"]))
        # Two holders conflict only when *listings* show both under the ticker at once (the
        # SEC fund files are yearly, so a mid-year fund reorganisation overlaps by a year).
        overlap = [
            (a["holder"], b["holder"])
            for i, a in enumerate(rows)
            for b in rows[i + 1 :]
            if a["listed_first"]
            and b["listed_first"]
            and a["listed_first"] <= b["listed_last"]
            and b["listed_first"] <= a["listed_last"]
        ]
        out.append(
            {
                "ticker": t,
                "holders": [{**h, "sources": sorted(h["sources"])} for h in rows],
                "status": CONFLICTING if overlap else CONFIRMED,
                "conflicts": [
                    f"{a} and {b} both listed as {t} in overlapping periods" for a, b in overlap
                ],
                "explanation": f"{t} was held by {len(rows)} different issuers or share classes; "
                "each is a separate security and no price history passes between them.",
            }
        )
    return out


# ---------------------------------------------------------------------- provider identities
def _sweeps(store: x4.Store) -> dict[date, dict[str, dict[str, tuple[float, int]]]]:
    """formation -> ticker -> session -> (raw close, volume), from the X4.1 sweeps."""
    out: dict[date, dict[str, dict[str, tuple[float, int]]]] = {}
    for f in x4.FORMATIONS:
        by: dict[str, dict[str, tuple[float, int]]] = defaultdict(dict)
        for rel in sorted(k for k in store.entries if k.startswith(f"provider/sweeps/{f}/")):
            if "/history/" in rel:
                continue
            for req in x4.read_json(store.path(rel))["requests"]:
                for page in req["pages"]:
                    for sym, rows in ((page.get("body") or {}).get("bars") or {}).items():
                        for r in rows:
                            by[sym][r["t"][:10]] = (round(float(r["c"]), 6), int(r["v"]))
        out[f] = by
    return out


def fingerprint_tickers(
    bars: dict[str, tuple[float, int]],
    sweep: dict[str, dict[str, tuple[float, int]]],
    index: dict[tuple[str, float, int], set[str]],
) -> list[str]:
    """Tickers whose sweep bars equal ``bars`` on every common session (at least
    :data:`MIN_FINGERPRINT` of them)."""
    cands: set[str] = set()
    for d, v in bars.items():
        cands |= index.get((d, *v), set())
        if len(cands) > 50:
            break
    out = []
    for t in sorted(cands):
        common = set(sweep[t]) & set(bars)
        if len(common) >= MIN_FINGERPRINT and all(sweep[t][d] == bars[d] for d in common):
            out.append(t)
    return out


def provider_links(
    x4s: x4.Store,
    x43s: x43.Store,
    ix: Index,
    ledger: dict[str, Any],
    edges: list[dict[str, Any]],
    notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    notice_by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in notes:
        notice_by_sec[n["security_key"]].append(n)
    ids = x4.read_json(x43s.path("db/provider_identities.json.gz"))
    latest: dict[str, dict[str, Any]] = {}
    revisions: Counter[str] = Counter()
    for r in ids:
        latest[r["asset_id"]] = r  # ordered by created_at, revision
        revisions[r["asset_id"]] += 1
    bars: dict[str, list[list[Any]]] = {}
    for rel in sorted(k for k in x43s.entries if k.startswith("db/provider_bars/")):
        bars.update(x4.read_json(x43s.path(rel)))
    sweeps = _sweeps(x4s)
    windows = {}
    sweep_index: dict[date, dict[tuple[str, float, int], set[str]]] = {}
    for f, by in sweeps.items():
        idx: dict[tuple[str, float, int], set[str]] = defaultdict(set)
        all_days: set[str] = set()
        for t, rows in by.items():
            for d, v in rows.items():
                idx[(d, *v)].add(t)
                all_days.add(d)
        sweep_index[f] = idx
        windows[f] = (min(all_days), max(all_days)) if all_days else ("", "")
    events_by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in ledger["events"]:
        events_by_sec[e["security_key"]].append(e)
    listed_by_entity: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for e in edges:
        listed_by_entity[e["entity_key"]].append((e["first_observed"], e["last_observed"]))
    ref_ids = _ref(x43s, "db/provider_identities.json.gz", "")
    out = []
    for asset in sorted(latest):
        r = latest[asset]
        det = r["details"] or {}
        series = bars.get(asset, [])
        raw = {
            d: (round(float(c), 6), int(Decimal(v)))
            for d, c, _, v in series
            if c is not None and v is not None
        }
        rec: dict[str, Any] = {
            "asset_id": asset,
            "request": {
                "ticker": det.get("request_symbol") or r["symbol"],
                "asof": det.get("asof"),
                "label": r["symbol"],
            },
            "revisions": revisions[asset],
            "first_bar": r["first_bar"],
            "last_bar": r["last_bar"],
            "recorded_delisting": r["delisted_on"],
            "research_type": r["security_type"],
            "research_type_basis": r["type_basis"],
            "formations": [],
            "entity_key": None,
            "security_key": None,
            "status": UNRESOLVED,
            "confidence": "LOW",
            "method": "PRICE_FINGERPRINT_THEN_LISTINGS",
            "findings": [],
            "conflicts": [],
            "evidence": [{**ref_ids, "locator": f"asset {asset}"}],
            "explanation": "",
        }
        for f in x4.FORMATIONS:
            lo, hi = windows[f]
            mine = {d: v for d, v in raw.items() if lo <= d <= hi}
            if not mine:
                continue
            ts = fingerprint_tickers(mine, sweeps[f], sweep_index[f])
            h = [holder(ix, t, f) for t in ts]
            rec["formations"].append({"formation": f.isoformat(), "tickers": ts, "holders": h})
        held = [h for fm in rec["formations"] for h in fm["holders"]]
        ents = {h["entity"] for h in held if h["status"] == CONFIRMED}
        if len(ents) == 1:
            confs = [h["confidence"] for h in held if h["status"] == CONFIRMED]
            conf = min(confs, key=lambda c: RANK[c])
            sec = next(h["security"] for h in held if h["status"] == CONFIRMED)
            rec.update(
                entity_key=next(iter(ents)), security_key=sec, status=_status(conf), confidence=conf
            )
            unheld = [h for h in held if h["status"] != CONFIRMED]
            if unheld:
                rec["findings"].append(
                    {
                        "kind": "PARTIAL_HOLDER_EVIDENCE",
                        "detail": [h["explanation"] for h in unheld],
                    }
                )
        elif len(ents) > 1:
            rec.update(status=CONFLICTING, confidence="LOW")
            rec["conflicts"].append(
                f"the bar series is listed for {sorted(ents)} at different formations: "
                "it joins two issuers' histories"
            )
        else:
            no_fp = not any(fm["tickers"] for fm in rec["formations"])
            rec["findings"].append(
                {
                    "kind": "NO_FINGERPRINT" if no_fp else "HOLDER_UNRESOLVED",
                    "detail": "the bars equal no ticker's formation sweep"
                    if no_fp
                    else [h["explanation"] for h in held],
                }
            )
        _lifecycle_checks(rec, series, events_by_sec, listed_by_entity, notice_by_sec, ix)
        rec["explanation"] = _provider_explanation(rec)
        out.append(rec)
    _continuations(out, bars)
    return out


def _lifecycle_checks(
    rec: dict[str, Any],
    series: list[list[Any]],
    events_by_sec: dict[str, list[dict[str, Any]]],
    listed_by_entity: dict[str, list[tuple[str, str]]],
    notice_by_sec: dict[str, list[dict[str, Any]]],
    ix: Index,
) -> None:
    adj = [(d, float(s)) for d, _, s, _ in series if s is not None and float(s) > 0]
    for (d0, c0), (d1, c1) in itertools.pairwise(adj):
        if abs(math.log(c1 / c0)) >= JUMP:
            rec["findings"].append(
                {
                    "kind": "PRICE_JUMP",
                    "from": [d0, c0],
                    "to": [d1, c1],
                    "detail": "split-adjusted close moves 3x or more in one session",
                }
            )
    for (d0, _), (d1, _) in itertools.pairwise(adj):
        if (date.fromisoformat(d1) - date.fromisoformat(d0)).days > 20:
            rec["findings"].append({"kind": "BAR_GAP", "from": d0, "to": d1})
    sec, ent = rec["security_key"], rec["entity_key"]
    if not sec or not rec["last_bar"]:
        return
    last = date.fromisoformat(rec["last_bar"])
    tickers = {t for f in rec["formations"] for t in f["tickers"]}
    for e in events_by_sec.get(sec, []):
        if e["event_type"] in lc.TERMINAL and e["resolution_status"] == CONFIRMED:
            mine = [
                n
                for n in notice_by_sec.get(sec, [])
                if lc.RULES.get(n["reason_rule"] or "", ("", False))[1]
            ]
            linked = [n for n in mine if n["status"] == CONFIRMED and n["ticker"] in tickers]
            if not linked:
                rec["findings"].append(
                    {
                        "kind": "TERMINAL_EVENT_NOT_LINKED",
                        "terminal_event": e["code"],
                        "notice_classes": sorted({n["class_description"] or "" for n in mine}),
                        "detail": "the ledger ends a security with this key, but no notice ending "
                        "it is linked to this identity's ticker (X4.2 labels can join "
                        "classes, e.g. purchase rights or a preferred ADS under COMMON or ADS): "
                        "not applied",
                    }
                )
                continue
            limit = e["not_after"] or (
                (date.fromisoformat(e["filing_date"]) + timedelta(days=PENDING_DAYS)).isoformat()
                if e["filing_date"]
                else None
            )
            if limit and rec["last_bar"] > limit:
                after = [d for d, *_ in series if d > limit]
                nxt = (
                    holder(ix, sorted(tickers)[0], date.fromisoformat(after[0]))
                    if after and tickers
                    else None
                )
                rec["findings"].append(
                    {
                        "kind": "BARS_AFTER_SECURITY_END",
                        "terminal_event": e["code"],
                        "end_no_later_than": limit,
                        "bars_after": len(after),
                        "first_bar_after": after[0] if after else None,
                        "successor_notice": next(
                            (n["successor"] for n in linked if n.get("successor")), None
                        ),
                        "later_holder": nxt["entity"] if nxt else None,
                        "later_holder_status": nxt["status"] if nxt else None,
                        "detail": "the security ended, yet the series continues: the later bars "
                        "belong to another security (history contamination)"
                        + (
                            f"; the ticker is then held by {nxt['entity']}"
                            + (" (a successor issuer)" if nxt["entity"] != ent else "")
                            if nxt and nxt["entity"]
                            else ""
                        ),
                    }
                )
                rec["status"], rec["confidence"] = CONFLICTING, "LOW"
                rec["conflicts"].append(f"bars after {e['code']} ended the security")
            elif rec["recorded_delisting"]:
                rec["findings"].append(
                    {"kind": "END_CONSISTENT_WITH_SEC", "terminal_event": e["code"]}
                )
    if rec["recorded_delisting"] and ent:
        later = [b for a, b in listed_by_entity.get(ent, []) if b > rec["last_bar"]]
        if later and (date.fromisoformat(max(later)) - last).days > 10:
            transfers = [
                e
                for e in events_by_sec.get(sec, [])
                if e["event_type"] == "EXCHANGE_TRANSFER"
                and (e["not_before"] or "") <= rec["last_bar"] <= (e["not_after"] or "9999")
            ]
            rec["findings"].append(
                {
                    "kind": "TRUNCATED_AT_EXCHANGE_TRANSFER" if transfers else "TRUNCATED",
                    "listed_until_at_least": max(later),
                    "transfer": transfers[0]["code"] if transfers else None,
                    "detail": "the provider history ends while the security is still listed: a "
                    "false delisting",
                }
            )


def _continuations(out: list[dict[str, Any]], bars: dict[str, list[list[Any]]]) -> None:
    """A truncated identity whose security another identity continues with identical bars."""
    by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in out:
        if r["security_key"]:
            by_sec[r["security_key"]].append(r)
    for r in out:
        if not any(f["kind"].startswith("TRUNCATED") for f in r["findings"]):
            continue
        mine = {d: c for d, c, _, _ in bars.get(r["asset_id"], [])}
        for o in by_sec.get(r["security_key"], []):
            if o is r or (o["last_bar"] or "") <= (r["last_bar"] or ""):
                continue
            theirs = {d: c for d, c, _, _ in bars.get(o["asset_id"], [])}
            common = set(mine) & set(theirs)
            if len(common) >= MIN_FINGERPRINT and all(
                Decimal(mine[d]) == Decimal(theirs[d]) for d in common
            ):
                r["findings"].append(
                    {
                        "kind": "CONTINUED_BY",
                        "asset_id": o["asset_id"],
                        "identical_sessions": len(common),
                        "continues_to": o["last_bar"],
                    }
                )


def _provider_explanation(rec: dict[str, Any]) -> str:
    fm = "; ".join(
        f"{f['formation']}: {','.join(f['tickers']) or 'no ticker'}" for f in rec["formations"]
    )
    head = (
        f"Bars equal to the formation sweeps of [{fm}]. "
        if rec["formations"]
        else "No bars in any formation window. "
    )
    if rec["status"] == CONFIRMED:
        return head + f"Every resolved holder is {rec['entity_key']} ({rec['security_key']})."
    if rec["status"] == CONFLICTING:
        return head + "Conflicting: " + "; ".join(rec["conflicts"])
    return head + "No issuer could be established from the listings: not linked."


# ---------------------------------------------------------------------- dividends
def dividend_links(store: x4.Store, ix: Index, ledger: dict[str, Any]) -> dict[str, Any]:
    """Each captured provider dividend joined to the holder of its symbol on its ex-date."""
    ends: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in ledger["events"]:
        if e["event_type"] in lc.TERMINAL and e["resolution_status"] == CONFIRMED:
            ends[e["security_key"]].append(e)
    seen, rows = set(), []
    for rel in sorted(k for k in store.entries if k.startswith("provider/dividends/")):
        for page in x4.read_json(store.path(rel))["pages"]:
            body = (page.get("body") or {}).get("corporate_actions") or {}
            for d in body.get("cash_dividends") or []:
                if d.get("id") in seen:
                    continue
                seen.add(d.get("id"))
                rows.append((d["symbol"], d["ex_date"], d.get("rate"), d.get("id"), rel))
    stats: Counter[str] = Counter()
    flagged = []
    for sym, ex, rate, did, rel in sorted(rows, key=lambda r: (r[0], r[1], str(r[3]))):
        h = holder(ix, sym, date.fromisoformat(ex))
        stats[h["status"]] += 1
        if h["status"] == CONFIRMED:
            for e in ends.get(h["security"], []):
                lim = e["not_after"]
                if lim and ex > lim:
                    stats["after_holder_end"] += 1
            continue
        if "changed issuer" in h["explanation"] or h["status"] == CONFLICTING:
            flagged.append(
                {
                    "symbol": sym,
                    "ex_date": ex,
                    "rate": rate,
                    "id": did,
                    "status": h["status"],
                    "explanation": h["explanation"],
                    "evidence": [_ref(store, rel, f"dividend {did}")] + h["evidence"],
                }
            )
    return {"dividends": len(rows), "by_status": dict(stats), "ambiguous_holder": flagged}


# ---------------------------------------------------------------------- probes
def probe_summary(x43s: x43.Store) -> dict[str, Any]:
    """For each probed ticker and mapping date: the bar range, and where a later mapping
    ends later or starts later than the one before it (a mapping segment change), with
    whether the overlapping closes are identical (None when they do not overlap)."""
    pb = x43.probe_bars(x43s)
    out: dict[str, Any] = {}
    for t in x43.PROBE_TICKERS:
        rows = []
        for asof in sorted(pb):
            s = pb[asof].get(t, [])
            rows.append(
                {
                    "asof": asof.isoformat(),
                    "bars": len(s),
                    "first": s[0][0] if s else None,
                    "last": s[-1][0] if s else None,
                }
            )
        breaks = []
        for a, b in zip(sorted(pb), sorted(pb)[1:], strict=False):
            sa, sb = dict(pb[a].get(t, [])), dict(pb[b].get(t, []))
            if not sa or not sb:
                continue
            la, lb = max(sa), max(sb)
            common = set(sa) & set(sb)
            same = all(sa[d] == sb[d] for d in common) if common else None
            if min(sb) > min(sa):
                breaks.append(
                    {
                        "mapped_before": a.isoformat(),
                        "mapped_after": b.isoformat(),
                        "later_mapping_starts": min(sb),
                        "earlier_mapping_starts": min(sa),
                        "common_sessions": len(common),
                        "identical_on_common": same,
                    }
                )
            if lb > la and la < BARS_END.isoformat():
                breaks.append(
                    {
                        "mapped_before": a.isoformat(),
                        "mapped_after": b.isoformat(),
                        "earlier_mapping_ends": la,
                        "common_sessions": len(common),
                        "identical_on_common": same,
                    }
                )
            elif common and not same:
                breaks.append(
                    {
                        "mapped_before": a.isoformat(),
                        "mapped_after": b.isoformat(),
                        "common_sessions": len(common),
                        "identical_on_common": False,
                        "first_difference": min(d for d in common if sa[d] != sb[d]),
                    }
                )
        out[t] = {"mappings": rows, "segment_changes": breaks}
    return out


# ---------------------------------------------------------------------- ledger proposals
def ledger_proposals(
    edges: list[dict[str, Any]], notes: list[dict[str, Any]], ledger: dict[str, Any]
) -> list[dict[str, Any]]:
    """Exchange transfers and ticker changes the graph shows and the X4.2 ledger lacks.
    Proposals only: the ledger is never changed here. One security's consecutive symbol-
    directory edges (HIGH, a known class) on two venues or under two tickers, not overlapping."""
    have = {e["code"] for e in ledger["events"]}
    removals: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for n in notes:
        if n["status"] == CONFIRMED and n["reason"] == lc.RULES["c"][0]:
            removals[(n["security_key"], n["venue"])].append(n)
    by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in edges:
        if e["ticker"].endswith(".V"):
            continue  # a when-issued symbol, not the security's ticker
        if e["confidence"] == "HIGH" and not e["security_key"].endswith("|UNSPECIFIED"):
            by_sec[e["security_key"]].append(e)
    out = []
    for sec, rows in sorted(by_sec.items()):
        rows.sort(key=lambda e: (e["first_observed"], e["last_observed"]))
        for a, b in itertools.pairwise(rows):
            if a["last_observed"] >= b["first_observed"]:
                continue  # overlapping observations: not a succession
            if a["venue"] != b["venue"] and a["ticker"] == b["ticker"]:
                code = f"EXCHANGE_TRANSFER|{sec}|{a['venue']}->{b['venue']}"
                kind = "EXCHANGE_TRANSFER"
                rem = [
                    n
                    for n in removals.get((sec, a["venue"]), [])
                    if a["last_observed"] <= n["filed"] <= b["first_observed"]
                ]
                status = CONFIRMED if rem else UNRESOLVED
                conf = "HIGH" if rem else "LOW"
                why = (
                    f"a voluntary removal from {a['venue']} ({rem[0]['accession']}, filed "
                    f"{rem[0]['filed']}) is linked to {a['ticker']}"
                    if rem
                    else "no linked removal from the old venue: not confirmed"
                )
            elif a["ticker"] != b["ticker"] and a["venue"] == b["venue"]:
                code = f"TICKER_CHANGE|{sec}|{a['ticker']}->{b['ticker']}"
                kind = "TICKER_CHANGE"
                status, conf = CONFIRMED, "HIGH" if sec.startswith("SECCLASS:") else "MEDIUM"
                why = (
                    "the same SEC share class"
                    if sec.startswith("SECCLASS:")
                    else "the same issuer and class, by listed name"
                ) + f" is listed as {a['ticker']} and then as {b['ticker']}"
            else:
                continue
            out.append(
                {
                    "proposed_code": code,
                    "event_type": kind,
                    "security_key": sec,
                    "in_x42_ledger": code in have,
                    "not_before": a["last_observed"],
                    "not_after": b["first_observed"],
                    "from": [a["ticker"], a["venue"]],
                    "to": [b["ticker"], b["venue"]],
                    "status": status,
                    "confidence": conf,
                    "method": "CONSECUTIVE_LISTING_EDGES",
                    "evidence": [a["evidence"][-1], b["evidence"][0]]
                    + ([rem[0]["evidence"][0]] if kind == "EXCHANGE_TRANSFER" and rem else []),
                    "explanation": f"{sec}: listed as {a['ticker']} on {a['venue']} until at least "
                    f"{a['last_observed']}, then as {b['ticker']} on {b['venue']} from "
                    f"{b['first_observed']}; {why}. Bounded, not dated.",
                }
            )
    return out


# ---------------------------------------------------------------------- investigations
INVESTIGATIONS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def _bars_of(x43s: x43.Store, asset: str) -> dict[str, float]:
    rel = f"db/provider_bars/{asset[0]}.json.gz"
    if not x43s.has(rel):
        return {}
    key = ("bars", rel)
    if key not in x43s.cache:
        x43s.cache[key] = x4.read_json(x43s.path(rel))
    return {d: float(c) for d, c, _, _ in x43s.cache[key].get(asset, []) if c is not None}


def agreement(mine: dict[str, float], probe: list[tuple[str, float]]) -> dict[str, Any] | None:
    """Where an identity's raw closes equal a probe's: equal and differing sessions, and the
    first and last equal session."""
    common = [d for d, _ in probe if d in mine]
    if not common:
        return None
    pd = dict(probe)
    equal = [d for d in common if round(mine[d], 6) == round(pd[d], 6)]
    return {
        "common_sessions": len(common),
        "equal_sessions": len(equal),
        "first_equal": equal[0] if equal else None,
        "last_equal": equal[-1] if equal else None,
        "first_different": next((d for d in common if d not in set(equal)), None),
    }


def investigations(
    doc: dict[str, Any], ledger: dict[str, Any], x43s: x43.Store | None = None
) -> dict[str, Any]:
    """Everything the graph holds about each named case, gathered by ticker and then by the
    securities those tickers resolved to (never by ticker alone)."""
    probes = x43.probe_bars(x43s) if x43s is not None else {}
    out = {}
    for case, tickers in INVESTIGATIONS.items():
        ts = set(tickers)
        edges = [e for e in doc["ticker_edges"] if e["ticker"] in ts]
        secs = {e["security_key"] for e in edges}
        ents = {e["entity_key"] for e in edges}
        prov = [
            p
            for p in doc["provider_links"]
            if p["request"]["ticker"] in ts
            or p["request"]["label"].split("~")[0] in ts
            or any(set(f["tickers"]) & ts for f in p["formations"])
        ]
        out[case] = {
            "tickers": sorted(ts),
            "ticker_edges": [
                {
                    k: e[k]
                    for k in (
                        "security_key",
                        "ticker",
                        "venue",
                        "first_observed",
                        "last_observed",
                        "listed_names",
                        "confidence",
                    )
                }
                for e in edges
            ],
            "unresolved_listings": [
                {
                    k: u[k]
                    for k in (
                        "ticker",
                        "venue",
                        "listed_name",
                        "first_observed",
                        "last_observed",
                        "method",
                    )
                }
                for u in doc["unresolved_listings"]
                if u["ticker"] in ts
            ],
            "notices": [
                {
                    k: n.get(k)
                    for k in (
                        "accession",
                        "form",
                        "filed",
                        "venue",
                        "reason",
                        "security_key",
                        "ticker",
                        "status",
                        "confidence",
                        "observed",
                        "successor",
                        "explanation",
                    )
                }
                for n in doc["notices"]
                if n["ticker"] in ts
                or n["security_key"] in secs
                or (
                    n["issuer"] in ents
                    and n["status"] != CONFIRMED
                    and n["class_category"] in GENERIC
                )
            ],
            "ledger_events": sorted(
                f"{e['event_type']}|{e['resolution_status']}|{e['code']}"
                for e in ledger["events"]
                if e["security_key"] in secs and e["event_type"] != "LISTING_OBSERVED"
            ),
            "provider_links": [
                {
                    k: p[k]
                    for k in (
                        "asset_id",
                        "request",
                        "first_bar",
                        "last_bar",
                        "recorded_delisting",
                        "security_key",
                        "status",
                        "confidence",
                        "conflicts",
                    )
                }
                | {
                    "tickers": [f["tickers"] for f in p["formations"]],
                    "findings": [
                        f for f in p["findings"] if f["kind"] != "PARTIAL_HOLDER_EVIDENCE"
                    ],
                }
                for p in prov
            ],
            "ticker_reuse": [r for r in doc["ticker_reuse"] if r["ticker"] in ts],
            "provider_vs_probes": [
                {"asset_id": p["asset_id"], "probe_ticker": t, "probe_asof": a.isoformat(), **agr}
                for p in prov
                for a in sorted(probes)
                for t in sorted(ts)
                if (agr := agreement(_bars_of(x43s, p["asset_id"]), probes[a].get(t, [])))
                and agr["equal_sessions"]
            ]
            if x43s is not None
            else [],
            "probes": {t: doc["probes"][t] for t in sorted(ts) if t in doc["probes"]},
        }
    return out


# ---------------------------------------------------------------------- build
def load_ledger(path: Path = LEDGER, expect: str = LEDGER_SHA256) -> dict[str, Any]:
    import json

    data = path.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != expect:
        raise ValueError(f"{path}: sha256 {got} is not the X4.2 ledger {expect}")
    doc: dict[str, Any] = json.loads(data)
    if lc.content_sha256(doc["events"]) != doc["content_sha256"]:
        raise ValueError(f"{path}: events do not hash to the recorded content")
    return doc


def build(
    x4s: x4.Store, x43s: x43.Store, ledger: dict[str, Any], ledger_sha256: str = LEDGER_SHA256
) -> dict[str, Any]:
    funds = lc.fund_classes(x4s)
    ed = edgar(x4s)
    obs = observations(x4s)
    resolve_observations(obs, ed, funds)
    ix = Index.of(obs)
    edges, loose = ticker_edges(obs)
    notes, nstats = notices(x4s, ledger, obs, ix)
    reuse = ticker_reuse(edges, funds, x4s)
    prov = (
        provider_links(x4s, x43s, ix, ledger, edges, notes)
        if x43s.has("db/provider_identities.json.gz")
        else []
    )
    divs = dividend_links(x4s, ix, ledger)
    probes = probe_summary(x43s)
    doc = {
        "kind": "x43_identity_graph",
        "decision": "D-247",
        "graph_code": GRAPH_CODE,
        "inputs": {
            "x41_manifest_sha256": x4.sha256(x4s.manifest_path.read_bytes()),
            "x43_manifest_sha256": x4.sha256(x43s.manifest_path.read_bytes())
            if x43s.manifest_path.exists()
            else None,
            "ledger_sha256": ledger_sha256,
            "ledger_content_sha256": ledger["content_sha256"],
        },
        "notices": notes,
        "ticker_edges": edges,
        "unresolved_listings": loose,
        "ticker_reuse": reuse,
        "provider_links": prov,
        "dividends": divs,
        "probes": probes,
    }
    doc["ledger_proposals"] = ledger_proposals(edges, notes, ledger)
    doc["investigations"] = investigations(doc, ledger, x43s)
    doc["summary"] = summarize(doc, nstats)
    doc["content_sha256"] = content_sha256(doc)
    return doc


def summarize(doc: dict[str, Any], nstats: dict[str, Any]) -> dict[str, Any]:
    def by(rows: list[dict[str, Any]], key: str = "status") -> dict[str, int]:
        return dict(sorted(Counter(r[key] for r in rows).items()))

    notes = doc["notices"]
    unparsed = [n for n in notes if n["rule_x43"] is not None]
    linked = {n["security_key"] for n in notes if n["status"] == CONFIRMED}
    return {
        "notice_documents": nstats.get("documents", 0),
        "notice_index_copies": nstats.get("index_copies", 0),
        "duplicate_ledger_events": nstats.get("duplicate_events", 0),
        "notice_targets": len(notes),
        "notice_targets_by_status": by(notes),
        "notice_targets_by_method": by(notes, "method"),
        "notice_securities_linked": len(linked),
        "amendments_linked": sum(1 for n in notes if n["amends"]),
        "label_notes": sum(1 for n in notes if n["label_note"]),
        "unparsed_rule_targets": len(unparsed),
        "unparsed_rule_resolved": dict(
            sorted(Counter((n["rule_x43"]["rule"] or "unresolved") for n in unparsed).items())
        ),
        "ticker_edges": len(doc["ticker_edges"]),
        "ticker_edge_securities": len({e["security_key"] for e in doc["ticker_edges"]}),
        "ticker_edge_issuers": len({e["entity_key"] for e in doc["ticker_edges"]}),
        "unresolved_listings": len(doc["unresolved_listings"]),
        "unresolved_listings_by_method": by(doc["unresolved_listings"], "method"),
        "ticker_reuse": len(doc["ticker_reuse"]),
        "ticker_reuse_by_status": by(doc["ticker_reuse"]),
        "provider_identities": len(doc["provider_links"]),
        "provider_links_by_status": by(doc["provider_links"]),
        "provider_findings": dict(
            sorted(Counter(f["kind"] for p in doc["provider_links"] for f in p["findings"]).items())
        ),
        "dividends": doc["dividends"]["dividends"],
        "dividends_by_status": doc["dividends"]["by_status"],
        "ledger_proposals": dict(
            sorted(
                Counter(
                    f"{p['event_type']}|{p['status']}|{'in_x42' if p['in_x42_ledger'] else 'new'}"
                    for p in doc.get("ledger_proposals", [])
                ).items()
            )
        ),
        "successors_under_same_ticker": sum(1 for n in notes if n.get("successor")),
    }


def content_sha256(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k not in ("content_sha256", "inputs", "summary")}
    return hashlib.sha256(x4.canonical(_plain(body))).hexdigest()


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def write(doc: dict[str, Any], path: Path) -> str:
    """Write the graph once (deterministic gzip); never replace an existing file."""
    if path.exists():
        raise FileExistsError(f"{path} exists: the identity graph is written once")
    data = x4.gz(x4.canonical(_plain(doc)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def verify(path: Path, x4s: x4.Store, x43s: x43.Store, ledger: dict[str, Any]) -> dict[str, Any]:
    """Rebuild from the preserved evidence and compare."""
    stored = x4.read_json(path)
    rebuilt = build(x4s, x43s, ledger)
    return {
        "stored_content_sha256": stored["content_sha256"],
        "rebuilt_content_sha256": rebuilt["content_sha256"],
        "ok": stored["content_sha256"] == rebuilt["content_sha256"]
        and stored["inputs"] == rebuilt["inputs"],
    }


__all__ = [
    "GRAPH_CODE",
    "build",
    "category",
    "checkbox_rule",
    "compatible",
    "content_sha256",
    "holder",
    "load_ledger",
    "nkey",
    "verify",
    "write",
]
