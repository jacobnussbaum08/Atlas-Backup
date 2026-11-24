"""X4.2 (D-246): the security lifecycle ledger, built only from the X4.1 evidence.

:func:`build` reads the X4.1 store (never writes to it) and returns a deterministic ledger
document: issuers, securities and lifecycle events, each event with its evidence (artifact
path, SHA-256, locator). :func:`register` loads a ledger into the 0028 tables in one
transaction, once, with its content hash. :func:`reconstruct` returns a security's timeline.

**Identities.** An issuer is an SEC CIK. A fund share class is its SEC class id (from the
SEC series/class file, matched by the exact class ticker *and* the exact listed name). An
operating company's security is ``CIK|LABEL`` (the class of securities as filed or listed).
A ticker is never an identity: ticker reuse makes two securities, never one.

**Events and precedence.**

* Every Form 25 / 25-NSE becomes a ``VENUE_REMOVAL``: the security left *that venue*. It
  never ends a security by itself.
* A lifecycle end is derived only from the rule provision that retires or converts the class
  (17 CFR 240.12d2-2(a)(1)-(4)): ``DELISTING`` or, for a registered fund class,
  ``FUND_TERMINATION``. Exchange-initiated (b) and voluntary (c) removals end nothing: the
  security may trade elsewhere. Their consequence stays unresolved unless listing evidence
  resolves it (``EXCHANGE_TRANSFER``).
* Dates: an effective date only when the evidence states one (none of the sources here
  does). Otherwise bounds from point-in-time listings (the last capture listing it, the first
  capture listing it elsewhere or not at all), and the filing date separately. Unknown stays
  unknown.
* Source precedence: SEC filings, then Nasdaq Trader symbol directories (the exchanges' own
  daily listing files), then Nasdaq.com company lists. A lower source that contradicts a
  higher one at the same time is marked ``CONFLICTING``, not dropped.
* Named cases (:data:`NAMED`) are linked to SEC issuers explicitly, each link citing the
  listed name it relies on; nothing is matched by ticker similarity.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import html
import itertools
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import (
    SecurityEntityRecord,
    SecurityIdentityRecord,
    SecurityLifecycleEventRecord,
    SecurityLifecycleSetRecord,
)
from app.research import x4_evidence as x4

OUT = Path("config/simulation/lifecycle")
SET_CODE = "SECURITY_LIFECYCLE_V1"
CREATED_BY = "operator:research-foundation"
TERMINAL = ("DELISTING", "FUND_TERMINATION")
# 17 CFR 240.12d2-2: what each provision says about the class.
RULES = {
    "a1": ("CLASS_CALLED_FOR_REDEMPTION_OR_RETIREMENT", True),
    "a2": ("CLASS_REDEEMED_OR_RETIRED", True),
    "a3": ("CLASS_CONVERTED_BY_OPERATION_OF_LAW", True),
    "a4": ("CLASS_RIGHTS_EXTINGUISHED", True),
    "b": ("EXCHANGE_INITIATED_REMOVAL", False),
    "c": ("ISSUER_VOLUNTARY_WITHDRAWAL", False),
}
VENUES = (
    (re.compile(r"arca", re.I), "NYSE_ARCA"),
    (re.compile(r"american|\bmkt\b|amex", re.I), "NYSE_AMERICAN"),
    (re.compile(r"chicago", re.I), "NYSE_CHICAGO"),
    (re.compile(r"new york stock exchange|\bnyse\b", re.I), "NYSE"),
    (re.compile(r"nasdaq", re.I), "NASDAQ"),
    (re.compile(r"bats|bzx|cboe", re.I), "CBOE_BZX"),
)
# Nasdaq Trader otherlisted exchange codes.
OTHERLISTED_VENUE = {
    "N": "NYSE",
    "P": "NYSE_ARCA",
    "A": "NYSE_AMERICAN",
    "Z": "CBOE_BZX",
    "V": "IEX",
    "M": "NYSE_CHICAGO",
}
SOURCE_RANK = {"symdir": 2, "companylist": 1}


@dataclass(frozen=True)
class Named:
    """An explicitly investigated security: its ticker, its issuer, the class label, and the
    listed-name stem a point-in-time listing must start with to be this security."""

    ticker: str
    cik: str
    label: str
    name_stem: str
    note: str


NAMED = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
ISHARES_2017 = x4.ISHARES_2017
ISHARES_TRUSTS = ("0001100663", "0000930667")


# ---------------------------------------------------------------------- helpers
def norm(text: str | None) -> str:
    t = html.unescape(text or "").lower().replace("&", "and")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.&,/ -]", " ", t)).strip()


def alnum(text: str | None) -> str:
    """A name compared on its letters and digits only (case-folded): the exchanges' own files
    REDACTED_FOR_GITHUB
    Letters are never altered, so a different name never matches."""
    return re.sub(r"[^a-z0-9]", "", html.unescape(text or "").lower().replace("&", "and"))


def class_label(text: str | None) -> str:
    """A stable label for a class of securities as filed or listed."""
    t = norm(text)
    if not t:
        return "UNSPECIFIED"
    if "depositary" in t or "depository" in t or re.search(r"\bads\b|\badr\b", t):
        return "ADS"
    if "preferred" in t or "preference" in t:
        return "PREFERRED-" + hashlib.sha256(t.encode()).hexdigest()[:10]
    m = re.search(r"class ([a-z]) (common|ordinary)", t)
    if m:
        return f"CLASS_{m.group(1).upper()}_COMMON"
    if "common" in t:
        return "COMMON"
    if "ordinary share" in t:
        return "ORDINARY"
    return "CLASS-" + hashlib.sha256(t.encode()).hexdigest()[:10]


def venue_of(name: str | None) -> str | None:
    for rx, code in VENUES:
        if name and rx.search(name):
            return code
    return None


def evidence_ref(store: x4.Store, rel: str, locator: str) -> dict[str, Any]:
    e = store.entries[rel]
    return {"artifact": rel, "sha256": e["sha256"], "locator": locator}


def _sha(obj: Any) -> str:
    return hashlib.sha256(x4.canonical(obj)).hexdigest()


def _day(ts: str) -> date:
    return date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))


# ---------------------------------------------------------------------- SEC sources
def fund_classes(store: x4.Store) -> dict[int, list[dict[str, str]]]:
    """Year -> SEC series/class rows (positional columns; headers vary by year)."""
    out: dict[int, list[dict[str, str]]] = {}
    for rel in sorted(
        k for k in store.entries if k.startswith("sec/funds/") and k.endswith(".csv.gz")
    ):
        name = rel.rsplit("/", 1)[-1]
        m = re.search(r"(\d{4})\.csv\.gz$", name)
        year = int(m.group(1)) if m else x4.FUND_FILE_YEARS.get(name)
        if year is None:
            continue
        lines = gzip.decompress(store.path(rel).read_bytes()).decode("latin-1").splitlines()
        start = next(i for i, ln in enumerate(lines) if "ticker" in ln.lower())
        rows = []
        for r in csv.reader(lines[start + 1 :]):
            if len(r) < 9 or not r[1].strip().isdigit():
                continue
            rows.append(
                {
                    "cik": f"{int(r[1]):010d}",
                    "entity": r[2].strip(),
                    "series_id": r[4].strip(),
                    "series_name": r[5].strip(),
                    "class_id": r[6].strip(),
                    "class_name": r[7].strip(),
                    "ticker": r[8].strip().upper(),
                    "artifact": rel,
                }
            )
        out[year] = rows
    return out


def removal_documents(store: x4.Store) -> list[dict[str, Any]]:
    """Every removal notice with its text, artifact and line locator."""
    out = []
    for rel in sorted(k for k in store.entries if k.startswith("sec/form25/")):
        for i, line in enumerate(
            gzip.decompress(store.path(rel).read_bytes()).decode().splitlines()
        ):
            row = json.loads(line)
            row["artifact"], row["line"] = rel, i
            out.append(row)
    return out


def _plain(textbody: str) -> str:
    i = textbody.find("<DOCUMENT>")
    t = re.sub(r"<[^>]+>", " ", textbody[i if i >= 0 else 0 :])
    return re.sub(r"\s+", " ", html.unescape(t))


def parse_removal(row: dict[str, Any]) -> dict[str, Any]:
    """Issuer, venue, class, rule and exhibit rows of a removal notice (XML 25-NSE, or the
    text of an issuer-filed Form 25)."""
    xml = x4.parse_notice(row.get("text") or "")
    plain = _plain(row.get("text") or "")
    rule = None
    if xml.get("rule"):
        m = re.search(r"12d2-2\(([abc])\)(?:\((\d)\))?", xml["rule"])
        rule = (m.group(1) + (m.group(2) or "")) if m else None
    else:
        m = re.search(r"☒[^☐☒]{0,400}?12d2?-2\(([abc])\)(?:\((\d)\))?", plain)
        rule = (m.group(1) + (m.group(2) or "")) if m else None
    cls = xml.get("class")
    if not cls:
        m = re.search(
            r"\)\s*([^()]{3,200}?)\s*(?:\(See exhibit A\)\s*)?\(Description of class", plain, re.I
        )
        cls = m.group(1).strip() if m else None
    venue = venue_of(xml.get("exchange_name"))
    if venue is None:
        head = plain.split("(Exact name", 1)[0][-300:]
        venue = venue_of(head)
    exhibit = []
    if re.search(r"Exhibit A", plain):
        tail = plain.split("Exhibit A", 1)[1]
        # The exhibit's column headings end with the employer-number heading.
        tail = re.split(r"Identification No\.?", tail, maxsplit=1)[-1]
        for m in re.finditer(
            # The exhibits are typed by hand: "Inc." appears without its period too.
            r"\s*(.+?)\s+(NYSE Arca, Inc\.?|The NASDAQ Stock Market LLC|Nasdaq Stock Market LLC|"
            r"NYSE American LLC|Cboe BZX Exchange, Inc\.?|Bats BZX Exchange, Inc\.?)"
            r"\s+(\d{2}-\d{7})",
            tail,
        ):
            exhibit.append(
                {"name": m.group(1).strip(), "venue": venue_of(m.group(2)), "ein": m.group(3)}
            )
    return {"rule": rule, "class": cls, "venue": venue, "exhibit": exhibit}


# ---------------------------------------------------------------------- the ledger
class Ledger:
    def __init__(self, store: x4.Store) -> None:
        self.store = store
        self.entities: dict[str, dict[str, Any]] = {}
        self.securities: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}

    def entity(self, cik: str, name: str, source: str, ev: dict[str, Any]) -> str:
        key = f"CIK:{cik}"
        if key not in self.entities:
            self.entities[key] = {
                "entity_key": key,
                "cik": cik,
                "name": name,
                "name_source": source,
                "evidence": [ev],
            }
        return key

    def security(
        self,
        key: str,
        entity_key: str | None,
        label: str,
        description: str | None,
        ev: dict[str, Any],
        class_id: str | None = None,
    ) -> str:
        if key not in self.securities:
            self.securities[key] = {
                "security_key": key,
                "entity_key": entity_key,
                "sec_class_id": class_id,
                "class_label": label,
                "class_description": description,
                "evidence": [ev],
            }
        elif ev not in self.securities[key]["evidence"]:
            self.securities[key]["evidence"].append(ev)
        return key

    def event(self, code: str, **fields: Any) -> dict[str, Any]:
        base = {
            "code": code,
            "revision": 0,
            "effective_date": None,
            "effective_precision": "UNKNOWN",
            "not_before": None,
            "not_after": None,
            "filing_date": None,
            "ticker": None,
            "from_ticker": None,
            "to_ticker": None,
            "venue": None,
            "from_venue": None,
            "to_venue": None,
            "security_type": None,
            "reason": None,
            "counterparty_entity_key": None,
        }
        ev = {**base, **fields}
        if code in self.events:
            old = self.events[code]
            for ref in ev["evidence"]:
                if ref not in old["evidence"]:
                    old["evidence"].append(ref)  # the same fact from another source
            return old
        self.events[code] = ev
        return ev


def _fund_lookup(
    funds: dict[int, list[dict[str, str]]],
) -> dict[tuple[int, str], list[dict[str, str]]]:
    by: dict[tuple[int, str], list[dict[str, str]]] = defaultdict(list)
    for year, rows in funds.items():
        for r in rows:
            by[(year, alnum(r["series_name"]))].append(r)
    return by


def _fund_class(rows: list[dict[str, str]]) -> dict[str, str] | None:
    """The one exchange-traded class of a series (a class with a ticker), else None."""
    traded = [r for r in rows if r["ticker"]]
    return traded[0] if len({r["class_id"] for r in traded}) == 1 else None


def _notice_events(led: Ledger, funds: dict[int, list[dict[str, str]]]) -> list[dict[str, Any]]:
    by_series = _fund_lookup(funds)
    fund_ciks = {r["cik"] for rows in funds.values() for r in rows}
    unparsed = []
    for doc in removal_documents(led.store):
        p = parse_removal(doc)
        filed = date.fromisoformat(doc["filed"])
        ref = evidence_ref(led.store, doc["artifact"], f"line {doc['line']} ({doc['path']})")
        ent = led.entity(doc["cik"], doc["company"], "EDGAR form index (removal notice)", ref)
        targets: list[tuple[str, str | None, dict[str, str] | None]] = []
        if p["exhibit"]:
            for row in p["exhibit"]:
                targets.append((row["name"], row["venue"] or p["venue"], None))
        else:
            targets.append((p["class"] or "", p["venue"], None))
        if p["rule"] is None:
            unparsed.append({"artifact": doc["artifact"], "line": doc["line"], "form": doc["form"]})
        for name, venue, _ in targets:
            fund = None
            if doc["cik"] in fund_ciks:
                for y in (filed.year, filed.year - 1, filed.year + 1):
                    hit = _fund_class(
                        [r for r in by_series.get((y, alnum(name)), []) if r["cik"] == doc["cik"]]
                    )
                    if hit:
                        fund = hit
                        break
            if fund:
                key = led.security(
                    f"SECCLASS:{fund['class_id']}",
                    ent,
                    "FUND_CLASS",
                    fund["series_name"],
                    ref,
                    class_id=fund["class_id"],
                )
            else:
                label = (
                    class_label(name)
                    if not p["exhibit"]
                    else "SERIES-" + hashlib.sha256(norm(name).encode()).hexdigest()[:10]
                )
                key = led.security(f"{ent}|{label}", ent, label, name or None, ref)
            rule = p["rule"]
            reason = RULES.get(rule or "", (f"UNPARSED_RULE:{doc['form']}", False))[0]
            led.event(
                f"VENUE_REMOVAL|{key}|{venue or 'UNKNOWN'}|{doc['filed']}|{doc['form']}",
                security_key=key,
                event_type="VENUE_REMOVAL",
                filing_date=filed,
                venue=venue,
                reason=reason,
                confidence="HIGH",
                resolution_status="CONFIRMED",
                explanation=f"Form {doc['form']} filed {doc['filed']}: the security is removed "
                f"from "
                f"{venue or 'an unparsed venue'} ({reason}). A removal does not by itself end "
                "trading: the security may trade on another venue.",
                evidence=[ref],
            )
            if rule and RULES[rule][1]:
                is_fund = key.startswith("SECCLASS:") or doc["cik"] in fund_ciks
                etype = "FUND_TERMINATION" if is_fund else "DELISTING"
                ev = led.event(
                    f"{etype}|{key}",
                    security_key=key,
                    event_type=etype,
                    filing_date=filed,
                    venue=venue,
                    reason=RULES[rule][0],
                    confidence="HIGH",
                    resolution_status="CONFIRMED",
                    explanation=f"17 CFR 240.12d2-2({rule[0]})({rule[1:]}) states the whole class "
                    f"is {RULES[rule][0].lower().replace('_', ' ')}: the class ends on every "
                    "venue. The effective date is not stated in the filing; the filing date is "
                    "kept separately.",
                    evidence=[ref],
                )
                if filed < ev["filing_date"]:
                    ev["filing_date"] = filed  # the earliest notice of the end
    return unparsed


def _observations(led: Ledger) -> list[dict[str, Any]]:
    """Every archived listing row of a named ticker, with source and venue."""
    out = []
    wanted = {n.ticker for n in NAMED} | set(ISHARES_2017)
    for rel, e in sorted(led.store.entries.items()):
        if (
            not rel.startswith("archive/")
            or not rel.endswith(".raw")
            or not e.get("digest_verified")
        ):
            continue
        family = rel.split("/")[1]
        ts = e["source"]["timestamp"]
        for i, row in enumerate(x4.parse_symbol_list(family, led.store.path(rel).read_bytes())):
            if row["symbol"] not in wanted:
                continue
            venue = (
                OTHERLISTED_VENUE.get(row["exchange"] or "", row["exchange"])
                if family == "symdir_otherlisted"
                else (
                    "NASDAQ"
                    if family == "symdir_nasdaqlisted"
                    else family.rsplit("_", 1)[-1].upper()
                )
            )
            if venue == "AMEX":
                venue = "NYSE_AMERICAN"
            out.append(
                {
                    "ticker": row["symbol"],
                    "name": row["name"],
                    "etf": row["etf"],
                    "venue": venue,
                    "family": family,
                    "timestamp": ts,
                    "day": _day(ts),
                    "source": "symdir" if family.startswith("symdir") else "companylist",
                    "ref": evidence_ref(led.store, rel, f"row {i} ({row['symbol']})"),
                }
            )
    return out


def _listings_without(
    led: Ledger, ticker: str, after: date, families: Iterable[str]
) -> date | None:
    """The first symbol-directory capture after ``after`` that does not list ``ticker``."""
    for rel, e in sorted(
        led.store.entries.items(), key=lambda kv: kv[1].get("source", {}).get("timestamp", "")
    ):
        if not rel.startswith("archive/") or not rel.endswith(".raw"):
            continue
        family = rel.split("/")[1]
        if family not in families:
            continue
        day = _day(e["source"]["timestamp"])
        if day <= after:
            continue
        rows = x4.parse_symbol_list(family, led.store.path(rel).read_bytes())
        if all(r["symbol"] != ticker for r in rows):
            return day
    return None


def _named(led: Ledger, funds: dict[int, list[dict[str, str]]]) -> None:
    obs = _observations(led)
    by_class = {(y, r["ticker"]): r for y, rows in funds.items() for r in rows if r["ticker"]}
    # Operating companies and ADS: observations whose listed name is this security's.
    subs = _submissions(led.store, {n.cik for n in NAMED})
    for n in NAMED:
        sub = subs.get(n.cik)
        if sub is None:
            raise ValueError(f"no captured SEC submissions for named issuer {n.cik}")
        ent = led.entity(n.cik, sub["name"], "SEC submissions (X4.1)", sub["ref"])
        key = led.security(
            f"{ent}|{n.label}", ent, n.label, None, {"named_case": n.note, "name_stem": n.name_stem}
        )
        mine = [
            o for o in obs if o["ticker"] == n.ticker and norm(o["name"]).startswith(n.name_stem)
        ]
        for o in mine:
            led.event(
                f"LISTING_OBSERVED|{key}|{o['ticker']}|{o['family']}|{o['timestamp']}",
                security_key=key,
                event_type="LISTING_OBSERVED",
                effective_date=o["day"],
                effective_precision="EXACT",
                ticker=o["ticker"],
                venue=o["venue"],
                confidence="HIGH" if o["source"] == "symdir" else "MEDIUM",
                resolution_status="CONFIRMED",
                explanation=f"Listed as {o['ticker']} on {o['venue']} in the {o['family']} "
                f"capture of "
                f"{o['timestamp']} ({o['name']!r}). The capture date is when the listing was "
                "observed, not when it began.",
                evidence=[o["ref"]],
            )
    # REDACTED_FOR_GITHUB
    for t in ISHARES_2017:
        mine = [o for o in obs if o["ticker"] == t]
        names = {alnum(o["name"]) for o in mine if o["source"] == "symdir"}
        hit = None
        for y in (2017, 2016, 2018):
            r = by_class.get((y, t))
            if r and r["cik"] in ISHARES_TRUSTS and alnum(r["series_name"]) in names:
                hit = r
                break
        if hit is None:
            continue  # no exact SEC identity: left out rather than guessed
        ent = led.entity(
            hit["cik"],
            hit["entity"],
            f"SEC series/class file ({hit['artifact']})",
            evidence_ref(led.store, hit["artifact"], f"class {hit['class_id']}"),
        )
        key = led.security(
            f"SECCLASS:{hit['class_id']}",
            ent,
            "FUND_CLASS",
            hit["series_name"],
            evidence_ref(led.store, hit["artifact"], f"class {hit['class_id']} ticker {t}"),
            class_id=hit["class_id"],
        )
        for o in mine:
            if alnum(o["name"]) != alnum(hit["series_name"]) and o["source"] == "symdir":
                continue
            led.event(
                f"LISTING_OBSERVED|{key}|{t}|{o['family']}|{o['timestamp']}",
                security_key=key,
                event_type="LISTING_OBSERVED",
                effective_date=o["day"],
                effective_precision="EXACT",
                ticker=t,
                venue=o["venue"],
                confidence="HIGH" if o["source"] == "symdir" else "MEDIUM",
                resolution_status="CONFIRMED",
                explanation=f"Listed as {t} on {o['venue']} in the {o['family']} capture of "
                f"{o['timestamp']} ({o['name']!r}).",
                evidence=[o["ref"]],
            )


def _derive_transfers(led: Ledger) -> None:
    """A venue change between consecutive directory observations of one security is an
    exchange transfer when a removal from the old venue is filed in between, or (lower
    confidence) when the issuer filed a voluntary removal for its funds in between."""
    by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in led.events.values():
        if e["event_type"] == "LISTING_OBSERVED" and "symdir" in e["code"]:
            by_sec[e["security_key"]].append(e)
    removals = defaultdict(list)
    for e in led.events.values():
        if e["event_type"] == "VENUE_REMOVAL":
            removals[e["security_key"]].append(e)
    for key, rows in sorted(by_sec.items()):
        rows.sort(key=lambda e: (e["effective_date"], e["code"]))
        for a, b in itertools.pairwise(rows):
            if a["venue"] == b["venue"] or a["ticker"] != b["ticker"]:
                continue
            matched = [
                r
                for r in removals.get(key, [])
                if r["venue"] == a["venue"]
                and a["effective_date"] <= r["filing_date"] <= b["effective_date"]
                and r["reason"] == RULES["c"][0]
            ]
            entity = led.securities[key]["entity_key"]
            trust_level = [
                r
                for r in led.events.values()
                if r["event_type"] == "VENUE_REMOVAL"
                and r["reason"] == RULES["c"][0]
                and r["venue"] == a["venue"]
                and led.securities[r["security_key"]]["entity_key"] == entity
                and a["effective_date"] <= r["filing_date"] <= b["effective_date"]
            ]
            terminal = [
                e
                for e in led.events.values()
                if e["security_key"] == key and e["event_type"] in TERMINAL
            ]
            if terminal:
                status, conf = "CONFLICTING", "LOW"
            elif matched:
                status, conf = "CONFIRMED", "HIGH"
            elif trust_level:
                status, conf = "CONFIRMED", "MEDIUM"
            else:
                status, conf = "UNRESOLVED", "LOW"
            filing = (matched or trust_level or [{}])[0].get("filing_date")
            led.event(
                f"EXCHANGE_TRANSFER|{key}|{a['venue']}->{b['venue']}",
                security_key=key,
                event_type="EXCHANGE_TRANSFER",
                effective_precision="BOUNDED",
                not_before=a["effective_date"],
                not_after=b["effective_date"],
                filing_date=filing,
                ticker=a["ticker"],
                from_venue=a["venue"],
                to_venue=b["venue"],
                confidence=conf,
                resolution_status=status,
                explanation=f"Listed as {a['ticker']} on {a['venue']} on {a['effective_date']} and "
                f"on {b['venue']} on {b['effective_date']}, same security, no lifecycle end. "
                + (
                    "A voluntary removal of this security from the old venue was filed in between: "
                    "the listing moved; the security continued."
                    if matched
                    else "The issuer filed a voluntary removal from the old venue in between, "
                    "but the "
                    "filing could not be matched to this security exactly (see its exhibit)."
                    if trust_level
                    else "No removal from the old venue is in the evidence."
                )
                + " The exact switch date is not stated in the evidence: bounded.",
                evidence=sorted(
                    [a["evidence"][0], b["evidence"][0]]
                    + [r["evidence"][0] for r in matched or trust_level[:1]],
                    key=lambda r: (r["artifact"], r["locator"]),
                ),
            )


def _named_refinements(led: Ledger) -> None:
    """Named terminal events: bounds from the listings, mergers, the ticker change, the
    REDACTED_FOR_GITHUB"""

    def key_of(cik: str, label: str) -> str:
        return f"CIK:{cik}|{label}"

    def obs(key: str) -> list[dict[str, Any]]:
        return sorted(
            (
                e
                for e in led.events.values()
                if e["security_key"] == key
                and e["event_type"] == "LISTING_OBSERVED"
                and e["resolution_status"] == "CONFIRMED"
            ),
            key=lambda e: e["effective_date"],
        )

    # Bounds on every ended security that has symbol-directory observations: the last
    # capture listing it, and the first capture of that directory without its ticker after it
    # (only when such a capture exists). A capture listing the ticker for another security
    # does not show this one listed.
    terminals = [e for e in led.events.values() if e["event_type"] in TERMINAL]
    for term in sorted(terminals, key=lambda e: e["code"]):
        key = term["security_key"]
        seen = [o for o in obs(key) if "symdir" in o["code"]]
        if not seen:
            continue
        last, ticker = seen[-1]["effective_date"], seen[-1]["ticker"]
        family = "symdir_nasdaqlisted" if seen[-1]["venue"] == "NASDAQ" else "symdir_otherlisted"
        gone = _listings_without(led, ticker, last, (family,))
        term.update(effective_precision="BOUNDED", not_before=last, not_after=gone)
        term["explanation"] += (
            f" Listed until at least {last} (last symbol-directory capture listing it)"
            + (
                f"; not listed in the {family} capture of {gone}."
                if gone
                else "; no later capture exists in the evidence."
            )
        )
        term["evidence"].append(seen[-1]["evidence"][0])
        # Mergers: a conversion by operation of law with the issuer's own business-combination
        # filings (Form 425) in the captured submissions confirms a merger. The counterparty
        # is never in this evidence and stays unknown.
        if term["reason"] != RULES["a3"][0]:
            continue
        cik = (led.securities[key]["entity_key"] or "")[4:]
        sub = _submissions(led.store, {cik}).get(cik)
        f425 = [d for f, d in sub["forms"] if f == "425"] if sub else []
        f15 = sorted(d for f, d in sub["forms"] if f.startswith("15-12")) if sub else []
        dereg = sorted({f for f, _ in sub["forms"] if f.startswith("15-12")}) if sub else []
        confirmed = bool(f425)
        led.event(
            f"MERGER_ACQUISITION|{key}",
            security_key=key,
            event_type="MERGER_ACQUISITION",
            filing_date=term["filing_date"],
            reason=term["reason"],
            confidence="MEDIUM" if confirmed else "LOW",
            resolution_status="CONFIRMED" if confirmed else "UNRESOLVED",
            explanation=(
                "The class was converted by operation of law (12d2-2(a)(3)) and the issuer "
                f"filed {len(f425)} business-combination communications (Form 425, "
                f"{min(f425)}..{max(f425)})"
                if confirmed
                else "The class was converted by operation of law (12d2-2(a)(3)), but the "
                "captured filings hold no business-combination communication from the issuer"
            )
            + (f" and deregistered (Form {', '.join(dereg)} {f15[0]})" if f15 else "")
            + ". The acquirer is not named in the evidence: the counterparty stays unknown.",
            evidence=[term["evidence"][0]] + ([sub["ref"]] if sub else []),
        )
    # Ticker changes: one security observed on one venue under one ticker, then another.
    for key in sorted({e["security_key"] for e in led.events.values()}):
        seen = [o for o in obs(key) if "symdir" in o["code"]]
        for a, b in itertools.pairwise(seen):
            if a["ticker"] == b["ticker"] or a["venue"] != b["venue"]:
                continue
            led.event(
                f"TICKER_CHANGE|{key}|{a['ticker']}->{b['ticker']}",
                security_key=key,
                event_type="TICKER_CHANGE",
                effective_precision="BOUNDED",
                not_before=a["effective_date"],
                not_after=b["effective_date"],
                from_ticker=a["ticker"],
                to_ticker=b["ticker"],
                venue=a["venue"],
                confidence="HIGH",
                resolution_status="CONFIRMED",
                explanation=f"The same security (same listed name, same issuer) is listed on "
                f"{a['venue']} as {a['ticker']} on {a['effective_date']} and as {b['ticker']} on "
                f"{b['effective_date']}: it changed ticker. Any other security that held "
                f"{b['ticker']} before is a separate security.",
                evidence=[a["evidence"][0], b["evidence"][0]],
            )
    # REDACTED_FOR_GITHUB
    funds = fund_classes(led.store)
    for ticker, cik in (("REDACTED_FOR_GITHUB", "0000721683"), ("REDACTED_FOR_GITHUB", "0000921112"), ("REDACTED_FOR_GITHUB", "0000701374")):
        key = key_of(cik, "COMMON")
        seen = obs(key)
        in_fund_files = sorted(
            y for y in range(2015, 2020) for r in funds.get(y, []) if r["ticker"] == ticker
        )
        flags = [e for e in seen if "symdir" in e["code"]]
        if seen:
            led.event(
                f"CLASSIFICATION|{key}|OPERATING_COMPANY",
                security_key=key,
                event_type="CLASSIFICATION",
                effective_precision="BOUNDED",
                not_before=seen[0]["effective_date"],
                not_after=seen[-1]["effective_date"],
                ticker=ticker,
                security_type="OPERATING_COMPANY_EQUITY",
                confidence="HIGH" if not in_fund_files and flags else "MEDIUM",
                resolution_status="CONFIRMED" if not in_fund_files else "CONFLICTING",
                explanation=f"{ticker} is listed as an operating company's stock in every "
                f"capture from "
                f"{seen[0]['effective_date']} to {seen[-1]['effective_date']} "
                "(symbol directory ETF flag N; "
                "Nasdaq.com company lists) and is in no SEC fund series/class file for 2015-2019"
                + ("" if not in_fund_files else f" -- but it is in the {in_fund_files} fund files")
                + ". "
                "Typing it as a fund (research master) relied on today's fund lists.",
                evidence=[e["evidence"][0] for e in seen],
            )
        later = [r for y in sorted(funds) if y >= 2020 for r in funds[y] if r["ticker"] == ticker]
        if later:
            r = later[-1]
            years = sorted(
                {
                    y
                    for y in funds
                    if y >= 2020
                    for x in funds[y]
                    if x["ticker"] == ticker and x["class_id"] == r["class_id"]
                }
            )
            ent = led.entity(
                r["cik"],
                r["entity"],
                f"SEC series/class file ({r['artifact']})",
                evidence_ref(led.store, r["artifact"], f"class {r['class_id']}"),
            )
            fkey = led.security(
                f"SECCLASS:{r['class_id']}",
                ent,
                "FUND_CLASS",
                r["series_name"],
                evidence_ref(led.store, r["artifact"], f"class {r['class_id']} ticker {ticker}"),
                class_id=r["class_id"],
            )
            led.event(
                f"CLASSIFICATION|{fkey}|FUND",
                security_key=fkey,
                event_type="CLASSIFICATION",
                effective_precision="BOUNDED",
                not_before=date(years[0], 1, 1),
                not_after=date(years[-1], 12, 31),
                ticker=ticker,
                security_type="FUND_CLASS",
                confidence="HIGH",
                resolution_status="CONFIRMED",
                explanation=f"The SEC's {years[0]}-{years[-1]} series/class files give ticker "
                f"{ticker} to "
                f"{r['series_name']!r} ({r['entity']}), a fund class unrelated to the 2016-2019 "
                f"operating company: the ticker was reused. Bounds are the files' years.",
                evidence=[evidence_ref(led.store, r["artifact"], f"class {r['class_id']}")],
            )
            continue
        # No SEC fund class: today's provider list may still show the ticker's new holder.
        for status in ("active", "inactive"):
            rel = f"provider/assets/assets_{status}.json.gz"
            if not led.store.has(rel):
                continue
            body = x4.read_json(led.store.path(rel)).get("body") or []
            for i, a in enumerate(body):
                name = a.get("name") or ""
                if a.get("symbol") != ticker or not x4.ETP_NAME.search(name):
                    continue
                seen_on = date.fromisoformat(led.store.entries[rel]["retrieved_at"][:10])
                pkey = led.security(
                    f"ALPACA:{a['id']}",
                    None,
                    "FUND_OR_ETP",
                    name,
                    evidence_ref(led.store, rel, f"row {i} ({ticker})"),
                )
                venue = venue_of(a.get("exchange")) or a.get("exchange")
                led.event(
                    f"LISTING_OBSERVED|{pkey}|{ticker}|provider_assets|{seen_on}",
                    security_key=pkey,
                    event_type="LISTING_OBSERVED",
                    effective_date=seen_on,
                    effective_precision="EXACT",
                    ticker=ticker,
                    venue=venue,
                    security_type="FUND_OR_ETP",
                    confidence="MEDIUM",
                    resolution_status="CONFIRMED",
                    explanation=f"Today's provider asset list ({status}, retrieved {seen_on}) "
                    "gives "
                    f"{ticker} to {name!r} on {a.get('exchange')}: a fund unrelated to the "
                    "2016-2019 "
                    "operating company, so the ticker was reused. No SEC series/class record names "
                    "it in the captured files; its issuer is not identified.",
                    evidence=[evidence_ref(led.store, rel, f"row {i} ({ticker})")],
                )


def _submissions(store: x4.Store, ciks: set[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for rel in sorted(k for k in store.entries if k.startswith("sec/submissions/")):
        for i, line in enumerate(
            gzip.decompress(store.path(rel).read_bytes()).decode().splitlines()
        ):
            row = json.loads(line)
            if row["cik"] not in ciks:
                continue
            d = json.loads(row["text"])
            f = d["filings"]["recent"]
            forms = sorted(zip(f["form"], f["filingDate"], strict=False))
            out[row["cik"]] = {
                "name": d.get("name"),
                "forms": [(a, b) for a, b in forms if "2015" <= b <= "2020"],
                "ref": evidence_ref(store, rel, f"line {i} (CIK {row['cik']})"),
            }
    return out


# ---------------------------------------------------------------------- validation
def validate(
    events: list[dict[str, Any]], securities: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Temporal and precedence checks. Returns issues; :func:`build` marks the events."""
    issues: list[dict[str, Any]] = []
    by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_sec[e["security_key"]].append(e)
        if e["security_key"] not in securities:
            issues.append({"code": e["code"], "issue": "event of an unknown security"})
        nb, na = e["not_before"], e["not_after"]
        if nb and na and nb > na:
            issues.append({"code": e["code"], "issue": "bounds out of order"})
        if e["effective_precision"] == "EXACT" and e["effective_date"] is None:
            issues.append({"code": e["code"], "issue": "exact without a date"})
        if e["effective_precision"] != "EXACT" and e["effective_date"] is not None:
            issues.append({"code": e["code"], "issue": "a date without exact precision"})
    # A confirmed listing observed after a terminal event's latest possible date conflicts.
    for _key, evs in by_sec.items():
        ends = [
            e for e in evs if e["event_type"] in TERMINAL and e["resolution_status"] == "CONFIRMED"
        ]
        for end in ends:
            limit = end["not_after"] or (
                end["filing_date"] + timedelta(days=30) if end["filing_date"] else None
            )
            for o in evs:
                if (
                    o["event_type"] == "LISTING_OBSERVED"
                    and limit
                    and o["effective_date"] is not None
                    and o["effective_date"] > limit
                ):
                    issues.append(
                        {
                            "code": o["code"],
                            "issue": f"listed after {end['code']} ended the class",
                            "mark": "CONFLICTING",
                        }
                    )
    # Ticker reuse: one ticker, two securities, observed on the same day -> the lower source
    # conflicts with the higher one.
    seen: dict[tuple[str, date], list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e["event_type"] == "LISTING_OBSERVED" and e["resolution_status"] == "CONFIRMED":
            seen[(e["ticker"], e["effective_date"])].append(e)
    for (ticker, _), evs in seen.items():
        if len({e["security_key"] for e in evs}) > 1:
            for e in evs:
                issues.append(
                    {
                        "code": e["code"],
                        "issue": f"{ticker} listed for two securities the same day",
                        "mark": "CONFLICTING",
                    }
                )
    return issues


def _cross_source(events: dict[str, dict[str, Any]]) -> None:
    """A company-list observation contradicted within a week by a symbol-directory
    observation of the same ticker (another venue or another security) is CONFLICTING."""
    obs = [e for e in events.values() if e["event_type"] == "LISTING_OBSERVED"]
    for c in obs:
        if "companylist" not in c["code"]:
            continue
        for s in obs:
            if "symdir" not in s["code"] or s["ticker"] != c["ticker"]:
                continue
            if abs((s["effective_date"] - c["effective_date"]).days) <= 7 and (
                s["venue"] != c["venue"]
            ):
                c["resolution_status"] = "CONFLICTING"
                c["confidence"] = "LOW"
                family = s["evidence"][0]["artifact"].split("/")[1]
                c["explanation"] += (
                    f" Contradicted by the {family} capture of "
                    f"{s['effective_date']} ({s['ticker']} on {s['venue']}); the symbol "
                    "directory takes precedence."
                )


# ---------------------------------------------------------------------- build
def _json(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(type(obj))


def build(store: x4.Store) -> dict[str, Any]:
    """The ledger document (deterministic for a given evidence manifest)."""
    led = Ledger(store)
    funds = fund_classes(store)
    unparsed = _notice_events(led, funds)
    _named(led, funds)
    _cross_source(led.events)
    _derive_transfers(led)
    _named_refinements(led)
    events = sorted(led.events.values(), key=lambda e: e["code"])
    issues = validate(events, led.securities)
    marks = {i["code"]: i for i in issues if i.get("mark")}
    for e in events:
        if e["code"] in marks and e["resolution_status"] == "CONFIRMED":
            e["resolution_status"] = marks[e["code"]]["mark"]
            e["explanation"] += f" Validation: {marks[e['code']]['issue']}."
    for e in events:
        e["evidence"] = sorted(e["evidence"], key=lambda r: (r["artifact"], r["locator"]))
        e["evidence_sha256"] = _sha(e["evidence"])
    for s in led.securities.values():
        s["evidence"] = sorted(s["evidence"], key=lambda r: json.dumps(r, sort_keys=True))
    unresolved_issues = [i for i in issues if not i.get("mark")]
    if unresolved_issues:
        raise ValueError(f"ledger fails validation: {unresolved_issues[:5]}")
    entities = sorted(led.entities.values(), key=lambda e: e["entity_key"])
    securities = sorted(led.securities.values(), key=lambda s: s["security_key"])
    body = json.loads(
        json.dumps(
            {"entities": entities, "securities": securities, "events": events},
            default=_json,
            sort_keys=True,
        )
    )
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in body["events"]:
        counts[e["event_type"]][e["resolution_status"]] += 1
    return {
        "kind": "x42_security_lifecycle_ledger",
        "decision": "D-246",
        "set_code": SET_CODE,
        "evidence_manifest_sha256": hashlib.sha256(store.manifest_path.read_bytes()).hexdigest(),
        "content_sha256": content_sha256(body["events"]),
        **body,
        "summary": {
            "entities": len(entities),
            "securities": len(securities),
            "events": len(events),
            "by_type_and_status": {k: dict(sorted(v.items())) for k, v in sorted(counts.items())},
            "notices_without_parsed_rule": len(unparsed),
            "validation_marks": len(marks),
        },
        "unparsed_rule_notices": unparsed,
    }


EVENT_FIELDS = (
    "code",
    "revision",
    "security_key",
    "event_type",
    "effective_date",
    "effective_precision",
    "not_before",
    "not_after",
    "filing_date",
    "ticker",
    "from_ticker",
    "to_ticker",
    "venue",
    "from_venue",
    "to_venue",
    "security_type",
    "reason",
    "counterparty_entity_key",
    "confidence",
    "resolution_status",
    "explanation",
    "evidence",
    "evidence_sha256",
)


def content_sha256(events: list[dict[str, Any]]) -> str:
    """Canonical hash of the events (order-independent; dates as ISO strings)."""
    rows = sorted(
        ({k: e[k] for k in EVENT_FIELDS if k in e} for e in events),
        key=lambda r: (r["code"], r["revision"]),
    )
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":"), default=_json).encode()
    ).hexdigest()


