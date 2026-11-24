"""X4.3.1 (D-248): the lifecycle ledger V2, a reconciliation of V1 with the X4.3 graph.

:func:`build` reads the X4.1 evidence, the X4.2 ledger V1 and the X4.3 identity graph (each
verified by its SHA-256) and the X4.3 evidence. It never writes to any of them, and returns a
deterministic document that :func:`write` stores once
(``config/simulation/lifecycle/x431_ledger_v2.json.gz``). The document holds:

* ``entities``, ``securities``, ``events``: the V2 ledger. Same schema as V1 (migration 0028),
  so it can be registered beside V1.
* ``supersedes``: V1 by set code and hashes.
* ``v1_events``: one disposition for every V1 event (``CARRIED``, ``REVISED``,
  ``RECLASSIFIED``, ``DUPLICATE_OF``), so nothing is deleted silently.
* ``v1_securities``: one disposition for every V1 security (carried, relabelled or retired as
  an exchange alias).
* ``proposals``: the verdict on every X4.3 proposal, accepted or rejected, with the rule
  applied.
* ``provider_histories``: segments of each provider identity, attributed to a security or left
  unattributed; join determinations under :data:`JOIN_RULES`; the price-jump investigations.
* ``successors``: conversions whose ticker continues. Kept apart from identity: a successor is
  never joined to its predecessor here.
* ``unresolved``: what remains open, by kind.
* ``reconciliation``: V1 against V2 by event type and by notice.

**Corrections to V1.**

* *Duplicates.* EDGAR indexes each 25-NSE under the issuer and under the exchange, and V1 made
  events for both. V2 keeps the issuer copy (the CIK inside the notice). Each exchange copy
  becomes ``DUPLICATE_OF`` the canonical event, and so does the terminal event derived from it.
* *Class keys.* A V1 key that labels a non-equity class as plain equity is relabelled
  ``CIK|<category>-<hash of the class wording>``: rights, preferred depositary shares, LP
  units, warrants, units, debt. So is a class the notice names after another entity or a
  tracking series (LiLAC, Liberty Ventures, FNFV Group); see :func:`named_class`. Terminal
  events follow the class they belong to, so the plain equity no longer ends falsely
  REDACTED_FOR_GITHUB
* *Rules.* The X4.3 glyph readings replace ``UNPARSED_RULE``, but only when exactly one
  provision is checked. The others stay unparsed and are listed as unresolved.
* *Bounds.* A terminal event takes bounds from its notice's listing link (last capture
  listing it, first capture without it), when V1 had none. The filing date stays separate,
  and no effective date is ever set.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from datetime import date
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
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import x4_evidence as x4
from app.research import x43_evidence as x43

OUT = lc.OUT
SET_CODE = "SECURITY_LIFECYCLE_V2"
V2_PATH = OUT / "x431_ledger_v2.json.gz"
GRAPH = rs.GRAPH_PATH
GRAPH_SHA256 = "eceee9dfe8af0049db8470e54cc92accad612266a4f1933a8d74c7b9289c297e"
CREATED_BY = "operator:research-foundation"
CARRIED, REVISED, RECLASSIFIED, DUPLICATE = "CARRIED", "REVISED", "RECLASSIFIED", "DUPLICATE_OF"
CONFIRMED, UNRESOLVED, CONFLICTING = rs.CONFIRMED, rs.UNRESOLVED, rs.CONFLICTING
JUMP_LOG = math.log(3)

JOIN_RULES: dict[str, str] = {
    "J1_SAME_SECURITY": "Both histories are attributed to the same ledger security by listing "
    "or notice evidence (issuer by listed name, SEC class), never by prices.",
    "J2_IDENTICAL_OVERLAP": "Where they overlap, every common session (at least 20) has the "
    "same close (and volume where stored). Necessary, never sufficient on its own.",
    "J3_EXPLAINED_SEAM": "The seam is a confirmed non-terminal event of that security "
    "(exchange transfer, ticker change), or a provider mapping change with the security "
    "listed under the same identity before and after it.",
    "J4_NO_END_OR_SUCCESSOR": "No terminal event, merger or successor relationship of the "
    "security inside the seam; bars after a security's end are never joined to it.",
    "J5_OUTCOMES": "All of J1-J4: JOIN_PERMITTED. J1, J3 and J4 without overlap to compare: "
    "JOIN_UNVERIFIED (identity only; prices across the seam unchecked). J1 or J4 failing: "
    "JOIN_FORBIDDEN. Prices are never inherited across securities.",
}
GENERIC_CLASS_WORDS = frozenset(
    [
        "common",
        "stock",
        "stocks",
        "shares",
        "share",
        "class",
        "series",
        "ordinary",
        "voting",
        "nonvoting",
        "non",
        "par",
        "value",
        "per",
        "each",
        "representing",
        "represented",
        "represent",
        "american",
        "depositary",
        "depository",
        "receipts",
        "receipt",
        "evidenced",
        "the",
        "and",
        "of",
        "with",
        "without",
        "no",
        "nominal",
        "beneficial",
        "interest",
        "interests",
        "limited",
        "liability",
        "equity",
        "having",
        "attached",
        "together",
        "trade",
        "which",
        "are",
        "cpo",
        "cpos",
        "ads",
        "adr",
        "adrs",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "fifteen",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "sixty",
        "seventy",
        "eighty",
        "ninety",
        "hundred",
        "thousand",
        "half",
        "third",
        "fourth",
        "fifth",
        "tenth",
        "nt",
        "usd",
        "dollar",
        "dollars",
        "cents",
        "cent",
        "eur",
        "euro",
        "chf",
        "sek",
        "nok",
        "nis",
        "dkk",
        "gbp",
        "jpy",
        "israeli",
        "shekels",
        "shekel",
        "pence",
        "sterling",
        "its",
        "in",
        "a",
        "an",
        "to",
        "for",
        "or",
        "as",
        "is",
        "be",
        "old",
        "new",
    ]
)


# ---------------------------------------------------------------------- inputs
def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_graph(path: Path = GRAPH, expect: str = GRAPH_SHA256) -> dict[str, Any]:
    got = _sha_file(path)
    if got != expect:
        raise ValueError(f"{path}: sha256 {got} is not the X4.3 graph {expect}")
    doc: dict[str, Any] = x4.read_json(path)
    if rs.content_sha256(doc) != doc["content_sha256"]:
        raise ValueError(f"{path}: the graph does not hash to its content_sha256")
    return doc


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date) else v


def named_class(desc: str | None, issuer_names: list[str]) -> list[str]:
    """Words of a plain-equity class description that name neither the issuer nor a
    generic class feature: a tracking series ('LiLAC', 'Liberty Ventures', 'FNFV Group') or
    another entity's paired shares. Empty for an ordinary description."""
    issuer: set[str] = set()
    for n in issuer_names:
        issuer |= set(rs.words(n))
    toks = {t for t in rs.words(desc) if t.isalpha() and len(t) >= 3}
    return sorted(toks - GENERIC_CLASS_WORDS - issuer - rs.SUFFIX - set(rs.ABBREV.values()))


def v2_key(
    v1_key: str, v1_label: str, desc: str | None, issuer_names: list[str]
) -> tuple[str, str | None]:
    """The V2 security key of a notice's class, and why it differs from V1 (or None)."""
    if v1_key.startswith("SECCLASS:") or "|" not in v1_key:
        return v1_key, None
    entity = v1_key.split("|", 1)[0]
    cat = rs.category(desc)
    if v1_label in rs.GENERIC and cat not in rs.GENERIC and cat not in ("FUND", "OTHER"):
        return f"{entity}|{rs.security_label(cat, desc)}", f"CATEGORY_{cat}"
    if v1_label in rs.GENERIC and (extra := named_class(desc, issuer_names)):
        label = "NAMED_CLASS-" + hashlib.sha256(lc.norm(desc).encode()).hexdigest()[:10]
        return f"{entity}|{label}", "NAMED_CLASS:" + ",".join(extra)
    return v1_key, None


