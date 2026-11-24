"""X4.6 (D-252): final validation of the historical research data (X4.1 to X4.5).

Offline and read-only. Every input is pinned by SHA-256 and every evidence artifact it reads is
checked against its store manifest. Nothing is fetched, nothing is written to a database, and no
earlier artifact is changed. The result is one file, written once
(``config/simulation/lifecycle/x46_validation_v1.json.gz``). It holds, for every membership slot
REDACTED_FOR_GITHUB
slot's data, and the data range that research may use.

Statuses:

* ``PASS``: every check passed and no restriction applies.
* ``RESTRICTED``: every check passed; named sessions, dates or uses must not be used (listed).
* ``EXCLUDED``: not usable for research, with the reason (unresolved identity, membership invalid
  at the formation, an X1 asset exclusion).
* ``FAIL``: a check failed; never usable until resolved.

Retrieval is by membership slot, or by ticker and session (:class:`Lookup`). A security key that
covers more than one listing is never a lookup key on its own.
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
import html
import itertools
import json
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.backtesting.data import sessions_between
from app.research import exclusions, exclusions_x2
from app.research import graph_v2 as g2
from app.research import ledger_v2 as l2
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import x4_evidence as x4
from app.research import x43_evidence as x43e
from app.research import x45_data as x45d

CODE = "X46_VALIDATION_V1"
DECISION = "D-252"
VALIDATION_PATH = lc.OUT / "x46_validation_v1.json.gz"

# Every earlier X4 artifact, as reported (preserved byte-for-byte).
PINNED: dict[str, str] = {
    "config/simulation/lifecycle/x42_ledger_v1.json": (
        "a6b93d376d0de3700ad9ee1df89c8af1f3d5b26a5885614b750bb02bc67d10fe"
    ),
    "config/simulation/lifecycle/x43_identity_graph_v1.json.gz": (
        "eceee9dfe8af0049db8470e54cc92accad612266a4f1933a8d74c7b9289c297e"
    ),
    "config/simulation/lifecycle/x431_ledger_v2.json.gz": (
        "42f5e7d49b9755a95e1f8f2a8d084fe12b5d75ea1285d3ce7cb60bd5a472b8dd"
    ),
    "config/simulation/lifecycle/x432_identity_graph_v2.json.gz": (
        "86ae132cc94f16c1352405d80e4d7021b362cce9e165cabce47933c33630aaf3"
    ),
    "config/simulation/lifecycle/x44_historical_universe_v1.json.gz": (
        "6ad21763c8b5dc1ddaf4c07161f6fafcd5b74e2379491b2bfc10f57aabab26d7"
    ),
    "config/simulation/lifecycle/x45_member_data_v1.json.gz": (
        "9778c5f2802abfe90d80bbb9d94880f79675131120853e87cad5e13c82a511f4"
    ),
    "config/simulation/evidence/x4/manifest.json": (
        "8b375e672b7a4b6c9a0d22eccf82472d3b14f0faf3692dd99c56143db310e5ba"
    ),
    "config/simulation/evidence/x43/manifest.json": (
        "4a096166116d86409367aff8fefb4e66367e48cacb5a538d74e3d268f3b8d4d0"
    ),
    "config/simulation/evidence/x432/manifest.json": (
        "2b9d6d30fb7daf0687c1865b664c05b7a9a98749eda48deade8a580679fced5e"
    ),
    "config/simulation/evidence/x45/manifest.json": (
        "da4e8f1433b2af54afe030e747d04bdd1a826943adccae60c7abb87df580dbbd"
    ),
}
DATA_CONTENT_SHA256 = "f5fa06f0eb8fe4f0a7e2097747933332e7fac764f05b9461b65fb0961e8e13eb"

PASS, RESTRICTED, EXCLUDED, FAIL = "PASS", "RESTRICTED", "EXCLUDED", "FAIL"
OK, BAD, NA, WARN = "PASS", "FAIL", "NOT_APPLICABLE", "WARN"
LINEAGE_MIN = 20  # identical sessions for a slot's data to be (part of) a provider asset's series
SPLIT_TOLERANCE = 0.25  # as X4.5 (D-251)
FACTOR_STEP = 0.02  # a provider split-adjustment factor change larger than rounding
LARGE_DISTRIBUTION = 0.10  # a cash distribution of at least 10% of the prior close
ISSUER_FORM25_DAYS = 10  # 17 CFR 240.12d2-2(d)(1): an issuer's Form 25 is effective 10 days on
NOTICE_SLACK_DAYS = 14
DEREGISTRATION_FORMS = ("15-12B", "15-12G", "15-15D")
TERMS = re.compile(r"converted|right to receive|in cash|exchanged for", re.I)
CUT_AT_FILING = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection

# What each use of the data may rely on (part of the artifact; D-252).
RETURN_USES: dict[str, dict[str, str]] = {
    "price_strategy": {
        "verdict": "SAFE_WITH_RESTRICTIONS",
        "series": "raw close, adjusted only by the verified split factors (D-251)",
        "rule": "use only sessions inside the slot's usable range with a recorded trade; skip "
        "every listed unusable return date, X2 session or span, and non-executable session; "
        "never compound across a missing session",
        "not_safe": "the provider's split-adjusted close where its factor changes on a date "
        "that is not a verified split (listed per slot)",
    },
    "total_return": {
        "verdict": "NOT_READY",
        "series": "raw close plus dividends as dated cash flows (ex-date, rate)",
        "rule": "no total-return series exists. A reinvestment convention, the treatment of "
        "REDACTED_FOR_GITHUB"
        "values (X1/X2 events) must be specified by X5. No factor is invented here.",
        "not_safe": "the provider's split-adjusted close as a total-return proxy; it carries "
        "no dividends",
    },
    "portfolio_accounting": {
        "verdict": "NOT_READY",
        "series": "raw close for marking, dividends credited at the ex-date rate",
        "rule": "a position cannot be marked after the slot's last recorded trade. Terminal "
        "proceeds of exits are not in the dataset (the removal notice's terms are quoted where "
        "present); dividend pay dates are not captured. X5 must specify both.",
        "not_safe": "carrying the last close forward after an exit or a stop in trading",
    },
}


# ---------------------------------------------------------------------- evidence reading
class Evidence:
    """Reads store artifacts, checking each against its manifest hash once."""

    def __init__(self, stores: dict[str, x4.Store]) -> None:
        self.stores = stores
        self.checked: dict[str, str] = {}
        self.failures: list[str] = []
        self.cache: dict[str, Any] = {}

    def read(self, name: str, rel: str) -> Any:
        key = f"{name}:{rel}"
        if key not in self.cache:
            store = self.stores[name]
            raw = store.path(rel).read_bytes()
            got = hashlib.sha256(raw).hexdigest()
            if got != store.entries[rel]["sha256"]:
                self.failures.append(key)
            self.checked[key] = got
            if rel.endswith(".jsonl.gz"):
                lines = gzip.decompress(raw).decode("utf-8").splitlines()
                self.cache[key] = [json.loads(line) for line in lines if line.strip()]
            else:
                self.cache[key] = x4.read_json(store.path(rel))
        return self.cache[key]

    def raw_text(self, name: str, rel: str) -> str:
        key = f"{name}:{rel}"
        if key not in self.cache:
            store = self.stores[name]
            raw = store.path(rel).read_bytes()
            got = hashlib.sha256(raw).hexdigest()
            if got != store.entries[rel]["sha256"]:
                self.failures.append(key)
            self.checked[key] = got
            data = gzip.decompress(raw) if rel.endswith(".gz") else raw
            self.cache[key] = data.decode("latin-1")
        return str(self.cache[key])

    def keys(self, name: str, prefix: str) -> list[str]:
        return sorted(k for k in self.stores[name].entries if k.startswith(prefix))


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight_hashes(root: Path = Path(".")) -> dict[str, Any]:
    got = {p: _sha_file(root / p) for p in PINNED}
    return {
        "artifacts": {p: {"expected": PINNED[p], "found": got[p]} for p in PINNED},
        "ok": all(got[p] == PINNED[p] for p in PINNED),
    }


# ---------------------------------------------------------------------- sessions
def _sessions(lo: str, hi: str) -> list[str]:
    if hi < lo:
        return []
    days = sessions_between(date.fromisoformat(lo), date.fromisoformat(hi))
    return [s.day.isoformat() for s in days]


def _day(d: str, days: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


def _rows(s: dict[str, Any]) -> list[dict[str, Any]]:
    cols = s["bars"]["columns"]
    return [dict(zip(cols, r, strict=True)) for r in s["bars"]["rows"]]


def _source_ticker(source: str) -> str:
    m = re.search(r"\(([^)]+)\)\s*$", source or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------------- reconciliation
def reconcile(dataset: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    """Slots and securities: the dataset against the universe, every count explained."""
    members: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for key, f in universe["formations"].items():
        for r in f["records"]:
            if r["decision"] == "MEMBER":
                members[f"{key}|{r['ticker']}"] = r
            elif r["decision"] == "MEMBER_IDENTITY_UNRESOLVED":
                unresolved.append(f"{key}|{r['ticker']}")
    slots = {s["slot"]: s for s in dataset["slots"]}
    excluded = sorted(e["slot"] for e in dataset["excluded_unresolved_slots"])
    security_keys = {s["security_key"] for s in slots.values()}
    ledger_keys = {s["ledger_key"] for s in slots.values()}
    by_ledger: dict[str, set[str]] = defaultdict(set)
    for s in slots.values():
        by_ledger[s["ledger_key"]].add(s["security_key"])
    merged = {k: sorted(v) for k, v in by_ledger.items() if len(v) > 1}
    shared = shared_keys(dataset["slots"])
    extra_listings = sum(
        max(len(v) for v in per_formation.values()) - 1 for per_formation in shared.values()
    )
    return {
        "universe_members": len(members),
        "universe_unresolved": len(unresolved),
        "dataset_slots": len(slots),
        "dataset_excluded_unresolved": len(excluded),
        "slots_match": sorted(members) == sorted(slots) and sorted(unresolved) == excluded,
        "missing_from_dataset": sorted(set(members) - set(slots)),
        "not_in_universe": sorted(set(slots) - set(members)),
        "security_keys": len(security_keys),
        "ledger_securities": len(ledger_keys),
        "listed_securities": len(ledger_keys) + extra_listings,
        "security_keys_merged_by_ledger": merged,
        "keys_covering_several_listings": {
            k: {f: sorted(t) for f, t in v.items()} for k, v in sorted(shared.items())
        },
        "explanation": (
            f"{len(security_keys)} is the number of distinct X4.4 security keys among confirmed "
            "members (the X4.5 preflight count). Ledger V2 maps two of them, one issuer's class "
            "keys worded differently, to one security, so the dataset reports "
            f"{len(ledger_keys)} Ledger securities. Three keys each cover two listings that trade "
            "at the same time, so the number of distinct listed securities is "
            f"{len(ledger_keys) + extra_listings}. No slot changes; lookups use the slot or the "
            "ticker."
        ),
    }


def shared_keys(slots: Iterable[dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    """Security keys held by two or more slots of one formation: distinct listings that share
    a key (a ticker change of one security never appears twice in one formation)."""
    by: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for s in slots:
        by[s["security_key"]][f"{s['pool']}|{s['formation']}"].add(s["ticker"])
    return {
        k: {f: sorted(t) for f, t in v.items()}
        for k, v in by.items()
        if any(len(t) > 1 for t in v.values())
    }


# ---------------------------------------------------------------------- lookup
class AmbiguousLookupError(LookupError):
    """The key names more than one listing; use the slot or the ticker."""


class Lookup:
    """Data retrieval by membership slot, or by ticker and session. A security key is a lookup
    key only when it names one listing."""

    def __init__(self, slots: Iterable[dict[str, Any]]) -> None:
        self.slots: dict[str, dict[str, Any]] = {}
        self.ticker_days: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.by_key: dict[str, list[str]] = defaultdict(list)
        for s in slots:
            self.slots[s["slot"]] = s
            self.by_key[s["security_key"]].append(s["slot"])
            for r in _rows(s):
                self.ticker_days[(_source_ticker(r["source"]) or s["ticker"], r["session"])].append(
                    s["slot"]
                )
        self.shared = shared_keys(self.slots.values())

    def slot(self, slot: str) -> dict[str, Any]:
        if slot not in self.slots:
            raise LookupError(f"no slot {slot}")
        return self.slots[slot]

    def by_ticker(self, ticker: str, session: str, pool: str | None = None) -> dict[str, Any]:
        found = [
            x for x in self.ticker_days.get((ticker, session), []) if not pool or x.startswith(pool)
        ]
        if not found:
            raise LookupError(f"no slot holds {ticker} on {session}")
        if len(found) > 1:
            raise AmbiguousLookupError(f"{ticker} on {session}: {sorted(found)}")
        return self.slots[found[0]]

    def by_security(self, key: str) -> list[dict[str, Any]]:
        """Every slot of a security key, all formations (several listings possible)."""
        return [self.slots[x] for x in sorted(self.by_key.get(key, []))]

    def unique_security(self, key: str, pool: str, formation: str) -> dict[str, Any]:
        found = [
            self.slots[x]
            for x in self.by_key.get(key, [])
            if self.slots[x]["pool"] == pool and self.slots[x]["formation"] == formation
        ]
        if not found:
            raise LookupError(f"no slot for {key} in {pool} {formation}")
        if len(found) > 1:
            raise AmbiguousLookupError(
                f"{key} covers {len(found)} listings in {pool} {formation}: "
                f"{sorted(x['ticker'] for x in found)}; use the slot or the ticker"
            )
        return found[0]


def retrieval_selftest(lk: Lookup) -> dict[str, Any]:
    """Every bar retrieved back by slot and by (ticker, session); every security key either
    unique per formation or refused as ambiguous."""
    by_ticker_ok = ambiguous = 0
    wrong: list[str] = []
    for (ticker, session), owners in sorted(lk.ticker_days.items()):
        try:
            got = lk.by_ticker(ticker, session)
            by_ticker_ok += got["slot"] == owners[0]
        except AmbiguousLookupError:
            ambiguous += 1
            wrong.append(f"{ticker} {session}")
    refused = []
    for key, slots in sorted(lk.by_key.items()):
        for pf in sorted({lk.slots[x]["pool"] + "|" + lk.slots[x]["formation"] for x in slots}):
            pool, formation = pf.split("|")
            try:
                lk.unique_security(key, pool, formation)
            except AmbiguousLookupError:
                refused.append(f"{key} {pf}")
    return {
        "slots": len(lk.slots),
        "ticker_sessions": len(lk.ticker_days),
        "ticker_sessions_resolved": by_ticker_ok,
        "ticker_sessions_ambiguous": ambiguous,
        "ambiguous_examples": wrong[:10],
        "security_keys": len(lk.by_key),
        "security_lookups_refused_as_ambiguous": refused,
        "ok": ambiguous == 0 and by_ticker_ok == len(lk.ticker_days),
    }


# ---------------------------------------------------------------------- notices
def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _cik(x: str | None) -> int | None:
    digits = re.sub(r"\D", "", x or "")
    return int(digits) if digits else None


def load_notices(ev: Evidence) -> dict[str, Any]:
    """Every stored removal notice (X4.1 index and texts), and the EDGAR deregistrations
    (15-12B/15-12G/15-15D) from the stored full indexes."""
    index = ev.read("x4", "derived/removal_notices.json.gz")
    texts: dict[str, str] = {}
    for rel in ev.keys("x4", "sec/form25/"):
        for r in ev.read("x4", rel):
            acc = r["path"].rsplit("/", 1)[1].removesuffix(".txt")
            texts.setdefault(acc, r.get("text") or "")
    notices: dict[str, dict[str, Any]] = {}
    for r in index:
        acc = r["path"].rsplit("/", 1)[1].removesuffix(".txt")
        n = notices.setdefault(
            acc,
            {
                "accession": acc,
                "form": r["form"],
                "filed": r["filed"],
                "class": r.get("class"),
                "rule": r.get("rule"),
                "ciks": set(),
                "subject": r.get("issuer_name") or r.get("company"),
            },
        )
        for c in (r.get("cik"), r.get("issuer_cik")):
            if _cik(c) and _cik(c) != _cik(r.get("exchange_cik")):
                n["ciks"].add(_cik(c))
    by_cik: dict[int, list[str]] = defaultdict(list)
    for acc, n in notices.items():
        for c in n["ciks"]:
            by_cik[c].append(acc)
    dereg: dict[int, list[dict[str, str]]] = defaultdict(list)
    line_re = re.compile(
        r"^(?P<form>15-1[25][BGD])\s{2,}(?P<name>.+?)\s{2,}(?P<cik>\d+)\s{2,}"
        r"(?P<date>\d{4}-\d{2}-\d{2})\s{2,}(?P<file>\S+)\s*$"
    )
    for rel in ev.keys("x4", "sec/full-index/"):
        for line in ev.raw_text("x4", rel).splitlines():
            if line.startswith("15-"):
                m = line_re.match(line)
                if m and m["form"] in DEREGISTRATION_FORMS:
                    dereg[int(m["cik"])].append(
                        {"form": m["form"], "filed": m["date"], "path": m["file"]}
                    )
    return {"notices": notices, "texts": texts, "by_cik": by_cik, "dereg": dereg}


_BAD_CLASS = re.compile(r"preferred|\bnotes?\b|debenture|warrant|\brights?\b|bond", re.I)


def _class_fits(security_key: str, cls: str | None) -> bool:
    if not cls:
        return True
    if _BAD_CLASS.search(cls):
        return False
    label = security_key.split("|", 1)[1] if "|" in security_key else ""
    if "depositary" in cls.lower() and not label.startswith("ADS"):
        return False
    units_ok = label.startswith(("LP_UNIT", "UNIT", "UNSPECIFIED", "BENEFICIAL"))
    return not ("unit" in cls.lower() and not units_ok)


def terms_text(text: str) -> list[str]:
    """The notice's own sentences on what holders received (quoted, at most two)."""
    body = _clean(text)
    i = body.find("NOTIFICATION OF THE REMOVAL")
    body = body[i:] if i >= 0 else body
    out = [s.strip()[:300] for s in re.split(r"(?<=\.)\s+", body) if TERMS.search(s) and "$" in s]
    return out[:2]