def write(doc: dict[str, Any], path: Path) -> str:
    """Write the ledger once (an identical rebuild is accepted, a different one refused)."""
    body = json.dumps(doc, indent=1, sort_keys=True, default=_json)
    if path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise FileExistsError(
                f"{path} exists with different content: ledgers are never replaced"
            )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="\n")
    return hashlib.sha256(body.encode()).hexdigest()


# ---------------------------------------------------------------------- reconstruction
def reconstruct(doc: dict[str, Any], security_key: str) -> dict[str, Any]:
    """A security's timeline from its events: observed tickers and venues, transfers,
    ticker changes, classification and end, all with their date precision."""
    evs = sorted(
        (e for e in doc["events"] if e["security_key"] == security_key),
        key=lambda e: (
            e["effective_date"] or e["not_before"] or e["filing_date"] or "9999",
            e["code"],
        ),
    )
    listings = [
        (e["effective_date"], e["ticker"], e["venue"])
        for e in evs
        if e["event_type"] == "LISTING_OBSERVED" and e["resolution_status"] == "CONFIRMED"
    ]
    end = next(
        (e for e in evs if e["event_type"] in TERMINAL and e["resolution_status"] == "CONFIRMED"),
        None,
    )
    return {
        "security_key": security_key,
        "listings": listings,
        "venues": sorted({v for _, _, v in listings}),
        "tickers": sorted({t for _, t, _ in listings}),
        "transfers": [
            (
                e["from_venue"],
                e["to_venue"],
                e["not_before"],
                e["not_after"],
                e["resolution_status"],
            )
            for e in evs
            if e["event_type"] == "EXCHANGE_TRANSFER"
        ],
        "ticker_changes": [
            (e["from_ticker"], e["to_ticker"], e["not_before"], e["not_after"])
            for e in evs
            if e["event_type"] == "TICKER_CHANGE"
        ],
        "classification": [
            (e["security_type"], e["resolution_status"])
            for e in evs
            if e["event_type"] in ("CLASSIFICATION", "RECLASSIFICATION")
        ],
        "ended": None
        if end is None
        else {
            "type": end["event_type"],
            "reason": end["reason"],
            "filing_date": end["filing_date"],
            "not_before": end["not_before"],
            "not_after": end["not_after"],
        },
        "removals": [
            (e["venue"], e["filing_date"], e["reason"])
            for e in evs
            if e["event_type"] == "VENUE_REMOVAL"
        ],
    }