def _graph_ref(locator: str) -> dict[str, Any]:
    return {"artifact": GRAPH.as_posix(), "sha256": GRAPH_SHA256, "locator": locator}


def _comparable(e: dict[str, Any]) -> dict[str, Any]:
    return {k: _iso(e.get(k)) for k in lc.EVENT_FIELDS if k not in ("revision", "evidence_sha256")}


# ---------------------------------------------------------------------- build
class V2:
    def __init__(self, ledger: dict[str, Any], graph: dict[str, Any], x4s: x4.Store) -> None:
        self.v1 = ledger
        self.g = graph
        self.x4s = x4s
        self.v1_events = {e["code"]: e for e in ledger["events"]}
        self.v1_secs = {s["security_key"]: s for s in ledger["securities"]}
        self.v1_ents = {e["entity_key"]: e for e in ledger["entities"]}
        self.entities: dict[str, dict[str, Any]] = {}
        self.securities: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.origin: dict[str, list[str]] = defaultdict(list)  # V2 code -> origins
        self.v1_disp: dict[str, dict[str, Any]] = {}
        self.sec_disp: dict[str, dict[str, Any]] = {}
        self.class_notes: list[dict[str, Any]] = []
        self.unresolved: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.edges_by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in graph["ticker_edges"]:
            self.edges_by_sec[e["security_key"]].append(e)
        self._ed: rs.Edgar | None = None
        self.v1_removal_map: dict[str, list[str]] = {}
        self.v2_terminal_of_v1: dict[str, set[str]] = defaultdict(set)
        self.reused_keys: set[str] = set()

    # -- registries
    def edgar(self) -> rs.Edgar:
        if self._ed is None:
            self._ed = rs.edgar(self.x4s)
        return self._ed

    def issuer_names(self, entity: str | None) -> list[str]:
        if not entity or not entity.startswith("CIK:"):
            return []
        names = list(self.edgar().names.get(entity[4:], {}))
        if entity in self.v1_ents:
            names.append(self.v1_ents[entity]["name"] or "")
        return names

    def add_entity(self, key: str, ev: dict[str, Any]) -> None:
        if key in self.entities:
            return
        if key in self.v1_ents:
            self.entities[key] = self.v1_ents[key]
            return
        names = self.edgar().names.get(key[4:], {})
        name = max(names, key=lambda n: names[n][1]) if names else key
        self.entities[key] = {
            "entity_key": key,
            "cik": key[4:],
            "name": name,
            "name_source": "EDGAR full index (latest name filed under, 2015-2020)",
            "evidence": [ev],
        }

    def add_security(
        self, key: str, entity: str | None, label: str, desc: str | None, ev: dict[str, Any]
    ) -> None:
        if key in self.securities:
            return
        if key in self.v1_secs:
            s = self.v1_secs[key]
            self.securities[key] = s
            if s["entity_key"]:
                self.add_entity(s["entity_key"], ev)
            return
        if entity:
            self.add_entity(entity, ev)
        self.securities[key] = {
            "security_key": key,
            "entity_key": entity,
            "sec_class_id": key[9:] if key.startswith("SECCLASS:") else None,
            "class_label": label,
            "class_description": desc,
            "evidence": [ev],
        }

    def listing_security(self, key: str, edge: dict[str, Any]) -> None:
        """A security identified by X4.3 listings. A V1 key whose notices were all relabelled
        to their real classes is free: it is reused for the listed class, with a new record
        (never V1's mislabelled description)."""
        if key in self.securities:
            return
        label = key.split("|", 1)[1] if "|" in key else "FUND_CLASS"
        relabelled = {n["v1_security"] for n in self.class_notes}
        if key in self.v1_secs and key not in relabelled:
            self.add_security(key, edge["entity_key"], label, None, edge["evidence"][0])
            return
        if key in relabelled:
            self.reused_keys.add(key)
        if edge["entity_key"]:
            self.add_entity(edge["entity_key"], edge["evidence"][0])
        self.securities[key] = {
            "security_key": key,
            "entity_key": edge["entity_key"],
            "sec_class_id": key[9:] if key.startswith("SECCLASS:") else None,
            "class_label": label,
            "class_description": edge["listed_names"][-1],
            "evidence": [edge["evidence"][0], _graph_ref(f"ticker_edges[{key}|{edge['ticker']}]")],
        }

    def add_event(self, origin: str, **fields: Any) -> dict[str, Any]:
        code = fields["code"]
        base: dict[str, Any] = {k: None for k in lc.EVENT_FIELDS}
        base.update(revision=0, effective_precision="UNKNOWN")
        ev = {**base, **{k: _iso(v) for k, v in fields.items()}}
        if code in self.events:
            old = self.events[code]
            for r in ev["evidence"]:
                if r not in old["evidence"]:
                    old["evidence"].append(r)
            self.origin[code].append(origin)
            return old
        self.events[code] = ev
        self.origin[code].append(origin)
        return ev


def _notice_ref(n: dict[str, Any]) -> dict[str, Any]:
    return _graph_ref(f"notices[accession={n['accession']}, security={n['security_key']}]")