def exit_evidence(
    s: dict[str, Any], last_traded: str | None, nt: dict[str, Any], entity_name: str | None
) -> dict[str, Any]:
    """The removal notices behind a slot that stops before its window's end: by the issuer's
    CIK, else (no notice under the CIK) by the issuer's name in a notice's text around the last
    trade (MEDIUM). Deregistrations by the issuer's CIK corroborate."""
    lo, hi = s["window"]
    cik = _cik(s["entity_key"])
    found = []
    for acc in nt["by_cik"].get(cik or -1, []):
        n = nt["notices"][acc]
        if _day(lo, -NOTICE_SLACK_DAYS) <= n["filed"] <= _day(
            hi, NOTICE_SLACK_DAYS
        ) and _class_fits(s["security_key"], n["class"]):
            found.append((n, "ISSUER_CIK"))
    if not found and last_traded and entity_name:
        want = rs.nkey(entity_name)
        for acc, n in sorted(nt["notices"].items()):
            if not (last_traded <= n["filed"] <= _day(last_traded, NOTICE_SLACK_DAYS)):
                continue
            if not _class_fits(s["security_key"], n["class"]):
                continue
            if want and want in rs.nkey(_clean(nt["texts"].get(acc, ""))):
                found.append((n, "ISSUER_NAME_IN_NOTICE_TEXT"))
    notices = []
    for n, how in sorted(found, key=lambda x: (x[0]["filed"], x[0]["accession"])):
        notices.append(
            {
                "accession": n["accession"],
                "form": n["form"],
                "filed": n["filed"],
                "class": n["class"],
                "subject": n["subject"],
                "matched_by": how,
                "terms": terms_text(nt["texts"].get(n["accession"], "")),
            }
        )
    dereg = [d for d in nt["dereg"].get(cik or -1, []) if lo <= d["filed"] <= _day(hi, 60)]
    return {"notices": notices, "deregistrations": sorted(dereg, key=lambda d: d["filed"])}