# ---------------------------------------------------------------------- registration
def _date(v: Any) -> date | None:
    return None if v is None else date.fromisoformat(v)


def register(factory: sessionmaker[Session], path: Path) -> dict[str, Any]:
    """Load the ledger once, in one transaction; the set's hash is recomputed from the rows."""
    raw = path.read_bytes()
    doc = json.loads(raw)
    if content_sha256(doc["events"]) != doc["content_sha256"]:
        raise ValueError("the ledger's events do not hash to its content_sha256")
    with factory() as session, session.begin():
        s = SecurityLifecycleSetRecord
        if session.scalar(select(s.id).where(s.code == doc["set_code"])) is not None:
            raise ValueError(f"{doc['set_code']} is already registered (append-only)")
        session.add_all(SecurityEntityRecord(**e, created_by=CREATED_BY) for e in doc["entities"])
        session.flush()
        session.add_all(
            SecurityIdentityRecord(**x, created_by=CREATED_BY) for x in doc["securities"]
        )
        session.flush()
        rows = []
        for e in doc["events"]:
            values = {k: e[k] for k in EVENT_FIELDS}
            for k in ("effective_date", "not_before", "not_after", "filing_date"):
                values[k] = _date(values[k])
            rows.append(SecurityLifecycleEventRecord(**values, created_by=CREATED_BY))
        session.add_all(rows)
        session.flush()
        stored = [
            {
                k: (getattr(r, k).isoformat() if isinstance(getattr(r, k), date) else getattr(r, k))
                for k in EVENT_FIELDS
            }
            for r in session.scalars(select(SecurityLifecycleEventRecord))
        ]
        sha = content_sha256(stored)
        if sha != doc["content_sha256"]:
            raise ValueError("the stored events do not hash to the ledger's content_sha256")
        session.add(
            SecurityLifecycleSetRecord(
                code=doc["set_code"],
                version=1,
                description="Security lifecycle ledger from the X4.1 evidence (D-246)",
                rule={
                    "precedence": "SEC filings > symbol directories > company lists",
                    "terminal": "12d2-2(a)(1)-(4) only; (b), (c) are venue removals",
                    "dates": "effective only when stated; bounds otherwise; unknown stays unknown",
                },
                members=[{"code": r.code, "revision": r.revision} for r in rows],
                content_sha256=sha,
                ledger_sha256=hashlib.sha256(raw).hexdigest(),
                evidence_manifest_sha256=doc["evidence_manifest_sha256"],
                created_by=CREATED_BY,
            )
        )
    return {
        "set": doc["set_code"],
        "content_sha256": sha,
        "events": len(rows),
        "entities": len(doc["entities"]),
        "securities": len(doc["securities"]),
    }


__all__ = [
    "NAMED",
    "SET_CODE",
    "build",
    "class_label",
    "content_sha256",
    "parse_removal",
    "reconstruct",
    "register",
    "validate",
    "write",
]