def _notice_events(v: V2, fund_ciks: set[str]) -> None:
    """V2 removals and ends from the canonical notices (issuer copies)."""
    term: dict[str, list[tuple[dict[str, Any], str, dict[str, Any]]]] = defaultdict(list)
    seen_v1: dict[str, list[str]] = defaultdict(list)  # V1 removal code -> V2 codes
    for n in sorted(v.g["notices"], key=lambda n: (n["accession"], n["ledger_event"])):
        e1 = v.v1_events[n["ledger_event"]]
        k1 = e1["security_key"]
        s1 = v.v1_secs[k1]
        names = v.issuer_names(n["issuer"] or s1["entity_key"])
        k2, why = v2_key(k1, s1["class_label"], s1["class_description"], names)
        no_issuer = n["issuer"] is None
        if no_issuer:
            # The only copy is the exchange's and the notice names no issuer CIK: the class
            # gets its own key with no issuer, never the exchange as issuer.
            k2, why = f"NOTICE:{n['accession']}|{s1['class_label']}", "NO_ISSUER_CIK"
            v.unresolved["notices_without_issuer"].append(
                {
                    "accession": n["accession"],
                    "security_key": k2,
                    "filed": n["filed"],
                    "class_description": s1["class_description"],
                }
            )
        if no_issuer:
            v.add_security(k2, None, s1["class_label"], s1["class_description"], _notice_ref(n))
        elif why:
            label = k2.split("|", 1)[1]
            v.add_security(k2, s1["entity_key"], label, s1["class_description"], _notice_ref(n))
            v.class_notes.append(
                {
                    "v1_security": k1,
                    "v2_security": k2,
                    "reason": why,
                    "class_description": s1["class_description"],
                    "accession": n["accession"],
                    "terminal": bool(n["terminal_events"]),
                }
            )
        else:
            v.add_security(
                k1, s1["entity_key"], s1["class_label"], s1["class_description"], _notice_ref(n)
            )
        reason, rule_note = e1["reason"], ""
        rx = n.get("rule_x43")
        if rx:
            if rx["status"] == CONFIRMED:
                reason = rx["reason"]
                rule_note = (
                    f" The rule is read from the form's checkbox glyphs (X4.3: {rx['marks']}): "
                    f"exactly one provision is checked, 12d2-2({rx['rule']})."
                )
            else:
                v.unresolved["unreadable_rules"].append(
                    {
                        "accession": n["accession"],
                        "security_key": k2,
                        "filed": n["filed"],
                        "marks": rx["marks"],
                        "explanation": rx["explanation"],
                    }
                )
        venue = e1["venue"]
        code = f"VENUE_REMOVAL|{k2}|{venue or 'UNKNOWN'}|{n['filed']}|{n['form']}"
        linked = n["status"] == CONFIRMED
        evidence = [r for r in e1["evidence"] if n["accession"] in r.get("locator", "")] or list(
            e1["evidence"]
        )
        v.add_event(
            f"V1:{e1['code']}",
            code=code,
            security_key=k2,
            event_type="VENUE_REMOVAL",
            filing_date=n["filed"],
            venue=venue,
            ticker=n["ticker"] if linked else None,
            reason=reason,
            confidence="LOW" if no_issuer else "HIGH",
            resolution_status=UNRESOLVED if no_issuer else CONFIRMED,
            explanation=e1["explanation"]
            + (" The notice names no issuer CIK: the issuer is unresolved." if no_issuer else "")
            + rule_note
            + (
                f" Linked to {n['ticker']} ({n['confidence']}): {n['explanation']}"
                if linked
                else f" Not linked to a ticker: X4.3 {n['status']} ({n['method']}): "
                f"{n['explanation']}"
            ),
            evidence=[*evidence, _notice_ref(n)],
        )
        seen_v1[e1["code"]].append(code)
        rule = next((r for r, (txt, _) in lc.RULES.items() if txt == reason), None)
        if rule and lc.RULES[rule][1]:
            term[k2].append((n, rule, e1))
    v.v1_removal_map = seen_v1
    # One end per class: the earliest terminal notice, bounded by its listing link.
    for k2, items in sorted(term.items()):
        items.sort(key=lambda t: (t[0]["filed"], t[0]["accession"]))
        n, rule, e1 = items[0]
        v1_type = next(
            (
                v.v1_events[c]["event_type"]
                for c in (
                    f"FUND_TERMINATION|{e1['security_key']}",
                    f"DELISTING|{e1['security_key']}",
                )
                if c in v.v1_events
            ),
            None,
        )
        is_fund = (
            k2.startswith("SECCLASS:")
            or v1_type == "FUND_TERMINATION"
            or (v1_type is None and (n["issuer"] or "")[4:] in fund_ciks)
        )
        etype = "FUND_TERMINATION" if is_fund else "DELISTING"
        code = f"{etype}|{k2}"
        v1_same = v.v1_events.get(code)
        nb = na = None
        bound_note = ""
        if v1_same and v1_same["effective_precision"] == "BOUNDED":
            nb, na = v1_same["not_before"], v1_same["not_after"]
        else:
            linked = [x for x, _, _ in items if x["status"] == CONFIRMED and x["observed"]]
            if linked:
                obs = linked[0]["observed"]
                if obs["after_filing"] == "ABSENT":
                    nb, na = obs["last"], obs["next_capture"]
                elif obs["after_filing"] in ("NO_LATER_CAPTURE", "PENDING"):
                    nb = obs["last"]
                if nb:
                    bound_note = (
                        f" Bounded by its X4.3 listing link: listed as {linked[0]['ticker']} "
                        f"on {nb}" + (f", absent on {na}" if na else ", no later capture") + "."
                    )
        tickers = sorted({x["ticker"] for x, _, _ in items if x["status"] == CONFIRMED})
        base_expl = (
            v1_same["explanation"]
            if v1_same
            else f"17 CFR 240.12d2-2({rule[0]})({rule[1:]}) states the whole class is "
            f"{lc.RULES[rule][0].lower().replace('_', ' ')}: the class ends on every venue. "
            "The effective date is not stated in the filing."
        )
        v.add_event(
            f"NOTICES:{','.join(sorted({x['accession'] for x, _, _ in items}))}",
            code=code,
            security_key=k2,
            event_type=etype,
            effective_precision="BOUNDED" if nb else "UNKNOWN",
            not_before=nb,
            not_after=na,
            filing_date=n["filed"],
            venue=e1["venue"],
            ticker=tickers[0] if len(tickers) == 1 else None,
            reason=lc.RULES[rule][0],
            confidence="HIGH",
            resolution_status=CONFIRMED,
            explanation=base_expl + bound_note,
            evidence=[r for x, _, ex in items[:3] for r in ex["evidence"][:1]]
            + [_notice_ref(x) for x, _, _ in items[:3]],
        )
        for _, _, ex in items:
            v.v2_terminal_of_v1[ex["security_key"]].add(code)


def _carry_other(v: V2) -> None:
    """V1 events not derived from notices: listings, transfers, ticker change, mergers,
    classifications. Carried with their keys (named securities are never relabelled)."""
    for code, e in sorted(v.v1_events.items()):
        if e["event_type"] in ("VENUE_REMOVAL", "DELISTING", "FUND_TERMINATION"):
            continue
        k = e["security_key"]
        s = v.v1_secs[k]
        v.add_security(
            k,
            s["entity_key"],
            s["class_label"],
            s["class_description"],
            {"artifact": "x42_ledger_v1", "locator": code},
        )
        fields = {f: e[f] for f in lc.EVENT_FIELDS if f not in ("revision", "evidence_sha256")}
        fields["evidence"] = list(e["evidence"])
        v.add_event(f"V1:{code}", **fields)


# ---------------------------------------------------------------------- proposals
def _captures(x4s: x4.Store) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for rel, e in x4s.entries.items():
        if rel.startswith("archive/symdir") and rel.endswith(".raw") and e.get("digest_verified"):
            out[rel.split("/")[1]].append(lc._day(e["source"]["timestamp"]).isoformat())
    return {k: sorted(v) for k, v in out.items()}


def _terminal_between(v: V2, sec: str, lo: str, hi: str) -> list[str]:
    out = []
    for e in v.events.values():
        if e["security_key"] != sec or e["event_type"] not in lc.TERMINAL:
            continue
        nb, na, fd = e["not_before"], e["not_after"], e["filing_date"]
        # The end can fall inside [lo, hi] unless its bounds or filing place it outside.
        if (na and na < lo) or (nb and nb > hi) or (fd and not nb and not na and fd > hi):
            continue
        out.append(e["code"])
    return out