def listing_last_session(notice: dict[str, Any], window: list[str]) -> str | None:
    """The last session the security could trade on its exchange per its removal notice: before
    an exchange's 25-NSE (filed after the suspension), or up to the effective date of an issuer's
    Form 25 (10 days after filing)."""
    if notice["form"].startswith("25-NSE"):
        days = _sessions(window[0], _day(notice["filed"], -1))
    else:
        days = _sessions(window[0], _day(notice["filed"], ISSUER_FORM25_DAYS))
    return days[-1] if days else None


# ---------------------------------------------------------------------- lineage (X1/X2)
def provider_lineage(ev: Evidence) -> tuple[dict[tuple[str, str, int], set[str]], dict[str, Any]]:
    idx: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for rel in ev.keys("x43", "db/provider_bars/"):
        for asset, rows in ev.read("x43", rel).items():
            for sess, close, _split, vol in rows:
                if vol and float(vol) > 0:
                    idx[(sess, f"{float(close):.4f}", round(float(vol)))].add(asset)
    ids = ev.read("x43", "db/provider_identities.json.gz")
    return idx, {r["asset_id"]: r for r in ids}


def slot_lineage(s: dict[str, Any], idx: dict[tuple[str, str, int], set[str]]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for r in _rows(s):
        if r["volume"] and float(r["volume"]) > 0:
            for a in idx.get(
                (r["session"], f"{float(r['close']):.4f}", round(float(r["volume"]))), ()
            ):
                c[a] += 1
    return {a: n for a, n in c.most_common() if n >= LINEAGE_MIN}


def x1_assets(identities: dict[str, Any]) -> dict[str, list[str]]:
    """X1 labels (D-240) to the provider assets carrying them (their V4/V5 identities)."""
    out: dict[str, list[str]] = {}
    for label, _event, _basis in exclusions.EXCLUDED:
        out[label] = sorted(a for a, r in identities.items() if r["symbol"] == label)
    return out


# ---------------------------------------------------------------------- formation checks
def formation_checks(
    universe: dict[str, Any], ev: Evidence, ledger_by_key: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """No look-ahead: membership inputs end at the formation; metrics agree with X4.1's stored
    derivation; ranks and the cut-off are consistent; the holding window starts after the
    formation; a member removed before its formation is identified."""
    out: dict[str, Any] = {}
    removed: dict[str, dict[str, Any]] = {}
    for key, f in sorted(universe["formations"].items()):
        formation = f["formation"]
        derived = ev.read("x4", f"derived/{formation}/formation_metrics.json.gz")
        size = 500 if f["pool"] == "STOCK" else 150
        top = [r for r in f["records"] if r["decision"] in ("MEMBER", "MEMBER_IDENTITY_UNRESOLVED")]
        below = [r for r in f["records"] if r["decision"] == "BELOW_CUTOFF"]
        mism = []
        for r in top:
            m = derived.get(r["ticker"])
            if (
                not m
                or m["traded_bars"] != r["traded_bars"]
                or m["last"] != r["last_bar"]
                or abs(m["mdv"] - r["median_dollar_volume"]) > 0.01
            ):
                mism.append(r["ticker"])
        after = [r["ticker"] for r in top if r["last_bar"] > formation or r["reasons"]]
        ranks = sorted(r["rank"] for r in top)
        lo, hi = f["window"]
        sess = _sessions(_day(formation, 1), _day(formation, 10))
        min_top = min(r["median_dollar_volume"] for r in top)
        max_below = max((r["median_dollar_volume"] for r in below), default=0.0)
        for r in top:
            if r["decision"] != "MEMBER":
                continue
            ends = [
                e
                for e in ledger_by_key.get((r.get("lifecycle") or {}).get("ledger_key") or "", [])
                if e["event_type"] in lc.TERMINAL
                and e["resolution_status"] == rs.CONFIRMED
                and e["filing_date"]
                and e["filing_date"] <= formation
                and e["filing_date"] >= _day(formation, -366)
            ]
            if ends and r["last_bar"] < formation:
                removed[f"{key}|{r['ticker']}"] = {
                    "removal": ends[0]["code"],
                    "filed": ends[0]["filing_date"],
                    "last_bar_before_formation": r["last_bar"],
                    "formation": formation,
                }
        out[key] = {
            "members_and_unresolved": len(top),
            "inputs_after_formation": after,
            "metrics_differ_from_x41_derivation": mism,
            "ranks_contiguous": ranks == list(range(1, len(top) + 1)) and len(top) == size,
            "cutoff_consistent": min_top >= max_below,
            "window_starts_after_formation": bool(sess) and lo == sess[0] and lo > formation,
            "window": [lo, hi],
            "ok": not after
            and not mism
            and ranks == list(range(1, len(top) + 1))
            and min_top >= max_below
            and bool(sess)
            and lo == sess[0],
        }
    return {"formations": out, "removed_before_formation": removed}


def formation_bars(
    ev: Evidence, formation: str, tickers: set[str]
) -> dict[str, dict[str, tuple[float, float]]]:
    """The formation-window sweep bars (traded only) for some tickers, as X4.4 read them."""
    out: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
    for rel in ev.keys("x4", f"provider/sweeps/{formation}/"):
        if rel.split("/")[3] == "history":
            continue
        doc = ev.read("x4", rel)
        for req in doc["requests"]:
            for pg in req["pages"]:
                body = pg.get("body")
                if not isinstance(body, dict):
                    continue
                for sym, bars in (body.get("bars") or {}).items():
                    if sym in tickers:
                        for b in bars:
                            if float(b.get("v") or 0) > 0:
                                out[sym][b["t"][:10]] = (float(b["c"]), float(b["v"]))
    return out


def ranking_robustness(
    r: dict[str, Any], bars: dict[str, tuple[float, float]], session: str, splice: bool,
    next_mdv: float, rule: dict[str, Any],
) -> dict[str, Any]:  # fmt: skip
    """The member's eligibility and rank with the contaminated session left out (a splice:
    every session before it left out), against the first non-member's median."""
    keep = {d: v for d, v in bars.items() if (d >= session if splice else d != session)}
    dv = [c * v for c, v in keep.values()]
    mdv = statistics.median(dv) if dv else 0.0
    eligible = len(keep) >= rule["min_bars"] and mdv >= float(rule["min_median_dollar_volume"])
    return {
        "session": session,
        "splice": splice,
        "sessions_kept": len(keep),
        "median_dollar_volume_kept": round(mdv, 2),
        "first_non_member_median": round(next_mdv, 2),
        "still_member": eligible and mdv > next_mdv,
        "recorded_median": r["median_dollar_volume"],
    }


# ---------------------------------------------------------------------- per-slot checks
def check_quality(rows: list[dict[str, Any]], lo: str, end: str) -> dict[str, Any]:
    cal = set(_sessions(lo, end))
    seen: set[str] = set()
    names = ("invalid_ohlc", "non_positive", "duplicates", "off_calendar", "outside_range")
    bad: dict[str, list[str]] = {k: [] for k in (*names, "vwap_outside_range")}
    for r in rows:
        d = r["session"]
        if d in seen:
            bad["duplicates"].append(d)
        seen.add(d)
        if d not in cal:
            bad["off_calendar" if lo <= d <= end else "outside_range"].append(d)
        o, h, low, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        if min(o, h, low, c) <= 0:
            bad["non_positive"].append(d)
        if not (low <= min(o, c) <= max(o, c) <= h):
            bad["invalid_ohlc"].append(d)
        if r["vwap"] is not None and float(r["volume"] or 0) > 0:
            vw = float(r["vwap"])
            if not (low * 0.999 <= vw <= h * 1.001):
                bad["vwap_outside_range"].append(d)
    hard = [k for k in bad if k != "vwap_outside_range" and bad[k]]
    return {
        "result": BAD if hard else (WARN if bad["vwap_outside_range"] else OK),
        **{k: v[:20] for k, v in bad.items()},
        "counts": {k: len(v) for k, v in bad.items()},
    }


def check_splits(s: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute each split record's raw ratio from the bars; find every change of the provider's
    split-adjustment factor and explain it."""
    close = {r["session"]: float(r["close"]) for r in rows}
    days = sorted(close)
    recs = s["splits"].get("records", [])
    mism = []
    verified: set[str] = set()
    artifacts: set[str] = set()
    fractional: list[dict[str, Any]] = []
    for rec in recs:
        ex = rec["ex_date"]
        i = bisect.bisect_left(days, ex)
        if i == 0 or i >= len(days) or days[i] != ex:
            mism.append({"ex_date": ex, "why": "no bar on the ex-date or before it"})
            continue
        raw = close[ex] / close[days[i - 1]]
        # As X4.5 (D-251): the record's ratio is new over old shares; the raw close moves by
        # its inverse.
        ok = abs(raw * float(rec["record_ratio"]) - 1) < SPLIT_TOLERANCE
        if ok != (rec["verdict"] == "VERIFIED_BY_RAW_PRICES") or abs(raw - rec["raw_ratio"]) > 1e-3:
            mism.append({"ex_date": ex, "recomputed_raw_ratio": round(raw, 4)})
        (verified if ok else artifacts).add(ex)
        if ok and not _whole_ratio(rec):
            fractional.append({"ex_date": ex, "new_rate": rec.get("new_rate"),
                               "old_rate": rec.get("old_rate")})  # fmt: skip
    seams = {x["date"] for x in s["splits"].get("unrecorded_factor_changes", [])}
    changes = []
    prev = None
    for r in rows:
        if r["split_close"] is None or float(r["close"]) <= 0:
            continue
        f = float(r["split_close"]) / float(r["close"])
        if prev is not None and abs(f / prev[1] - 1) > FACTOR_STEP:
            d = r["session"]
            why = (
                "VERIFIED_SPLIT" if d in verified else "PROVIDER_ARTIFACT_APPLIED" if d in artifacts
                else "MAPPING_SEAM" if d in seams else "UNEXPLAINED"
            )  # fmt: skip
            changes.append({"date": d, "factor_before": round(prev[1], 6),
                            "factor_after": round(f, 6), "cause": why})  # fmt: skip
        prev = (r["session"], f)
    inconsistent = [c["date"] for c in changes if c["cause"] != "VERIFIED_SPLIT"]
    return {
        "result": BAD if mism else OK,
        "records": len(recs),
        "verified": sorted(verified),
        "artifacts": sorted(artifacts),
        "mismatches": mism,
        "provider_factor_changes": changes,
        "provider_split_series_unusable_at": inconsistent,
        "fractional_verified_records": fractional,
    }


def _whole_ratio(rec: dict[str, Any]) -> bool:
    """A split of whole shares (n for 1, or 1 for n). A fractional ratio that the prices confirm
    REDACTED_FOR_GITHUB"""
    new, old = float(rec.get("new_rate") or 1), float(rec.get("old_rate") or 1)
    hi, lo = max(new, old), min(new, old)
    return lo > 0 and abs(hi / lo - round(hi / lo)) < 1e-9


Holders = dict[str, list[tuple[str, str, str, str]]]


def ticker_holders(graph2: dict[str, Any]) -> Holders:
    """Ticker -> every (first observed, last observed, security key, entity key) in graph V2."""
    out: Holders = defaultdict(list)
    for e in graph2["ticker_edges"]:
        out[e["ticker"]].append(
            (e["first_observed"], e["last_observed"], e["security_key"], e["entity_key"])
        )
    return {k: sorted(v) for k, v in out.items()}


def correct_holders(holders: Holders, corrections: list[dict[str, str]] | None) -> Holders:
    """Listing observations re-keyed by a documented identity correction (D-253)."""
    if not corrections:
        return holders
    out = {t: list(v) for t, v in holders.items()}
    for c in corrections:
        out[c["ticker"]] = sorted(
            (a, b, c["security_key"], c["entity_key"])
            if k == c["from_security_key"]
            else (a, b, k, e)
            for a, b, k, e in out.get(c["ticker"], [])
        )
    return out


def dividend_holder(d: dict[str, Any], s: dict[str, Any], holders: Holders) -> str:
    return holder_on(d["symbol"], d["ex_date"], s, holders)


def holder_on(ticker: str, day: str, s: dict[str, Any], holders: Holders) -> str:
    """Who held a ticker on a day, from graph V2's listing observations: THIS_SECURITY,
    ANOTHER_SECURITY, HOLDER_UNCERTAIN (the nearest observations either side disagree), or
    NO_EVIDENCE."""
    own_keys = {s["security_key"], s["ledger_key"]}

    def own(o: tuple[str, str, str, str]) -> bool:
        return o[2] in own_keys or o[3] == s["entity_key"]

    obs = holders.get(ticker, [])
    ex = day
    covering = [o for o in obs if o[0] <= ex <= o[1]]
    if any(not own(o) for o in covering):
        return "ANOTHER_SECURITY"
    if covering:
        return "THIS_SECURITY"
    before = max((o for o in obs if o[1] < ex), key=lambda o: o[1], default=None)
    after = min((o for o in obs if o[0] > ex), key=lambda o: o[0], default=None)
    sides = [o for o in (before, after) if o]
    if not sides:
        return "NO_EVIDENCE"
    if all(own(o) for o in sides):
        return "THIS_SECURITY"
    if not any(own(o) for o in sides):
        return "ANOTHER_SECURITY"
    return "HOLDER_UNCERTAIN"


def check_dividends(
    s: dict[str, Any],
    rows: list[dict[str, Any]],
    end: str,
    x41: dict[tuple[str, str], list[float]],
    holders: Holders | None = None,
) -> dict[str, Any]:
    lo = s["window"][0]
    cal = set(_sessions(lo, end))
    close = {r["session"]: float(r["close"]) for r in rows}
    days = sorted(close)
    tickers = set(s["tickers"])
    problems, large, not_in_x41 = [], [], []
    not_attributable: list[dict[str, Any]] = []
    ids: Counter[str] = Counter()
    keys: Counter[tuple[str, str, float]] = Counter()
    for d in s["dividends"]:
        ids[d.get("id") or ""] += 1
        keys[(d["symbol"], d["ex_date"], float(d["rate"] or 0))] += 1
        if not (lo <= d["ex_date"] <= end):
            problems.append({"ex_date": d["ex_date"], "why": "outside the slot's data range"})
        if d["ex_date"] not in cal:
            problems.append({"ex_date": d["ex_date"], "why": "not a session"})
        if not d["rate"] or float(d["rate"]) <= 0:
            problems.append({"ex_date": d["ex_date"], "why": "rate not positive"})
        if d["symbol"] not in tickers:
            problems.append(
                {"ex_date": d["ex_date"], "why": f"symbol {d['symbol']} not the slot's"}
            )
        i = bisect.bisect_left(days, d["ex_date"])
        ref = close[days[i - 1]] if i > 0 else close.get(d["ex_date"])  # else the ex-date close
        if ref and d["rate"] and float(d["rate"]) / ref >= LARGE_DISTRIBUTION:
            large.append({"ex_date": d["ex_date"], "rate": d["rate"], "special": d.get("special"),
                          "reference_close": ref})  # fmt: skip
        who = dividend_holder(d, s, holders or {})
        if who in ("ANOTHER_SECURITY", "HOLDER_UNCERTAIN"):
            item = {"symbol": d["symbol"], "ex_date": d["ex_date"], "rate": d["rate"]}
            not_attributable.append({**item, "id": d.get("id"), "holder": who})
        rates = x41.get((d["symbol"], d["ex_date"]))
        if rates is not None and not any(abs(x - float(d["rate"])) < 1e-6 for x in rates):
            not_in_x41.append({"ex_date": d["ex_date"], "rate": d["rate"], "x41_rates": rates})
    dup = [k for k, n in ids.items() if k and n > 1]
    dup += [f"{k[0]} {k[1]} {k[2]}" for k, n in keys.items() if n > 1]
    in_x41 = sum(1 for d in s["dividends"] if (d["symbol"], d["ex_date"]) in x41)
    missing_vs_x41 = sorted(
        f"{sym} {ex}"
        for (sym, ex) in x41
        if sym == s["ticker"] and lo <= ex <= end and (sym, ex) not in {(k[0], k[1]) for k in keys}
    )
    return {
        "result": BAD if problems or dup else OK,
        "dividends": len(s["dividends"]),
        "problems": problems[:20],
        "duplicates": dup,
        "large_distributions": large,
        "not_attributable": not_attributable,
        "x41_capture": {
            "compared": in_x41,
            "rate_differs": not_in_x41,
            "in_x41_not_x45": missing_vs_x41,
        },
    }


def return_integrity(
    rows: list[dict[str, Any]], lo: str, boundary: str | None, unusable: set[str],
    x2_blocked: set[str], verified: set[str],
) -> dict[str, Any]:  # fmt: skip
    """Close-to-close price returns inside the usable range: each one usable or excluded with
    its reason. Nothing is filled."""
    if not boundary:
        return {"pairs": 0, "usable": 0, "excluded": {}}
    cal = _sessions(lo, boundary)
    traded = {r["session"]: r for r in rows if float(r["volume"] or 0) > 0}
    why: Counter[str] = Counter()
    usable = 0
    for a, b in itertools.pairwise(cal):
        if a not in traded or b not in traded:
            why["MISSING_OR_NON_EXECUTABLE"] += 1
        elif b in unusable:
            why["UNUSABLE_RETURN_DATE"] += 1
        elif b in x2_blocked or a in x2_blocked:
            why["X2_SESSION_OR_SPAN"] += 1
        else:
            usable += 1
            if b in verified:
                why["USABLE_WITH_VERIFIED_SPLIT_FACTOR"] += 1
    return {"pairs": max(0, len(cal) - 1), "usable": usable, "excluded": dict(sorted(why.items()))}


# ---------------------------------------------------------------------- build
def _x41_dividends(ev: Evidence) -> dict[tuple[str, str], list[float]]:
    out: dict[tuple[str, str], list[float]] = defaultdict(list)
    for rel in ev.keys("x4", "provider/dividends/"):
        doc = ev.read("x4", rel)
        for pg in doc.get("pages", []):
            body = pg.get("body") or {}
            for d in (body.get("corporate_actions") or {}).get("cash_dividends", []) or []:
                out[(d["symbol"], d["ex_date"])].append(float(d["rate"]))
    return out


def validate_slot(s: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _rows(s)
    lo, hi = s["window"]
    end = s["end"]
    checks: dict[str, Any] = {}
    restrictions: list[dict[str, Any]] = []
    caveats: list[str] = []
    rec = ctx["members"].get(s["slot"])
    lf = (rec or {}).get("lifecycle") or {}

    # identity: the slot is the universe's member, under the same identity and window
    same = bool(rec) and all(
        (
            rec["ticker"] == s["ticker"],
            rec["security_key"] == s["security_key"],
            rec["entity_key"] == s["entity_key"],
            (lf.get("ledger_key") or rec["security_key"]) == s["ledger_key"],
            ctx["windows"][f"{s['pool']}|{s['formation']}"] == [lo, hi],
        )
    )
    checks["identity"] = {"result": OK if same else BAD}
    share = ctx["shared"].get(s["security_key"], {}).get(f"{s['pool']}|{s['formation']}")
    if share:
        checks["identity"]["security_key_shared_with"] = [t for t in share if t != s["ticker"]]

    # window and attribution
    sessions = [r["session"] for r in rows]
    inside = all(lo <= d <= end for d in sessions) and end <= hi
    exit_ok = not s["exit_cut"] or end <= s["exit_cut"]
    filed = s.get("removal_filed")
    cont = ctx["continuation"].get(s["slot"], 0)
    cut_ok = True
    if filed and cont:
        cut_ok = end < filed and (not sessions or sessions[-1] < filed)
    elif filed and sessions:
        # Without a continuing series, bars after the removal filing may only be zero-volume
        # placeholders (no trade recorded): never a traded bar.
        cut_ok = not any(r["session"] > filed and float(r["volume"] or 0) > 0 for r in rows)
    checks["attribution"] = {
        "result": OK if inside and exit_ok and cut_ok else BAD,
        "first_bar": sessions[0] if sessions else None,
        "last_bar": sessions[-1] if sessions else None,
        "end": end,
        "removal_filed": filed,
        "provider_series_continues_after_end": cont,
        "ends_before_removal_filing": s.get("ends_before_removal_filing"),
    }

    # quality, placeholders, splits, dividends
    checks["quality"] = check_quality(rows, lo, end)
    placeholders = [r["session"] for r in rows if not float(r["volume"] or 0) > 0]
    traded = [r["session"] for r in rows if float(r["volume"] or 0) > 0]
    last_traded = traded[-1] if traded else None
    checks["placeholders"] = {
        "result": OK
        if last_traded == s["last_traded"]
        and all(not (r["trades"] or 0) for r in rows if r["session"] in set(placeholders))
        else BAD,
        "non_executable_sessions": len(placeholders),
    }
    if placeholders:
        restrictions.append({"code": "NON_EXECUTABLE_SESSIONS", "sessions": placeholders,
                             "uses": ["price_strategy", "portfolio_accounting"]})  # fmt: skip
    sp = check_splits(s, rows)
    checks["splits"] = sp
    if sp["fractional_verified_records"]:
        restrictions.append({"code": "FRACTIONAL_SPLIT_RECORD",
                             "dates": [x["ex_date"] for x in sp["fractional_verified_records"]],
                             "uses": ["price_strategy", "total_return"]})  # fmt: skip
    if sp["provider_split_series_unusable_at"]:
        restrictions.append({"code": "PROVIDER_SPLIT_SERIES_INCONSISTENT",
                             "dates": sp["provider_split_series_unusable_at"],
                             "uses": ["split_adjusted_close"]})  # fmt: skip
    # exit evidence and the defensible boundary
    stops_early = last_traded is not None and last_traded < hi
    exe: dict[str, Any] = {"result": NA}
    gap: list[str] = []
    if s.get("successor_boundary"):
        # D-253: data ends at the last evidenced session of the original security.
        exe = {"result": OK, "notices": [], "successor_boundary": s["successor_boundary"]}
        exit_in_window = True
    elif stops_early or not traded:
        ent = ctx["entity_names"].get(s["entity_key"])
        exe = exit_evidence(s, last_traded or lo, ctx["notices"], ent)
        if exe["notices"]:
            first = exe["notices"][0]
            listed_to = listing_last_session(first, s["window"])
            gap = [
                d for d in _sessions(lo, listed_to or lo)
                if (last_traded is None or d > last_traded) and d <= end
            ] if listed_to else []  # fmt: skip
            exe["listing_last_session"] = listed_to
            exe["sessions_without_trade_while_listed"] = gap
            exe["result"] = OK
        else:
            exe["result"] = BAD
            exe["why"] = "the data stops before the window's end with no removal notice found"
        if s["exit_cut"] is None:
            exe["x45_exit"] = "NONE_RECORDED"
        exit_in_window = True
    else:
        exit_in_window = bool(s["exit_cut"] and s["exit_cut"] <= hi)
    checks["exit_evidence"] = exe

    # Identity on every bar: the bar's ticker held by this security on that session.
    holders = ctx.get("holders") or {}
    listed_to_own = exe.get("listing_last_session") if exe.get("notices") else None
    foreign = {}
    for r in rows:
        t = _source_ticker(r["source"]) or s["ticker"]
        who = holder_on(t, r["session"], s, holders)
        if (
            who == "HOLDER_UNCERTAIN"
            and t == s["ticker"]
            and listed_to_own
            and r["session"] <= listed_to_own
        ):
            # The member's own removal notice shows its class listed (under its formation
            # ticker) to this session: listing evidence, not a price.
            who = "THIS_SECURITY_BY_REMOVAL_NOTICE"
        if who in ("ANOTHER_SECURITY", "HOLDER_UNCERTAIN"):
            foreign[r["session"]] = f"{t}:{who}"
    checks["identity_on_bars"] = {
        "result": OK,
        "bars_not_evidenced": len(foreign),
        "first": min(foreign) if foreign else None,
        "last": max(foreign) if foreign else None,
    }
    if foreign:
        restrictions.append(
            {
                "code": "IDENTITY_NOT_EVIDENCED",
                "sessions": sorted(foreign),
                "detail": sorted(set(foreign.values())),
                "uses": ["price_strategy", "total_return", "portfolio_accounting"],
            }
        )
    attributable = [d for d in traded if d not in foreign]
    last_ok = attributable[-1] if attributable else None
    boundary = min(end, last_ok) if last_ok else None
    dv = check_dividends(s, rows, end, ctx["x41_dividends"], ctx.get("holders"))
    checks["dividends"] = dv
    if dv["not_attributable"]:
        na = dv["not_attributable"]
        uses = ["total_return", "portfolio_accounting"]
        restrictions.append({"code": "DIVIDEND_NOT_ATTRIBUTABLE", "dividends": na, "uses": uses})
    if dv["large_distributions"]:
        restrictions.append({"code": "LARGE_CASH_DISTRIBUTION",
                             "dates": [x["ex_date"] for x in dv["large_distributions"]],
                             "uses": ["total_return", "portfolio_accounting"]})  # fmt: skip
    unusable = set(s["unusable_return_dates"])
    if unusable:
        restrictions.append({"code": "UNUSABLE_RETURN_DATE", "dates": sorted(unusable),
                             "uses": ["price_strategy", "total_return"]})  # fmt: skip

    checks["coverage"] = coverage_audit(s, sessions, exe)
    if gap:
        restrictions.append({"code": "NO_TRADE_WHILE_LISTED", "sessions": gap,
                             "uses": ["price_strategy", "portfolio_accounting"]})  # fmt: skip
    tv = s.get("terminal")
    if (exit_in_window or stops_early) and tv is None:
        terms = [t for n in exe.get("notices", []) for t in n["terms"]]
        restrictions.append({
            "code": "TERMINAL_VALUE_NOT_IN_DATASET",
            "after": boundary,
            "notice_terms": terms,
            "uses": ["total_return", "portfolio_accounting"],
        })  # fmt: skip
    elif (
        (exit_in_window or stops_early)
        and tv is not None
        and tv["value_status"] != "DOCUMENTED_CASH"
    ):
        # D-253: the revision documents terminal terms; only a cash value is complete.
        needs_price = tv["value_status"].startswith("DOCUMENTED_TERMS")
        restrictions.append({
            "code": "TERMINAL_VALUE_NEEDS_PRICE" if needs_price else "TERMINAL_VALUE_UNRESOLVED",
            "after": boundary,
            "terminal": {"kind": tv["kind"], "value_status": tv["value_status"]},
            "uses": ["total_return", "portfolio_accounting"],
        })  # fmt: skip

    # X1 / X2 (lineage: the provider asset series this slot's data is drawn from)
    lin = ctx["lineage"].get(s["slot"], {})
    x1 = sorted({lab for lab, assets in ctx["x1_assets"].items() for a in assets if a in lin})
    x2_blocked: set[str] = set()
    x2 = []
    for e in exclusions_x2.EVENTS:
        if e.asset_id not in lin:
            continue
        d = e.session.isoformat()
        splice = e.kind == exclusions_x2.SPLICE
        scope = (
            "IN_WINDOW" if lo <= d <= hi
            else "FORMATION_WINDOW" if rec and rec["first_bar"] <= d <= s["formation"]
            else "BEFORE_WINDOW_LOOKBACK" if d < lo else "AFTER_WINDOW"
        )  # fmt: skip
        if scope == "AFTER_WINDOW":
            continue
        item: dict[str, Any] = {"ref": e.ref, "session": d, "kind": e.kind, "scope": scope}
        if scope == "IN_WINDOW":
            blocked = [x for x in _sessions(d, end)] if splice else [d]
            x2_blocked |= set(blocked)
            item["blocked_from"] = d
            item["blocked_to"] = blocked[-1] if blocked else d
        if scope == "FORMATION_WINDOW":
            item["ranking"] = ctx["robustness"].get((s["slot"], e.ref))
        x2.append(item)
    if x2:
        uses = ["price_strategy", "total_return", "portfolio_accounting"]
        restrictions.append({"code": "X2_EXCLUSION", "events": x2, "uses": uses})
    flagged = sorted(f for f in s["flags"] if f.startswith(("X1_", "X2_")))
    checks["exclusions"] = {
        "result": OK,
        "lineage_assets": {ctx["identities"][a]["symbol"]: n for a, n in lin.items()},
        "x1_labels": x1,
        "x2_events": [x["ref"] for x in x2],
        "x44_flags": flagged,
    }

    # returns
    checks["returns"] = return_integrity(
        rows, lo, boundary, unusable, x2_blocked | set(foreign), set(sp["verified"])
    )

    # caveats
    if s["formation"] == "2016-12-30":
        caveats.append("COVERAGE_LIMITED_2016")
    if s["identity_confidence"] == "MEDIUM":
        caveats.append("IDENTITY_MEDIUM")
    if (s["exit_reason"] or "").startswith(("ISSUER_LEVEL_END", "X44_PRIORITY_NOTICE_END")):
        caveats.append("EXIT_FROM_ISSUER_OR_NOTICE_EVIDENCE")
    if any(r["verdict"].startswith("ACCEPTED") for r in s["recoveries"]):
        caveats.append("RECOVERED_BARS")
    if s["splits"].get("unrecorded_factor_changes"):
        caveats.append("PROVIDER_MAPPING_SEAM")
    if exe.get("notices") and exe["notices"][0]["matched_by"] != "ISSUER_CIK":
        caveats.append("EXIT_NOTICE_MATCHED_BY_NAME")

    # status
    failed = sorted(k for k, v in checks.items() if isinstance(v, dict) and v.get("result") == BAD)
    reasons = []
    if s["slot"] in ctx["removed"]:
        reasons.append("REMOVED_BEFORE_FORMATION")
    if x1:
        reasons.append("X1_ASSET_EXCLUSION:" + ",".join(x1))
    if any(x.get("ranking") and not x["ranking"]["still_member"] for x in x2):
        reasons.append("MEMBERSHIP_DEPENDS_ON_X2_SESSION")
    status = FAIL if failed else EXCLUDED if reasons else RESTRICTED if restrictions else PASS
    excluded_dates = sorted(
        {d for d in set(placeholders) | unusable | x2_blocked | set(gap) | set(foreign)
         if not boundary or d <= boundary}
    )  # fmt: skip
    return {
        "slot": s["slot"],
        "pool": s["pool"],
        "formation": s["formation"],
        "ticker": s["ticker"],
        "tickers": s["tickers"],
        "security_key": s["security_key"],
        "ledger_key": s["ledger_key"],
        "lookup_key": s["slot"],
        "status": status,
        "failed_checks": failed,
        "exclusion_reasons": reasons,
        "restrictions": restrictions,
        "caveats": caveats,
        "usable_range": [sessions[0], boundary]
        if sessions and boundary and status in (PASS, RESTRICTED)
        else None,
        "excluded_sessions": excluded_dates if status in (PASS, RESTRICTED) else None,
        "checks": checks,
    }


def coverage_audit(s: dict[str, Any], sessions: list[str], exe: dict[str, Any]) -> dict[str, Any]:
    """Expected sessions recomputed from the exchange calendar; every missing session classified
    against the removal evidence: after the listing ended (not a data gap), or while still
    listed with no trade recorded, or unexplained."""
    lo, end = s["window"][0], s["end"]
    expected = _sessions(lo, end)
    present = set(sessions)
    missing = [d for d in expected if d not in present]
    after: list[str] = []
    listed: list[str] = []
    unexplained: list[str] = []
    if exe.get("notices"):
        to = exe.get("listing_last_session")
        for d in missing:
            (after if to is None or d > to else listed).append(d)
    else:
        unexplained = missing
    q = s["quality"]
    agrees = len(expected) == q["expected_sessions"] and len(missing) == q["missing_sessions"]
    return {
        "result": OK if agrees and not unexplained else BAD,
        "expected_sessions": len(expected),
        "bars": len(present & set(expected)),
        "missing": len(missing),
        "missing_after_listing_ended": len(after),
        "missing_while_listed": len(listed),
        "missing_unexplained": unexplained,
        "agrees_with_x45": agrees,
    }


def global_boundary(slots: list[dict[str, Any]]) -> dict[str, Any]:
    """No bar (session, close, volume) appears under two different securities."""
    idx: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for s in slots:
        for r in _rows(s):
            if float(r["volume"] or 0) > 0:
                idx[(r["session"], f"{float(r['close']):.4f}", round(float(r["volume"])))].add(
                    s["ledger_key"]
                )
    clash = Counter(tuple(sorted(v)) for v in idx.values() if len(v) > 1)
    return {
        "result": OK if not clash else BAD,
        "bars_compared": len(idx),
        "shared_across_securities": {" / ".join(k): n for k, n in clash.most_common(20)},
    }


def named_cases(*_args: object, **_kwargs: object) -> object:
    raise NotImplementedError("REDACTED_FOR_GITHUB")


def x5_requirements(summary: dict[str, Any]) -> list[dict[str, str]]:
    n_exit = sum(
        n for k, n in summary["all"]["restrictions"].items() if k.startswith("TERMINAL_VALUE")
    )
    return [
        {
            "id": "X5-1",
            "need": "Enforce slot status and restrictions in every research run: only PASS and "
            "RESTRICTED slots, only their usable range, never an excluded session.",
        },
        {
            "id": "X5-2",
            "need": f"Terminal proceeds for exits ({n_exit} slots): a documented rule per exit; "
            "the removal notices' quoted terms are evidence, not a computed value.",
        },
        {
            "id": "X5-3",
            "need": "A total-return construction: reinvestment convention, ex-date versus "
            "pay-date crediting (pay dates are not captured), large and special distributions, "
            "and spin-offs the provider encodes as fractional splits.",
        },
        {
            "id": "X5-4",
            "need": "X1/X2 enforcement on the historical slots: X1 assets excluded; X2 sessions "
            "and splice spans closed, including lookback reads before a window.",
        },
        {
            "id": "X5-5",
            "need": "Lookups by slot, or by ticker and session; a security key only when it "
            "names one listing (three keys do not).",
        },
        {
            "id": "X5-6",
            "need": "Sessions that cannot trade (no trade while listed, zero-volume "
            "placeholders) are never fills or marks.",
        },
        {
            "id": "X5-8",
            "need": "Dividends: use only those attributable to the slot's security on the "
            "REDACTED_FOR_GITHUB",
        },
        {
            "id": "X5-7",
            "need": "Labels carried into every result: the 2016 coverage limits (MEDIUM "
            "identity), and the unresolved and excluded members as known gaps, never replaced.",
        },
    ]


def status_csv(doc: dict[str, Any]) -> str:
    """One row per membership slot: status, reasons, restrictions, caveats, usable range."""
    lines = ["slot,status,reasons,restrictions,caveats,usable_from,usable_to,excluded_sessions"]
    for r in doc["slots"]:
        rng = r.get("usable_range") or ["", ""]
        reasons = r.get("exclusion_reasons") or r.get("failed_checks") or []
        lines.append(
            ",".join(
                [
                    r["slot"],
                    r["status"],
                    ";".join(reasons),
                    ";".join(sorted({x["code"] for x in r["restrictions"]})),
                    ";".join(r.get("caveats") or []),
                    rng[0] or "",
                    rng[1] or "",
                    str(len(r.get("excluded_sessions") or [])),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def build(
    dataset: dict[str, Any],
    universe: dict[str, Any],
    ledger: dict[str, Any],
    stores: dict[str, x4.Store],
    preflight: dict[str, Any] | None = None,
    graph2: dict[str, Any] | None = None,
    pinned: dict[str, str] | None = None,
    data_content_sha256: str = DATA_CONTENT_SHA256,
    edge_corrections: list[dict[str, str]] | None = None,
    code: str = CODE,
    decision: str = DECISION,
) -> dict[str, Any]:
    ev = Evidence(stores)
    slots = dataset["slots"]
    ledger_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in ledger["events"]:
        ledger_by_key[e["security_key"]].append(e)
    members = {
        f"{k}|{r['ticker']}": r
        for k, f in universe["formations"].items()
        for r in f["records"]
        if r["decision"] == "MEMBER"
    }
    fc = formation_checks(universe, ev, ledger_by_key)
    nt = load_notices(ev)
    idx, identities = provider_lineage(ev)
    lineage = {s["slot"]: slot_lineage(s, idx) for s in slots}
    br = x45d.bars_by_run(stores["x45"])  # type: ignore[arg-type]
    continuation = {}
    for s in slots:
        prim = br.get((f"{s['pool']}_{s['formation']}", "raw"), {}).get(s["ticker"], {})
        continuation[s["slot"]] = sum(1 for d in prim if d > s["end"])
    # Ranking robustness for X2 sessions inside a member's formation window.
    robustness: dict[tuple[str, str], Any] = {}
    need: dict[str, set[str]] = defaultdict(set)
    hits = []
    for s in slots:
        rec = members.get(s["slot"])
        for e in exclusions_x2.EVENTS:
            d = e.session.isoformat()
            if rec and e.asset_id in lineage[s["slot"]] and rec["first_bar"] <= d <= s["formation"]:
                need[s["formation"]].add(s["ticker"])
                hits.append((s, rec, e))
    fbars = {f: formation_bars(ev, f, t) for f, t in need.items()}
    for s, rec, e in hits:
        f = universe["formations"][f"{s['pool']}|{s['formation']}"]
        below = sorted(
            (r["median_dollar_volume"] for r in f["records"] if r["decision"] == "BELOW_CUTOFF"),
            reverse=True,
        )
        robustness[(s["slot"], e.ref)] = ranking_robustness(
            rec, fbars[s["formation"]].get(s["ticker"], {}), e.session.isoformat(),
            e.kind == exclusions_x2.SPLICE, below[0] if below else 0.0, f["rule"],
        )  # fmt: skip
    ctx = {
        "members": members,
        "windows": {k: f["window"] for k, f in universe["formations"].items()},
        "shared": shared_keys(slots),
        "continuation": continuation,
        "notices": nt,
        "entity_names": {e["entity_key"]: e["name"] for e in ledger["entities"]},
        "lineage": lineage,
        "identities": identities,
        "x1_assets": x1_assets(identities),
        "x41_dividends": _x41_dividends(ev),
        "removed": fc["removed_before_formation"],
        "robustness": robustness,
        "holders": correct_holders(ticker_holders(graph2) if graph2 else {}, edge_corrections),
    }
    results = [validate_slot(s, ctx) for s in slots]
    for e in dataset["excluded_unresolved_slots"]:
        pool, formation, ticker = e["slot"].split("|")
        results.append(
            {
                "slot": e["slot"],
                "pool": pool,
                "formation": formation,
                "ticker": ticker,
                "status": EXCLUDED,
                "exclusion_reasons": ["MEMBER_IDENTITY_UNRESOLVED"],
                "restrictions": [],
                "caveats": ["COVERAGE_LIMITED_2016"] if formation == "2016-12-30" else [],
                "usable_range": None,
                "lookup_key": e["slot"],
            }
        )
    results.sort(key=lambda r: r["slot"])
    lk = Lookup(slots)
    doc: dict[str, Any] = {
        "kind": "x46_research_data_validation",
        "validation_code": code,
        "decision": decision,
        "inputs": {
            "pinned": pinned or PINNED,
            "dataset_content_sha256": dataset["content_sha256"],
            "evidence_read": dict(sorted(ev.checked.items())),
        },
        "preflight": {
            "hashes": preflight,
            "dataset_content_ok": dataset["content_sha256"] == data_content_sha256
            and x45d.content_sha256(dataset) == data_content_sha256,
            "evidence_hash_failures": sorted(set(ev.failures)),
            "reconciliation": reconcile(dataset, universe),
        },
        "global_checks": {
            "security_boundaries": global_boundary(slots),
            "formations": fc,
            "retrieval": retrieval_selftest(lk),
            "x1_assets": ctx["x1_assets"],
            "x2_formation_window_rankings": {
                f"{k[0]} {k[1]}": v for k, v in sorted(robustness.items())
            },
        },
        "return_uses": RETURN_USES,
        "slots": results,
    }
    doc["global_checks"]["named_cases"] = named_cases(results, dataset, ledger)
    in_2016 = [r for r in results if r["formation"] == "2016-12-30"]
    doc["global_checks"]["coverage_2016_carried"] = {
        "ok": all("COVERAGE_LIMITED_2016" in r["caveats"] for r in in_2016),
        "slots_2016": len(in_2016),
    }
    doc["unresolved_corporate_actions"] = unresolved_actions(results, dataset)
    doc["summary"] = summarize(results)
    doc["x5_requirements"] = x5_requirements(doc["summary"])
    doc["content_sha256"] = content_sha256(doc)
    return doc


def unresolved_actions(results: list[dict[str, Any]], dataset: dict[str, Any]) -> dict[str, Any]:
    """Every corporate action this data cannot resolve, kept flagged."""
    out: dict[str, Any] = defaultdict(list)
    for r in results:
        if "checks" not in r:
            continue
        sp = r["checks"]["splits"]
        for d in sp["artifacts"]:
            out["provider_adjustment_artifacts"].append({"slot": r["slot"], "date": d})
        for x in sp["fractional_verified_records"]:
            out["fractional_split_records"].append({"slot": r["slot"], "date": x["ex_date"]})
        for x in r["checks"]["dividends"]["not_attributable"]:
            out["x45_dividends_not_attributable"].append({"slot": r["slot"], **x})
        for x in r["checks"]["dividends"]["large_distributions"]:
            out["large_cash_distributions"].append(
                {"slot": r["slot"], "date": x["ex_date"], "rate": x["rate"]}
            )
        for x in r["restrictions"]:
            if x["code"] == "X2_EXCLUSION":
                for e in x["events"]:
                    out["x2_events"].append(
                        {"slot": r["slot"], "ref": e["ref"], "session": e["session"],
                         "scope": e["scope"]}
                    )  # fmt: skip
    out["x45_known_cases"] = dataset["known_cases"]
    return dict(sorted(out.items()))


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def block(rs_: list[dict[str, Any]]) -> dict[str, Any]:
        data = [r for r in rs_ if "checks" in r]
        ret: Counter[str] = Counter()
        for r in data:
            rt = r["checks"]["returns"]
            ret["pairs"] += rt["pairs"]
            ret["usable"] += rt["usable"]
            for k, v in rt["excluded"].items():
                ret[k] += v
        restr = Counter(x["code"] for r in rs_ for x in r["restrictions"])
        cov: Counter[str] = Counter()
        for r in data:
            c = r["checks"]["coverage"]
            for k in ("expected_sessions", "bars", "missing", "missing_after_listing_ended",
                      "missing_while_listed"):  # fmt: skip
                cov[k] += c[k]
            cov["missing_unexplained"] += len(c["missing_unexplained"])
        listed = cov["expected_sessions"] - cov["missing_after_listing_ended"]
        ca: Counter[str] = Counter()
        for r in data:
            sp, dv = r["checks"]["splits"], r["checks"]["dividends"]
            ca["split_records"] += sp["records"]
            ca["splits_verified"] += len(sp["verified"])
            ca["split_artifacts"] += len(sp["artifacts"])
            ca["split_mismatches"] += len(sp["mismatches"])
            ca["fractional_split_records"] += len(sp["fractional_verified_records"])
            ca["dividends"] += dv["dividends"]
            ca["dividend_problems"] += len(dv["problems"]) + len(dv["duplicates"])
            ca["dividends_compared_with_x41"] += dv["x41_capture"]["compared"]
            ca["dividend_rates_differ_from_x41"] += len(dv["x41_capture"]["rate_differs"])
            ca["x41_dividends_not_in_x45"] += len(dv["x41_capture"]["in_x41_not_x45"])
            ca["large_cash_distributions"] += len(dv["large_distributions"])
            ca["dividends_not_attributable"] += len(dv["not_attributable"])
        reasons = Counter(x.split(":")[0] for r in rs_ for x in r.get("exclusion_reasons", []))
        return {
            "slots": len(rs_),
            "status": dict(sorted(Counter(r["status"] for r in rs_).items())),
            "exclusion_reasons": dict(sorted(reasons.items())),
            "restrictions": dict(sorted(restr.items())),
            "price_returns": {
                **dict(sorted(ret.items())),
                "usable_share": round(ret["usable"] / ret["pairs"], 6) if ret["pairs"] else 0.0,
            },
            "sessions_in_usable_ranges": sum(_usable_bars(r) for r in data),
            "corporate_actions": dict(sorted(ca.items())),
            "coverage": {
                **dict(sorted(cov.items())),
                "coverage_of_expected": round(cov["bars"] / cov["expected_sessions"], 6)
                if cov["expected_sessions"]
                else 0.0,
                "coverage_while_listed": round(cov["bars"] / listed, 6) if listed else 0.0,
            },
        }

    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by[f"{r['pool']}|{r['formation']}"].append(r)
    return {"all": block(results), "by_formation": {k: block(v) for k, v in sorted(by.items())}}


def _usable_bars(r: dict[str, Any]) -> int:
    if not r.get("usable_range"):
        return 0
    lo, hi = r["usable_range"]
    excluded = set(r.get("excluded_sessions") or [])
    return sum(1 for d in _sessions(lo, hi) if d not in excluded)


def content_sha256(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "content_sha256"}
    return x4.sha256(x4.canonical(rs._plain(body)))


def load_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    got = l2._sha_file(x45d.DATA_PATH)
    if got != PINNED[x45d.DATA_PATH.as_posix()]:
        raise ValueError(f"{x45d.DATA_PATH}: sha256 {got} is not the reported X4.5 dataset")
    dataset: dict[str, Any] = x4.read_json(x45d.DATA_PATH)
    graph2 = x4.read_json(g2.GRAPH2_PATH)
    return dataset, x45d.load_universe(), g2.load_ledger_v2(), graph2


def stores() -> dict[str, x4.Store]:
    return {"x4": x4.Store(), "x43": x43e.Store(), "x45": x45d.Store()}


def write(doc: dict[str, Any], path: Path = VALIDATION_PATH) -> str:
    if path.exists():
        raise FileExistsError(f"{path} exists: the validation is written once")
    data = x4.gz(x4.canonical(rs._plain(doc)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


STATUS_TABLE = Path("docs/phase-reports/x46-slot-status.csv")


def write_status_table(doc: dict[str, Any], path: Path = STATUS_TABLE) -> str:
    """The per-slot status table beside the report (derived from the artifact; written once)."""
    if path.exists():
        raise FileExistsError(f"{path} exists")
    data = status_csv(doc).encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def verify(path: Path = VALIDATION_PATH) -> dict[str, Any]:
    stored = x4.read_json(path)
    pre = preflight_hashes()
    dataset, universe, ledger, graph2 = load_inputs()
    rebuilt = build(dataset, universe, ledger, stores(), pre, graph2)
    return {
        "file_sha256": l2._sha_file(path),
        "preflight_ok": pre["ok"],
        "stored_content_sha256": stored["content_sha256"],
        "recomputed_content_sha256": content_sha256(stored),
        "rebuilt_content_sha256": rebuilt["content_sha256"],
        "ok": pre["ok"]
        and stored["content_sha256"] == content_sha256(stored) == rebuilt["content_sha256"],
    }


__all__ = [
    "CODE",
    "VALIDATION_PATH",
    "AmbiguousLookupError",
    "Lookup",
    "build",
    "verify",
    "write",
]