def _proposals(v: V2, funds: dict[int, list[dict[str, str]]]) -> list[dict[str, Any]]:
    caps = _captures(v.x4s)
    by_class_years: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for y, rows in funds.items():
        for r in rows:
            if r["ticker"]:
                by_class_years[r["class_id"]][r["ticker"]].add(y)
    out = []
    for p in sorted(v.g["ledger_proposals"], key=lambda p: p["proposed_code"]):
        rec: dict[str, Any] = {
            "proposed_code": p["proposed_code"],
            "event_type": p["event_type"],
            "security_key": p["security_key"],
            "from": p["from"],
            "to": p["to"],
            "not_before": p["not_before"],
            "not_after": p["not_after"],
            "x43_status": p["status"],
            "verdict": None,
            "v2_code": None,
            "rules": [],
            "explanation": "",
        }
        sec = p["security_key"]
        if p["in_x42_ledger"]:
            rec.update(verdict="CORROBORATES_V1", v2_code=p["proposed_code"])
            rec["explanation"] = "already a V1 event; the graph shows the same succession"
            out.append(rec)
            continue
        fails: list[str] = []
        if p["status"] != CONFIRMED:
            fails.append("X4.3 left it unresolved (no linked removal from the old venue)")
        lo, hi = p["not_before"], p["not_after"]
        if ends := _terminal_between(v, sec, lo, hi):
            fails.append(f"a terminal event of the security may fall inside the seam: {ends}")
        if any(n["v2_security"] == sec for n in v.class_notes):
            fails.append("the proposal names a relabelled class key: ambiguous class")
        family = "symdir_nasdaqlisted" if p["from"][1] == "NASDAQ" else "symdir_otherlisted"
        conf = "HIGH"
        if p["event_type"] == "TICKER_CHANGE":
            between = [d for d in caps.get(family, []) if lo < d < hi]
            if between:
                fails.append(
                    f"captures of {family} on {between} list neither ticker for the security: "
                    "the succession is not consecutive"
                )
            later_old = [
                e
                for e in v.edges_by_sec.get(sec, [])
                if e["ticker"] == p["from"][0] and e["last_observed"] > hi
            ]
            earlier_new = [
                e
                for e in v.edges_by_sec.get(sec, [])
                if e["ticker"] == p["to"][0] and e["first_observed"] < lo
            ]
            if later_old or earlier_new:
                fails.append("the two tickers are observed out of order for the security")
            if sec.startswith("SECCLASS:"):
                years = by_class_years.get(sec[9:], {})
                old_y, new_y = years.get(p["from"][0], set()), years.get(p["to"][0], set())
                if old_y and new_y and max(old_y) < min(new_y):
                    rec["rules"].append(
                        f"SEC series/class files: {p['from'][0]} in {sorted(old_y)}, "
                        f"{p['to'][0]} in {sorted(new_y)} for the same class"
                    )
                elif old_y and new_y:
                    fails.append("the SEC files give the class both tickers in overlapping years")
                else:
                    conf = "MEDIUM"
                    rec["rules"].append("the SEC files do not show both tickers for the class")
            else:
                conf = "MEDIUM"
                rec["rules"].append("same issuer (listed name) and class, consecutive captures")
        else:
            rem = next(
                (
                    n
                    for n in v.g["notices"]
                    if n["security_key"] == sec
                    and n["venue"] == p["from"][1]
                    and n["reason"] == lc.RULES["c"][0]
                    and n["status"] == CONFIRMED
                    and lo <= n["filed"] <= hi
                ),
                None,
            )
            if rem is None and p["status"] == CONFIRMED:
                fails.append("no confirmed voluntary removal from the old venue in the seam")
            elif rem is not None:
                rec["rules"].append(f"voluntary removal {rem['accession']} filed {rem['filed']}")
                if rem["confidence"] != "HIGH":
                    conf = "MEDIUM"
            later_old = [
                e
                for e in v.edges_by_sec.get(sec, [])
                if e["venue"] == p["from"][1] and e["last_observed"] > hi
            ]
            if later_old:
                fails.append("the security is listed on the old venue again after the move")
        if fails:
            rec.update(verdict="REJECTED", explanation="; ".join(fails))
            out.append(rec)
            continue
        a = next(
            e
            for e in v.edges_by_sec[sec]
            if [e["ticker"], e["venue"]] == p["from"] and e["last_observed"] == lo
        )
        v.listing_security(sec, a)
        graph = _graph_ref(f"ledger_proposals[{p['proposed_code']}]")
        common = {
            "code": p["proposed_code"],
            "security_key": sec,
            "event_type": p["event_type"],
            "effective_precision": "BOUNDED",
            "not_before": lo,
            "not_after": hi,
            "confidence": conf,
            "resolution_status": CONFIRMED,
            "evidence": [*p["evidence"], graph],
        }
        if p["event_type"] == "EXCHANGE_TRANSFER":
            rem_n = next(
                n
                for n in v.g["notices"]
                if n["security_key"] == sec
                and n["venue"] == p["from"][1]
                and lo <= n["filed"] <= hi
                and n["reason"] == lc.RULES["c"][0]
                and n["status"] == CONFIRMED
            )
            v.add_event(
                f"PROPOSAL:{p['proposed_code']}",
                **common,
                filing_date=rem_n["filed"],
                ticker=p["from"][0],
                from_venue=p["from"][1],
                to_venue=p["to"][1],
                explanation=f"Listed as {p['from'][0]} on {p['from'][1]} until at least {lo}, "
                f"then on {p['to'][1]} from {hi}; the issuer's voluntary removal from "
                f"{p['from'][1]} ({rem_n['accession']}, filed {rem_n['filed']}) is linked to "
                "this security. The listing moved; the security continued. Bounded, not dated.",
            )
        else:
            v.add_event(
                f"PROPOSAL:{p['proposed_code']}",
                **common,
                venue=p["from"][1],
                from_ticker=p["from"][0],
                to_ticker=p["to"][0],
                explanation=f"The same security ({sec}) is listed on {p['from'][1]} as "
                f"{p['from'][0]} until at least {lo} and as {p['to'][0]} from {hi}, with no "
                f"capture in between; {'; '.join(rec['rules'])}. Bounded, not dated.",
            )
        rec.update(verdict="ACCEPTED", v2_code=p["proposed_code"], confidence=conf)
        rec["explanation"] = "; ".join(rec["rules"])
        out.append(rec)
    return out


# ---------------------------------------------------------------------- the seven funds
FUNDS_2016 = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def _seven_funds(v: V2) -> list[dict[str, Any]]:
    """Listing observations (first and last capture of each graph edge) for the 2016 funds
    missing from V5, where the security is identified; the rest stay unresolved."""
    out = []
    for t in FUNDS_2016:
        edges = [e for e in v.g["ticker_edges"] if e["ticker"] == t and e["confidence"] == "HIGH"]
        for e in edges:
            sec = e["security_key"]
            v.listing_security(sec, e)
            for when, ref in (
                (e["first_observed"], e["evidence"][0]),
                (e["last_observed"], e["evidence"][-1]),
            ):
                fam = ref["artifact"].split("/")[-2]
                v.add_event(
                    f"FUND_2016:{t}",
                    code=f"LISTING_OBSERVED|{sec}|{t}|{fam}|{when}",
                    security_key=sec,
                    event_type="LISTING_OBSERVED",
                    effective_date=when,
                    effective_precision="EXACT",
                    ticker=t,
                    venue=e["venue"],
                    confidence="HIGH",
                    resolution_status=CONFIRMED,
                    explanation=f"Listed as {t} on {e['venue']} in the {fam} capture of {when} "
                    f"({e['listed_names'][-1]!r}); X4.3 identifies the security by "
                    + ("its SEC share class" if sec.startswith("SECCLASS:") else "its listed name")
                    + ". "
                    "The capture date is an observation, not the listing's start or end.",
                    evidence=[ref, _graph_ref(f"ticker_edges[{sec}|{t}|{e['venue']}]")],
                )
            out.append(
                {
                    "ticker": t,
                    "security_key": sec,
                    "venue": e["venue"],
                    "first": e["first_observed"],
                    "last": e["last_observed"],
                }
            )
        loose = [u for u in v.g["unresolved_listings"] if u["ticker"] == t]
        if not edges and loose:
            v.unresolved["funds_2016_issuer_unknown"].append(
                {
                    "ticker": t,
                    "listed_names": sorted({u["listed_name"] for u in loose}),
                    "first": min(u["first_observed"] for u in loose),
                    "last": max(u["last_observed"] for u in loose),
                    "explanation": "listed, but the listed name identifies no SEC filer or class",
                }
            )
    return out


# ---------------------------------------------------------------------- provider histories
def _bars(x43s: x43.Store, asset: str) -> list[list[Any]]:
    rel = f"db/provider_bars/{asset[0]}.json.gz"
    key = ("bars", rel)
    if key not in x43s.cache:
        x43s.cache[key] = x4.read_json(x43s.path(rel))
    rows: list[list[Any]] = x43s.cache[key].get(asset, [])
    return rows


def _edges_of_ticker(g: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in g["ticker_edges"]:
        out[e["ticker"]].append(e)
    for v in out.values():
        v.sort(key=lambda e: (e["first_observed"], e["last_observed"]))
    return out


def _jumps(series: list[list[Any]]) -> list[dict[str, Any]]:
    out = []
    for a, b in itertools.pairwise(series):
        sa, sb = a[2], b[2]
        if sa is None or sb is None or float(sa) <= 0 or float(sb) <= 0:
            continue
        if abs(math.log(float(sb) / float(sa))) >= JUMP_LOG:
            raw_a, raw_b = float(a[1]), float(b[1])
            out.append(
                {
                    "from": a[0],
                    "to": b[0],
                    "split_close": [float(sa), float(sb)],
                    "raw_close": [raw_a, raw_b],
                    "raw_ratio": round(raw_b / raw_a, 4),
                    "adjustment_factor_before": round(float(sa) / raw_a, 4),
                }
            )
    return out


def _provider_histories(
    v: V2, x43s: x43.Store, dividends: dict[tuple[str, str], list[dict[str, Any]]]
) -> dict[str, Any]:
    g = v.g
    by_ticker = _edges_of_ticker(g)
    links = {p["asset_id"]: p for p in g["provider_links"]}
    successors = {n["ticker"]: n for n in g["notices"] if n.get("successor")}
    identities = []
    jumps_out = []
    for asset, p in sorted(links.items()):
        series = _bars(x43s, asset)
        tickers = sorted({t for f in p["formations"] for t in f["tickers"]})
        segs: list[dict[str, Any]] = []
        sec = p["security_key"] if p["status"] != UNRESOLVED else None
        holders = [
            (f["formation"], h["entity"], h["security"])
            for f in p["formations"]
            for h in f["holders"]
            if h["status"] == CONFIRMED
        ]
        ents = sorted({e for _, e, _ in holders})
        first, last = p["first_bar"], p["last_bar"]
        jumps = _jumps(series)
        cuts: list[tuple[str, str, str, dict[str, Any]]] = []  # (last_of_a, first_of_b, why, info)
        # (1) Ticker holders change inside the bars (one ticker, two issuers over time) while
        # this history trades under that ticker: the nearest formation before the change and
        # the nearest after it (where they exist) must both show this history as the ticker.
        fticks = sorted((f["formation"], set(f["tickers"])) for f in p["formations"])
        for t in tickers:
            es = by_ticker.get(t, [])
            for a, b in itertools.pairwise(es):
                if a["entity_key"] == b["entity_key"] or a["last_observed"] >= b["first_observed"]:
                    continue
                if b["first_observed"] < first or a["last_observed"] > last:
                    continue
                before = [ts for f, ts in fticks if f <= a["last_observed"]]
                after = [ts for f, ts in fticks if f >= b["first_observed"]]
                if (before and t not in before[-1]) or (after and t not in after[0]):
                    continue  # this history reached or left the ticker by its own change
                inside = [j for j in jumps if a["last_observed"] < j["to"] <= b["first_observed"]]
                info = {
                    "ticker": t,
                    "before": a["security_key"],
                    "after": b["security_key"],
                    "listed_until": a["last_observed"],
                    "listed_from": b["first_observed"],
                }
                if len(inside) == 1:
                    cuts.append(
                        (
                            inside[0]["from"],
                            inside[0]["to"],
                            "TICKER_HOLDER_CHANGE_AT_JUMP",
                            info | {"jump": inside[0]},
                        )
                    )
                else:
                    cuts.append(
                        (a["last_observed"], b["first_observed"], "TICKER_HOLDER_CHANGE_ZONE", info)
                    )
        # (2) The linked security ended (its ending notice linked to this ticker).
        for f in p["findings"]:
            if f["kind"] == "BARS_AFTER_SECURITY_END":
                code = f["terminal_event"]
                n = next(
                    (
                        x
                        for x in g["notices"]
                        if x["status"] == CONFIRMED
                        and x["ticker"] in tickers
                        and x["terminal_events"]
                        and code in x["terminal_events"]
                    ),
                    None,
                )
                filed = n["filed"] if n else f["end_no_later_than"]
                later_bars = [str(r[0]) for r in series if r[0] > filed]
                if later_bars:
                    cuts.append(
                        (
                            filed,
                            later_bars[0],
                            "SECURITY_ENDED",
                            {
                                "terminal_event": code,
                                "filed": filed,
                                "successor": successors.get(n["ticker"]) is not None
                                if n
                                else False,
                            },
                        )
                    )
        # REDACTED_FOR_GITHUB
        # REDACTED_FOR_GITHUB
        fh = sorted({(f, e, s) for f, e, s in holders})
        for (f1, e1, s1), (f2, e2, s2) in itertools.pairwise(fh):
            if e1 == e2:
                continue
            a_last = max(
                (
                    e["last_observed"]
                    for t in tickers
                    for e in by_ticker.get(t, [])
                    if e["entity_key"] == e1 and e["last_observed"] <= f2
                ),
                default=f1,
            )
            b_first = min(
                (
                    e["first_observed"]
                    for t in tickers
                    for e in by_ticker.get(t, [])
                    if e["entity_key"] == e2 and e["first_observed"] >= a_last
                ),
                default=f2,
            )
            inside = [j for j in jumps if a_last < j["to"] <= b_first]
            info = {
                "before": s1,
                "after": s2,
                "listed_until": a_last,
                "listed_from": b_first,
                "formations": [f1, f2],
            }
            if len(inside) == 1:
                cuts.append(
                    (
                        inside[0]["from"],
                        inside[0]["to"],
                        "ISSUER_CHANGE_AT_JUMP",
                        info | {"jump": inside[0]},
                    )
                )
            else:
                cuts.append((a_last, b_first, "ISSUER_CHANGE_ZONE", info))
        prio = {
            "TICKER_HOLDER_CHANGE_AT_JUMP": 0,
            "ISSUER_CHANGE_AT_JUMP": 1,
            "TICKER_HOLDER_CHANGE_ZONE": 2,
            "ISSUER_CHANGE_ZONE": 3,
            "SECURITY_ENDED": 4,
        }
        ordered = sorted(
            {(a, b, w, json.dumps(i, sort_keys=True)) for a, b, w, i in cuts},
            key=lambda c: (c[0], prio[c[2]], c[1]),
        )
        if not ordered:
            segs.append(
                {
                    "from": first,
                    "to": last,
                    "security_key": sec,
                    "status": CONFIRMED if sec else UNRESOLVED,
                    "basis": "X4.3 price fingerprint at each formation and the holder "
                    "of that ticker from the listings"
                    if sec
                    else "no holder established in X4.3",
                }
            )
        else:
            a_last, b_first, why, info_s = ordered[0]
            info = json.loads(info_s)
            pre = info.get("before") if why != "SECURITY_ENDED" else sec
            post = info.get("after") if why != "SECURITY_ENDED" else None
            same_linked = sec in (pre, post)
            segs.append(
                {
                    "from": first,
                    "to": a_last,
                    "security_key": pre,
                    "status": CONFIRMED if pre else UNRESOLVED,
                    "basis": why,
                    "detail": info,
                }
            )
            if b_first > a_last and why.endswith("_ZONE"):
                segs.append(
                    {
                        "from": a_last,
                        "to": b_first,
                        "security_key": None,
                        "status": UNRESOLVED,
                        "basis": "between the last listing of one "
                        "holder and the first of the next: bars here are attributed to "
                        "neither",
                        "exclusive": True,
                    }
                )
            segs.append(
                {
                    "from": b_first,
                    "to": last,
                    "security_key": post,
                    "status": CONFIRMED if post else UNRESOLVED,
                    "basis": why + (": the successor is not identified" if not post else ""),
                    "detail": info,
                }
            )
            _ = same_linked
        clipped = []
        for sg in segs:
            lo, hi = max(sg["from"], first), min(sg["to"], last)
            if lo <= hi:
                clipped.append({**sg, "from": lo, "to": hi})
        segs = clipped
        verdict = (
            "SINGLE_SECURITY"
            if len(segs) == 1 and segs[0]["status"] == CONFIRMED
            else "UNATTRIBUTED"
            if len(segs) == 1
            else "SPLIT: never use one security's prices for another"
        )
        identities.append(
            {
                "asset_id": asset,
                "request": p["request"],
                "first_bar": first,
                "last_bar": last,
                "x43_status": p["status"],
                "linked_security": p["security_key"],
                "holders": holders,
                "issuers_at_formations": ents,
                "segments": segs,
                "verdict": verdict,
            }
        )
        for j in jumps:
            jumps_out.append(_classify_jump(asset, p, j, tickers, ordered, dividends))
    return {"identities": identities, "jumps": jumps_out}


def _classify_jump(
    asset: str,
    p: dict[str, Any],
    j: dict[str, Any],
    tickers: list[str],
    cuts: list[tuple[str, str, str, str]],
    dividends: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    rec = {"asset_id": asset, "label": p["request"]["label"], **j}
    at_cut = [c for c in cuts if (c[0] <= j["from"] and j["to"] <= c[1]) or c[1] == j["to"]]
    divs = [d for t in tickers for d in dividends.get((t, j["to"]), [])]
    raw_ok = 1 / 3 < j["raw_ratio"] < 3
    if at_cut:
        rec.update(
            finding="SECURITY_BOUNDARY",
            status=CONFIRMED,
            explanation="the jump is where the series passes to another security "
            f"({at_cut[0][2]}): not a price move of one security",
        )
    elif raw_ok and abs(j["adjustment_factor_before"] - 1) > 0.05:
        rec.update(
            finding="PROVIDER_ADJUSTMENT_FACTOR",
            status=CONFIRMED,
            explanation=f"raw closes move only {j['raw_ratio']:.2f}x; the provider's split-"
            f"adjusted history multiplies earlier bars by {j['adjustment_factor_before']:g}. "
            "The provider records an adjustment the raw prices do not show (consistent with a "
            "distribution such as a spin-off). The corporate action itself is not in the "
            "evidence: UNRESOLVED as an action, CONFIRMED as a provider adjustment artifact",
            corporate_action=UNRESOLVED,
        )
    elif (
        divs
        and abs(
            (j["raw_close"][0] - float(divs[0]["rate"]) - j["raw_close"][1]) / j["raw_close"][0]
        )
        < 0.1
    ):
        d = divs[0]
        rec.update(
            finding="CASH_DISTRIBUTION",
            status=CONFIRMED,
            explanation=f"a provider cash distribution of {d['rate']} on {d['symbol']} with "
            f"ex-date {j['to']} accounts for the drop ({j['raw_close'][0]} - {d['rate']} = "
            f"{j['raw_close'][0] - float(d['rate']):.2f}, close {j['raw_close'][1]})",
            dividend_id=d["id"],
        )
    else:
        rec.update(
            finding="RAW_MOVE_UNEXPLAINED",
            status=UNRESOLVED,
            explanation=f"the raw close itself moves {j['raw_ratio']:.2f}x with no corporate "
            "action, distribution or change of security in the evidence. A market move is "
            "possible; nothing is assumed",
        )
    return rec


def _joins(v: V2, x43s: x43.Store) -> list[dict[str, Any]]:
    """Join determinations for the truncated or segmented histories (J1-J5)."""
    g = v.g
    probes = x43.probe_bars(x43s)
    links = {p["asset_id"]: p for p in g["provider_links"]}
    out = []

    def transfer_or_change(sec: str, seam: str) -> list[str]:
        return [
            e["code"]
            for e in v.events.values()
            if e["security_key"] == sec
            and e["event_type"] in ("EXCHANGE_TRANSFER", "TICKER_CHANGE")
            and e["resolution_status"] == CONFIRMED
            and (e["not_before"] or "") <= seam <= (e["not_after"] or "9999")
        ]

    def listed_across(sec: str, seam: str) -> bool:
        es = v.edges_by_sec.get(sec, [])
        return any(e["first_observed"] <= seam for e in es) and any(
            e["last_observed"] > seam for e in es
        )

    def ends_in(sec: str, lo: str, hi: str) -> list[str]:
        return _terminal_between(v, sec, lo, hi)

    def decide(
        rec: dict[str, Any], sec: str | None, seam: str, overlap: int, same: bool | None
    ) -> None:
        j1 = sec is not None
        j2 = overlap >= rs.MIN_FINGERPRINT and bool(same)
        explained = transfer_or_change(sec, seam) if sec else []
        mapping = sec is not None and listed_across(sec, seam)
        j3 = bool(explained) or mapping
        end = ends_in(sec, seam, seam) if sec else []
        j4 = not end
        rec["rules"] = {
            "J1_SAME_SECURITY": j1,
            "J2_IDENTICAL_OVERLAP": j2,
            "J3_EXPLAINED_SEAM": j3,
            "J4_NO_END_OR_SUCCESSOR": j4,
        }
        rec["seam_events"] = explained
        rec["ends_in_seam"] = end
        rec["determination"] = (
            "JOIN_PERMITTED"
            if j1 and j2 and j3 and j4
            else "JOIN_UNVERIFIED"
            if j1 and j3 and j4
            else "JOIN_FORBIDDEN"
        )

    # REDACTED_FOR_GITHUB
    for p in g["provider_links"]:
        for f in p["findings"]:
            if f["kind"] != "CONTINUED_BY":
                continue
            q = links[f["asset_id"]]
            rec = {
                "kind": "RESEARCH_IDENTITIES",
                "earlier": p["asset_id"],
                "later": q["asset_id"],
                "ticker": p["request"]["ticker"],
                "seam": p["last_bar"],
                "overlap_sessions": f["identical_sessions"],
                "identical": True,
                "security_key": p["security_key"],
            }
            sec = p["security_key"] if p["security_key"] == q["security_key"] else None
            decide(rec, sec, p["last_bar"], f["identical_sessions"], True)
            out.append(rec)
    # REDACTED_FOR_GITHUB
    for t, pr in sorted(g["probes"].items()):
        for ch in pr["segment_changes"]:
            edges = [e for e in g["ticker_edges"] if e["ticker"] == t and e["confidence"] == "HIGH"]
            seam = ch.get("earlier_mapping_ends") or ch.get("later_mapping_starts")
            near = [e for e in edges if e["first_observed"] <= "2019-12-31"]
            secs = {e["security_key"] for e in near}
            sec = next(iter(secs)) if len(secs) == 1 else None
            rec = {
                "kind": "PROVIDER_MAPPING",
                "ticker": t,
                "mapped_before": ch["mapped_before"],
                "mapped_after": ch["mapped_after"],
                "seam": seam,
                "overlap_sessions": ch["common_sessions"],
                "identical": ch["identical_on_common"],
                "change": "later mapping starts later"
                if "later_mapping_starts" in ch
                else "earlier mapping ends earlier",
                "security_key": sec,
                "securities_listed_under_ticker": sorted(secs),
            }
            if "later_mapping_starts" in ch and ch["common_sessions"]:
                # The earlier mapping already holds the whole series: nothing to join.
                decide(rec, sec, seam, ch["common_sessions"], ch["identical_on_common"])
                rec["note"] = "the earlier mapping holds one continuous series; later mappings "
                rec["note"] += "lack the bars before " + ch["later_mapping_starts"]
            else:
                decide(rec, sec, seam, ch["common_sessions"], ch["identical_on_common"])
            if not ch["common_sessions"]:
                a = dict(probes[date.fromisoformat(ch["mapped_before"])].get(t, []))
                b = dict(probes[date.fromisoformat(ch["mapped_after"])].get(t, []))
                if a and b:
                    la, fb = max(a), min(b)
                    rec["seam_closes"] = {
                        la: a[la],
                        fb: b[fb],
                        "return": round(b[fb] / a[la] - 1, 6),
                    }
            out.append(rec)
    return out


def _successors(v: V2) -> list[dict[str, Any]]:
    out = []
    for n in sorted(v.g["notices"], key=lambda n: (n["filed"], n["accession"])):
        s = n.get("successor")
        if not s:
            continue
        after = [
            e
            for e in v.g["ticker_edges"]
            if e["ticker"] == s["ticker"]
            and e["first_observed"] >= s["on"]
            and e["entity_key"] == n["issuer"]
            and e["security_key"] != n["security_key"]
        ]
        out.append(
            {
                "predecessor": n["security_key"],
                "ticker": s["ticker"],
                "notice": n["accession"],
                "filed": n["filed"],
                "reason": n["reason"],
                "observed_after": n["observed"]["after_filing"],
                "successor_security": after[0]["security_key"] if after else None,
                "status": CONFIRMED if after else UNRESOLVED,
                "relationship": "SUCCESSOR_CLASS" if after else "SUCCESSOR_UNIDENTIFIED",
                "price_inheritance": "FORBIDDEN",
                "explanation": (
                    f"after the conversion another class of {n['issuer']} "
                    f"({after[0]['security_key']}) is listed as {s['ticker']}: a separate security"
                    if after
                    else f"{s['ticker']} is still listed for {n['issuer']} after the conversion: "
                    "a successor security, not identified (no class or exchange ratio in the "
                    "evidence)"
                ),
                "evidence": [_notice_ref(n)],
            }
        )
    return out


def _name_candidates(v: V2) -> list[dict[str, Any]]:
    """Unresolved X4.3 listings whose issuer name equals an EDGAR name once spaces are
    REDACTED_FOR_GITHUB
    rule did not match them and V2 does not apply them; a graph revision must."""
    ed = v.edgar()
    squash: dict[str, set[str]] = defaultdict(set)
    for cik, names in ed.names.items():
        for n in names:
            squash[rs.nkey(n).replace(" ", "")].add(cik)
    out = []
    for u in v.g["unresolved_listings"]:
        if u["method"] != "NO_EDGAR_NAME":
            continue
        issuer = rs.split_listing(u["evidence"][0]["artifact"].split("/")[-2], u["listed_name"])[0]
        key = rs.nkey(issuer).replace(" ", "")
        hits = squash.get(key, set()) if key else set()
        if len(hits) == 1:
            out.append(
                {
                    "ticker": u["ticker"],
                    "venue": u["venue"],
                    "listed_name": u["listed_name"],
                    "candidate": f"CIK:{next(iter(hits))}",
                    "status": UNRESOLVED,
                    "method": "SPACE_INSENSITIVE_NAME (not applied)",
                }
            )
    return out


# ---------------------------------------------------------------------- dispositions
def _dispositions(v: V2) -> None:
    dup_of: dict[str, str] = {}
    for n in v.g["notices"]:
        for d in n["duplicate_events"]:
            if d != n["ledger_event"]:  # a notice with no issuer CIK lists its own event
                dup_of[d] = n["ledger_event"]
    removal_map = v.v1_removal_map
    term_map = v.v2_terminal_of_v1
    # Exchange-alias securities -> issuer securities (via duplicate pairs).
    alias: dict[str, set[str]] = defaultdict(set)
    for d, own in dup_of.items():
        alias[v.v1_events[d]["security_key"]].add(v.v1_events[own]["security_key"])
    for code, e in sorted(v.v1_events.items()):
        t, k = e["event_type"], e["security_key"]
        if t == "VENUE_REMOVAL":
            if code in dup_of:
                targets = removal_map[dup_of[code]]
                v.v1_disp[code] = {
                    "disposition": DUPLICATE,
                    "v2": sorted(set(targets)),
                    "why": "exchange-index copy of the issuer's notice",
                }
                continue
            targets = sorted(set(removal_map[code]))
        elif t in lc.TERMINAL:
            if k in alias:
                targets = sorted({c for own in alias[k] for c in term_map.get(own, set())})
                v.v1_disp[code] = {
                    "disposition": DUPLICATE,
                    "v2": targets,
                    "why": "derived from exchange-index copies of issuer notices",
                }
                continue
            targets = sorted(term_map.get(k, set()))
        else:
            targets = [code]
        if not targets:
            v.v1_disp[code] = {
                "disposition": "SUPERSEDED_NO_END",
                "v2": [],
                "why": "no notice of this class ends it in V2",
            }
        elif targets == [code]:
            new, old = _comparable(v.events[code]), _comparable(e)
            changed = sorted(k for k in new if new[k] != old[k])
            v.v1_disp[code] = {"disposition": CARRIED if not changed else REVISED, "v2": targets}
            if changed:
                v.v1_disp[code]["changed_fields"] = changed
        else:
            v.v1_disp[code] = {
                "disposition": RECLASSIFIED,
                "v2": targets,
                "why": "the class it belongs to has another V2 key",
            }
    for code, ev in v.events.items():
        if code in v.v1_events and v.v1_disp[code]["disposition"] == REVISED:
            ev["revision"] = v.v1_events[code]["revision"] + 1
    for k in sorted(v.v1_secs):
        if k in alias:
            v.sec_disp[k] = {
                "disposition": "EXCHANGE_ALIAS",
                "v2": sorted(alias[k]),
                "why": "keyed to the exchange's CIK by an exchange-index copy",
            }
        elif k in v.reused_keys:
            moved = sorted({n["v2_security"] for n in v.class_notes if n["v1_security"] == k})
            v.sec_disp[k] = {
                "disposition": "RELABELLED_KEY_REUSED",
                "v2": [k, *moved],
                "why": "V1's classes under this key moved to their own keys; the "
                "key now holds the listed class X4.3 identifies",
            }
        elif k in v.securities:
            moved = sorted({n["v2_security"] for n in v.class_notes if n["v1_security"] == k})
            v.sec_disp[k] = {
                "disposition": CARRIED if not moved else "CARRIED_SPLIT",
                "v2": [k, *moved],
            }
        else:
            moved = sorted({n["v2_security"] for n in v.class_notes if n["v1_security"] == k})
            v.sec_disp[k] = {"disposition": "RELABELLED" if moved else "UNUSED", "v2": moved}
    used = {s["entity_key"] for s in v.securities.values() if s["entity_key"]}
    for k in list(v.entities):
        if k not in used:
            del v.entities[k]


# ---------------------------------------------------------------------- build
def _dividends(x4s: x4.Store) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rel in sorted(k for k in x4s.entries if k.startswith("provider/dividends/")):
        for page in x4.read_json(x4s.path(rel))["pages"]:
            for d in ((page.get("body") or {}).get("corporate_actions") or {}).get(
                "cash_dividends"
            ) or []:
                out[(d["symbol"], d["ex_date"])].append(d)
    return out


def build(
    x4s: x4.Store,
    x43s: x43.Store,
    ledger: dict[str, Any],
    graph: dict[str, Any],
    ledger_sha256: str = rs.LEDGER_SHA256,
    graph_sha256: str = GRAPH_SHA256,
) -> dict[str, Any]:
    v = V2(ledger, graph, x4s)
    funds = lc.fund_classes(x4s)
    fund_ciks = {r["cik"] for rows in funds.values() for r in rows}
    _notice_events(v, fund_ciks)
    _carry_other(v)
    proposals = _proposals(v, funds)
    funds_2016 = _seven_funds(v)
    for e in v.events.values():
        e["evidence_sha256"] = lc._sha(e["evidence"])
    _dispositions(v)
    for e in v.events.values():
        e["evidence_sha256"] = lc._sha(e["evidence"])
    histories = _provider_histories(v, x43s, _dividends(x4s))
    joins = _joins(v, x43s)
    successors = _successors(v)
    events = sorted(v.events.values(), key=lambda e: e["code"])
    issues = lc.validate(
        [
            {
                **e,
                **{
                    k: (date.fromisoformat(e[k]) if e[k] else None)
                    for k in ("effective_date", "not_before", "not_after", "filing_date")
                },
            }
            for e in events
        ],
        v.securities,
    )
    for k, items in (
        (
            "unattributed_provider_segments",
            [
                {"asset_id": i["asset_id"], "label": i["request"]["label"], **s}
                for i in histories["identities"]
                for s in i["segments"]
                if s["status"] != CONFIRMED
            ],
        ),
        ("unexplained_price_jumps", [j for j in histories["jumps"] if j["status"] != CONFIRMED]),
        ("successors_unidentified", [s for s in successors if s["status"] != CONFIRMED]),
        ("joins_unverified", [j for j in joins if j["determination"] == "JOIN_UNVERIFIED"]),
        ("listing_name_candidates", _name_candidates(v)),
        (
            "notices_without_ticker",
            [
                {
                    "accession": n["accession"],
                    "method": n["method"],
                    "category": n["class_category"],
                }
                for n in graph["notices"]
                if n["status"] != CONFIRMED
            ],
        ),
    ):
        v.unresolved[k] = items
    doc: dict[str, Any] = {
        "kind": "x431_lifecycle_ledger_v2",
        "decision": "D-248",
        "set_code": SET_CODE,
        "version": 2,
        "supersedes": {
            "set_code": ledger["set_code"],
            "ledger_sha256": ledger_sha256,
            "content_sha256": ledger["content_sha256"],
            "rule": "V2 replaces V1 for every use. V1 stays as recorded; each V1 event and "
            "security has a disposition here.",
        },
        "inputs": {
            "x41_manifest_sha256": x4.sha256(x4s.manifest_path.read_bytes()),
            "x43_manifest_sha256": x4.sha256(x43s.manifest_path.read_bytes())
            if x43s.manifest_path.exists()
            else None,
            "x42_ledger_sha256": ledger_sha256,
            "x43_graph_sha256": graph_sha256,
            "x43_graph_content_sha256": graph["content_sha256"],
        },
        "evidence_manifest_sha256": ledger["evidence_manifest_sha256"],
        "join_rules": JOIN_RULES,
        "entities": sorted(v.entities.values(), key=lambda e: e["entity_key"]),
        "securities": sorted(v.securities.values(), key=lambda s: s["security_key"]),
        "events": events,
        "event_origins": {k: sorted(set(o)) for k, o in sorted(v.origin.items())},
        "v1_events": dict(sorted(v.v1_disp.items())),
        "v1_securities": dict(sorted(v.sec_disp.items())),
        "class_corrections": sorted(
            v.class_notes, key=lambda n: (n["v2_security"], n["accession"])
        ),
        "proposals": proposals,
        "funds_2016": funds_2016,
        "provider_histories": histories,
        "joins": joins,
        "successors": successors,
        "unresolved": {k: v.unresolved[k] for k in sorted(v.unresolved)},
        "validation_issues": issues,
    }
    doc["content_sha256"] = lc.content_sha256(events)
    doc["reconciliation"] = reconcile(doc, ledger, graph)
    doc["document_sha256"] = document_sha256(doc)
    return doc


def reconcile(doc: dict[str, Any], ledger: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    v1 = Counter(e["event_type"] for e in ledger["events"])
    v2 = Counter(e["event_type"] for e in doc["events"])
    types = {e["code"]: e["event_type"] for e in ledger["events"]}
    v2_types = {e["code"]: e["event_type"] for e in doc["events"]}
    disp = Counter(f"{types[c]}|{d['disposition']}" for c, d in doc["v1_events"].items())
    origins = Counter(o.split(":", 1)[0] for os_ in doc["event_origins"].values() for o in os_)
    new_codes = [c for c in doc["event_origins"] if c not in types]
    accessions = {n["accession"] for n in graph["notices"]}
    return {
        "v1_events": dict(sorted(v1.items())),
        "v1_total": sum(v1.values()),
        "v2_events": dict(sorted(v2.items())),
        "v2_total": sum(v2.values()),
        "v1_dispositions": dict(sorted(disp.items())),
        "every_v1_event_has_a_disposition": len(doc["v1_events"]) == len(ledger["events"]),
        "v2_event_origins": dict(sorted(origins.items())),
        "v2_events_new": len(new_codes),
        "v2_new_by_type": dict(sorted(Counter(v2_types[c] for c in new_codes).items())),
        "notices": {
            "unique_accessions": len(accessions),
            "index_copies": graph["summary"]["notice_index_copies"],
            "notice_targets": len(graph["notices"]),
            "v2_removal_events": v2["VENUE_REMOVAL"],
            "rules_read_from_glyphs": sum(
                1
                for n in graph["notices"]
                if n.get("rule_x43") and n["rule_x43"]["status"] == CONFIRMED
            ),
            "rules_unreadable": len(doc["unresolved"].get("unreadable_rules", [])),
        },
        "securities": {
            "v1": len(ledger["securities"]),
            "v2": len(doc["securities"]),
            "v1_dispositions": dict(
                sorted(Counter(d["disposition"] for d in doc["v1_securities"].values()).items())
            ),
        },
        "entities": {"v1": len(ledger["entities"]), "v2": len(doc["entities"])},
        "proposals": dict(
            sorted(Counter(f"{p['event_type']}|{p['verdict']}" for p in doc["proposals"]).items())
        ),
    }


def document_sha256(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k not in ("document_sha256",)}
    return hashlib.sha256(x4.canonical(rs._plain(body))).hexdigest()


def write(doc: dict[str, Any], path: Path = V2_PATH) -> str:
    """Write V2 once (deterministic gzip); never replace an existing file."""
    if path.exists():
        raise FileExistsError(f"{path} exists: the ledger V2 is written once")
    data = x4.gz(x4.canonical(rs._plain(doc)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def verify(path: Path, x4s: x4.Store, x43s: x43.Store) -> dict[str, Any]:
    """Offline: recompute the stored hashes, then rebuild V2 from the inputs and compare."""
    stored = x4.read_json(path)
    ledger = rs.load_ledger()
    graph = load_graph()
    rebuilt = build(x4s, x43s, ledger, graph)
    return {
        "file_sha256": _sha_file(path),
        "stored_content_sha256": stored["content_sha256"],
        "recomputed_content_sha256": lc.content_sha256(stored["events"]),
        "stored_document_sha256": stored["document_sha256"],
        "recomputed_document_sha256": document_sha256(stored),
        "rebuilt_document_sha256": rebuilt["document_sha256"],
        "ok": stored["content_sha256"]
        == lc.content_sha256(stored["events"])
        == rebuilt["content_sha256"]
        and stored["document_sha256"] == document_sha256(stored) == rebuilt["document_sha256"],
    }


# ---------------------------------------------------------------------- registration
def register(factory: sessionmaker[Session], path: Path) -> dict[str, Any]:
    """Register V2 into the 0028 tables beside V1, once. Rows V1 already holds (same code
    and revision) must be identical; everything else is added. The set row names V1 as
    superseded."""
    raw = path.read_bytes()
    doc = x4.read_json(path)
    if lc.content_sha256(doc["events"]) != doc["content_sha256"]:
        raise ValueError("the V2 events do not hash to its content_sha256")
    with factory() as session, session.begin():
        s = SecurityLifecycleSetRecord
        if session.scalar(select(s.id).where(s.code == doc["set_code"])) is not None:
            raise ValueError(f"{doc['set_code']} is already registered (append-only)")
        have_ent = set(session.scalars(select(SecurityEntityRecord.entity_key)))
        session.add_all(
            SecurityEntityRecord(**e, created_by=CREATED_BY)
            for e in doc["entities"]
            if e["entity_key"] not in have_ent
        )
        session.flush()
        have_sec = set(session.scalars(select(SecurityIdentityRecord.security_key)))
        session.add_all(
            SecurityIdentityRecord(**x, created_by=CREATED_BY)
            for x in doc["securities"]
            if x["security_key"] not in have_sec
        )
        session.flush()
        existing = {
            (r.code, r.revision): r for r in session.scalars(select(SecurityLifecycleEventRecord))
        }
        added = 0
        for e in doc["events"]:
            row = existing.get((e["code"], e["revision"]))
            values = {k: e[k] for k in lc.EVENT_FIELDS}
            for k in ("effective_date", "not_before", "not_after", "filing_date"):
                values[k] = lc._date(values[k])
            if row is not None:
                if any(getattr(row, k) != values[k] for k in lc.EVENT_FIELDS):
                    raise ValueError(f"{e['code']} r{e['revision']} differs from the stored row")
                continue
            session.add(SecurityLifecycleEventRecord(**values, created_by=CREATED_BY))
            added += 1
        session.flush()
        members = {(e["code"], e["revision"]) for e in doc["events"]}
        stored = [
            {k: _iso(getattr(r, k)) for k in lc.EVENT_FIELDS}
            for r in session.scalars(select(SecurityLifecycleEventRecord))
            if (r.code, r.revision) in members
        ]
        sha = lc.content_sha256(stored)
        if sha != doc["content_sha256"]:
            raise ValueError("the stored V2 events do not hash to its content_sha256")
        session.add(
            SecurityLifecycleSetRecord(
                code=doc["set_code"],
                version=2,
                description="Security lifecycle ledger V2: V1 reconciled with the X4.3 identity "
                "graph (D-248)",
                rule={"supersedes": doc["supersedes"], "join_rules": doc["join_rules"]},
                members=[{"code": c, "revision": r} for c, r in sorted(members)],
                content_sha256=sha,
                ledger_sha256=hashlib.sha256(raw).hexdigest(),
                evidence_manifest_sha256=doc["evidence_manifest_sha256"],
                created_by=CREATED_BY,
            )
        )
    return {
        "set": doc["set_code"],
        "events": len(members),
        "added_events": added,
        "content_sha256": sha,
    }


__all__ = [
    "JOIN_RULES",
    "SET_CODE",
    "V2_PATH",
    "build",
    "load_graph",
    "named_class",
    "register",
    "v2_key",
    "verify",
    "write",
]
