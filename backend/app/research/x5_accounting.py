# ruff: noqa: E501  (evidence tables quote filings verbatim; quotes are not wrapped)
"""X5 (D-254): corporate actions, total returns and exit accounting on the X4.6.1 V2 data.

Inputs: universe V2, member data V2 and validation V2 (X4.6.1), each pinned by SHA-256, and the
write-once evidence stores (X4.1 notices, X4.6.1 filings, X5 filings and prices). Nothing is
fetched here, nothing is written to a database, and no earlier artifact changes.

Outputs (written once):

* ``x5_corporate_actions_v1.json.gz``: per slot, a normalised ledger of every corporate action
  (cash dividends, splits, spin-offs, exits and their consideration, delistings), each with its
  source; the terminal valuation of every exit; the spin-off review.
* ``x5_total_returns_v1.json.gz``: per usable slot, daily total-return factors inside its usable
  range, the terminal step where an exit is valued, and the independent verification examples.

Every value is either a provider record, a filing's stated term (quoted and checked against the
stored filing) or arithmetic on those and on recorded closing prices. An exit, spin-off or
election that the evidence does not settle stays classified as such; no last traded price is
used as an exit value.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import itertools
import json
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from app.backtesting.data import sessions_between
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import x4_evidence as x4

DECISION = "D-254"
LEDGER_PATH = lc.OUT / "x5_corporate_actions_v1.json.gz"
TR_PATH = lc.OUT / "x5_total_returns_v1.json.gz"
PINNED: dict[str, str] = {
    "config/simulation/lifecycle/x44_historical_universe_v2.json.gz": "28de49ecddc09284dbd97f3e982697a03343fa5e6a0c0e4879ba855308726080",
    "config/simulation/lifecycle/x45_member_data_v2.json.gz": "a2f7ef379a4c831bba4d6a52855dd9bbaf0cb43f17fe3e424052119ccb100159",
    "config/simulation/lifecycle/x46_validation_v2.json.gz": "93554882d370033842113b2b15501774d90225144345bff6e31e9b0b7cece7d5",
    "config/simulation/evidence/x4/manifest.json": "8b375e672b7a4b6c9a0d22eccf82472d3b14f0faf3692dd99c56143db310e5ba",
    "config/simulation/evidence/x461/manifest.json": "4eb0342395ae69cb6cf5c28ccdffcb414f259b304d09fbf322f21ac2ec1a9390",
}
X5_MANIFEST = "config/simulation/evidence/x5/manifest.json"
USABLE = ("PASS", "RESTRICTED")
TERMINAL_APPLIED = ("VALUED", "VALUED_DEFAULT_NO_ELECTION")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"

METHODOLOGY: dict[str, str] = {
    "data_scope": "Only X4.6.1 V2 slots with validation status PASS or RESTRICTED, only inside each slot's usable range, never on an excluded session (X2 sessions and spans, non-executable or unattributed sessions, sessions without a trade while listed). X1-excluded slots have no series. Membership and ranking are V2's, unchanged.",
    "price_return": "Raw close to raw close between consecutive sessions that both have a recorded trade; a session missing between them breaks the series (no compounding across a gap, nothing filled).",
    "splits": "A provider split record confirmed by the raw prices (D-251) adjusts the share count on its ex-date: gross return = ratio * close_ex / close_prev. A record the prices contradict is not applied.",
    "cash_dividends": "Entitlement fixed at the ex-date; amount = the provider's rate per share (the declared amount, known before the ex-date). Total return reinvests the dividend at the ex-date close: gross return = (close_ex + dividend) / close_prev. Record dates and pay dates are not in the evidence.",
    "pay_dates": "Not captured (the provider's dividend records carry no record or pay date). A dividend is therefore never spendable cash: portfolio accounting books it on the ex-date as a receivable at face value, counted in the position's value but never available to fund a trade, because its payment date is unknown. The same holds for the cash part of an exit's consideration. The total-return index reinvests at the ex-date close; that is a valuation convention, not a statement that cash was available.",
    "spin_offs": "A distribution in kind on its ex-date: value per parent share = sum of (documented distribution ratio * the distributed security's close on its first regular-way session on or after the ex-date). It enters the total return only when (a) the ratio is stated exactly in the filing and (b) the provider's own encoding of the event is reproducible from the distributed security's ex-date trading range (low to high) under either reading of its factor; otherwise the ex-date return is excluded (APPROXIMATE_RATIO, RATIO_UNDOCUMENTED, PROVIDER_UNRECONCILED), and the documented value stays in the ledger. The provider's fractional 'split' record is never applied as a split.",
    "exits": "On the first session that is on or after the documented effective date and after the member's last usable session, the position converts into the documented consideration: cash per share plus, per received security, ratio * that security's close on its own first session meeting the same condition. Elections are taken for a passive holder (no election made, the documented default). The total-return series takes the terminal step only for VALUED and VALUED_DEFAULT_NO_ELECTION; an approximate proration (bounds recorded), a lower bound (an unvalued right), a tender a passive holder could not use, and anything unsettled get no terminal step: the series ends at the last usable session.",
    "portfolio_cash": "Shares change only by splits, reverse splits and exits. Dividends and the cash part of an exit's consideration become receivables (counted in value, never spendable; see pay_dates). Distributed securities in a spin-off and received securities in an exit are sold at their valuation close: those proceeds are spendable (a market trade). A restricted spin-off or an exit without a value freezes the position (flagged) rather than valuing it. Cash earns nothing. No fees, taxes or withholding are modelled here.",
    "point_in_time": "Every quantity is known on the session it is used: a dividend's amount by its ex-date, a spin-off's ratio by its record date and the distributed security's close on the ex-date session, an exit's terms by its effective date and the received security's close on the valuation session. No later price, later filing or later revision enters an earlier session.",
    "double_counting": "REDACTED_FOR_GITHUB",
}


# ---------------------------------------------------------------------- evidence
def _norm(text: str) -> str:
    text = re.sub(r"<[^>]{0,3000}>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text))


class Evidence:
    """Store reads, each hash-checked against its manifest once."""

    def __init__(self, stores: dict[str, x4.Store]) -> None:
        self.stores = stores
        self.read: dict[str, str] = {}
        self.failures: list[str] = []
        self._texts: dict[str, str] = {}
        self._notices: dict[str, str] | None = None
        self._prices: dict[tuple[str, str], dict[str, tuple[float, float]]] = {}
        self._full: dict[tuple[str, str], dict[str, dict[str, float]]] = {}

    def _bytes(self, name: str, rel: str) -> bytes:
        store = self.stores[name]
        raw = store.path(rel).read_bytes()
        got = hashlib.sha256(raw).hexdigest()
        if got != store.entries[rel]["sha256"]:
            self.failures.append(f"{name}:{rel}")
        self.read[f"{name}:{rel}"] = got
        return raw

    def notices(self) -> dict[str, str]:
        if self._notices is None:
            self._notices = {}
            for rel in sorted(k for k in self.stores["x4"].entries if k.startswith("sec/form25/")):
                for line in gzip.decompress(self._bytes("x4", rel)).decode("utf-8").splitlines():
                    if line.strip():
                        r = json.loads(line)
                        acc = r["path"].rsplit("/", 1)[1].removesuffix(".txt")
                        self._notices.setdefault(acc, r.get("text") or "")
        return self._notices

    def text(self, acc: str) -> str:
        if acc not in self._texts:
            raw = ""
            for name in ("x5", "x461"):
                rel = f"sec/filings/{acc}.txt.gz"
                if self.stores[name].has(rel):
                    raw = gzip.decompress(self._bytes(name, rel)).decode("utf-8", "replace")
                    break
            else:
                raw = self.notices().get(acc, "")
            self._texts[acc] = _norm(raw)
        return self._texts[acc]

    def quote_ok(self, acc: str, quote: str) -> bool:
        return re.sub(r"\s+", " ", quote) in self.text(acc)

    def bars(self, ticker: str, asof: str) -> dict[str, dict[str, float]]:
        """Full daily bars (o, h, l, c, v) of a captured price window."""
        self.prices(ticker, asof)
        return self._full[(ticker, asof)]

    def prices(self, ticker: str, asof: str) -> dict[str, tuple[float, float]]:
        key = (ticker, asof)
        if key not in self._prices:
            rel = f"provider/prices/{ticker}_{asof}.json.gz"
            doc = json.loads(gzip.decompress(self._bytes("x5", rel)))
            out: dict[str, tuple[float, float]] = {}
            full: dict[str, dict[str, float]] = {}
            for pg in doc["pages"]:
                for rows in ((pg.get("body") or {}).get("bars") or {}).values():
                    for r in rows:
                        out[r["t"][:10]] = (float(r["c"]), float(r["v"]))
                        full[r["t"][:10]] = {
                            k: float(r[k]) for k in ("o", "h", "l", "c", "v") if k in r
                        }
            self._prices[key] = out
            self._full[key] = full
        return self._prices[key]


def _sessions(lo: str, hi: str) -> list[str]:
    if hi < lo:
        return []
    return [
        s.day.isoformat() for s in sessions_between(date.fromisoformat(lo), date.fromisoformat(hi))
    ]


def _iso(text: str) -> str:
    m = re.match(rf"({MONTHS}) (\d{{1,2}}), (\d{{4}})", text)
    if not m:
        raise ValueError(text)
    month = MONTHS.split("|").index(m.group(1)) + 1
    return date(int(m.group(3)), month, int(m.group(2))).isoformat()


def _rows(s: dict[str, Any]) -> list[dict[str, Any]]:
    cols = s["bars"]["columns"]
    return [dict(zip(cols, r, strict=True)) for r in s["bars"]["rows"]]


# ---------------------------------------------------------------------- exit terms
# Securities received in exits: (ratio, ticker, capture as-of). Ratios restate X4.6.1's quoted
# terms (checked equal at build); the ticker and its capture are X5's (identity noted).
RECEIVED = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection

# The nine exits X4.6.1 left open, each investigated (sources quoted; checked at build).
RESOLUTIONS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection

# Completion dates the removal notice does not state (quoted from the issuers' filings).
EFFECTIVE_OVERRIDES = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def effective_date(
    slot: str, notices: list[str], filings: list[str], ev: Evidence
) -> dict[str, Any]:
    """The documented effective date of an exit, from the exchange's removal notice (its 'That on
    <date> the instruments ...' or 'became effective ... on <date>' clause), else from the issuer's
    captured filings (the first completion statement naming a date)."""
    if slot in EFFECTIVE_OVERRIDES:
        d, acc, q = EFFECTIVE_OVERRIDES[slot]
        return {
            "date": d,
            "accession": acc,
            "quote": q,
            "rule": "OVERRIDE",
            "verified": ev.quote_ok(acc, q),
        }
    date_re = rf"(?:{MONTHS}) \d{{1,2}}, \d{{4}}"
    notice_pats = [
        rf"That on {date_re} the instruments",
        rf"became effective (?:before the open )?on {date_re}",
    ]
    filing_pats = [
        rf"On {date_re}[^.]{{0,250}}?(?:completed|consummated)",
        rf"(?:completed|consummated)[^.]{{0,60}}?on {date_re}",
        rf"(?:completion|consummation)(?: of the Merger[^,]{{0,30}})?, on {date_re}",
        rf"On {date_re} \(the\W{{1,3}}Closing Date",
        rf"effective at .{{0,60}}?on {date_re}",
    ]
    for rule, accs, pats in (("NOTICE", notices, notice_pats), ("FILING", filings, filing_pats)):
        for acc in accs:
            t = ev.text(acc)[:300000]
            found = [m for m in (re.search(p, t) for p in pats) if m]
            if found:
                m = min(found, key=lambda x: x.start())
                found_date = re.search(date_re, m.group(0))
                if found_date is None:  # every pattern contains date_re
                    continue
                return {"date": _iso(found_date.group(0)), "accession": acc, "quote": m.group(0)[:300],
                        "rule": rule, "verified": True}  # fmt: skip
    return {"date": None, "rule": "NOT_FOUND", "verified": False}


def last_usable_session(s: dict[str, Any], v: dict[str, Any]) -> str:
    """The member's last session with a usable traded bar: the usable range's end, stepping back
    over excluded sessions; a slot without a usable range (excluded) uses its last trade."""
    rng = v.get("usable_range")
    if not rng:
        return str(s.get("last_traded") or s["window"][0])
    excluded = set(v.get("excluded_sessions") or [])
    traded = sorted(r["session"] for r in _rows(s) if float(r["volume"] or 0) > 0
                    and rng[0] <= r["session"] <= rng[1] and r["session"] not in excluded)  # fmt: skip
    return str(traded[-1] if traded else rng[0])


def _ratio(text: str) -> Fraction:
    out = Fraction(1)
    for part in text.split("*"):
        out *= Fraction(part)
    return out


def valuation(
    ticker: str, asof: str, not_before: str, after: str, ev: Evidence
) -> dict[str, Any] | None:
    """The received security's close on its first traded session on or after ``not_before`` and
    after ``after`` (the member's last usable session)."""
    bars = ev.prices(ticker, asof)
    for d in sorted(bars):
        if d >= not_before and d > after and bars[d][1] > 0:
            return {
                "ticker": ticker,
                "session": d,
                "close": bars[d][0],
                "capture": f"{ticker}_{asof}",
            }
    return None


def terminal_x5(slot: str, s: dict[str, Any], v: dict[str, Any], ev: Evidence) -> dict[str, Any]:
    """The exit's value per share on its valuation session(s)."""
    tv = s["terminal"]
    last_usable = last_usable_session(s, v)
    res = RESOLUTIONS.get(slot)
    rec = RECEIVED.get(slot, {})
    notices = [
        n["accession"]
        for n in ((v.get("checks") or {}).get("exit_evidence") or {}).get("notices", [])
    ]
    filings = [x["accession"] for x in tv.get("sources", [])]
    if res:
        filings = [*filings, *[a for a, _ in res["sources"]]]
    eff = effective_date(slot, notices, filings, ev)
    out: dict[str, Any] = {"x461_kind": tv["kind"], "x461_status": tv["value_status"], "effective": eff,
                           "last_usable_session": last_usable}  # fmt: skip
    if res:
        for acc, q in res["sources"]:
            if not ev.quote_ok(acc, q):
                raise ValueError(f"{slot}: quote not in {acc}: {q!r}")
        basis = res["basis"]
        cash = res.get("cash")
        stock = res.get("stock", [])
        out["resolution"] = {k: res[k] for k in ("basis", "derivation") if k in res} | {
            "sources": res["sources"]
        }
    else:
        basis = "DOCUMENTED"
        cash = tv.get("cash_per_share")
        stock = rec.get("stock", [])
        terms = {(x["ratio"]) for x in tv.get("stock_per_share", [])}
        if {x[0] for x in stock} != terms:
            raise ValueError(
                f"{slot}: received ratios {stock} differ from the X4.6.1 terms {terms}"
            )
        for acc, q in rec.get("identity", []):
            if not ev.quote_ok(acc, q):
                raise ValueError(f"{slot}: identity quote not in {acc}")
    out["basis"] = basis
    if res and res.get("conditional_value"):
        out["conditional_value"] = res["conditional_value"]
    if basis in ("UNRESOLVED", "OUT_OF_USABLE_RANGE") or not eff["date"]:
        out["value_per_share"] = None
        out["status"] = (
            basis if basis in ("UNRESOLVED", "OUT_OF_USABLE_RANGE") else "EFFECTIVE_DATE_NOT_FOUND"
        )
        return out
    comps: list[dict[str, Any]] = []
    total = Fraction(0)
    if cash:
        c = _ratio(cash)
        comps.append({"kind": "cash", "per_share": float(c), "terms": cash})
        total += c
    sessions = []
    for ratio, ticker, asof, *own_date in stock:
        # A component with its own documented date (a distribution before the merger) is valued
        # from that date; otherwise from the exit's effective date.
        start = own_date[0] if own_date else eff["date"]
        p = valuation(ticker, asof, start, last_usable, ev)
        if p is None:
            comps.append({"kind": "stock", "ticker": ticker, "ratio": ratio, "price": None})
            out["status"] = "PRICE_NOT_FOUND"
            out["value_per_share"] = None
            out["components"] = comps
            return out
        r = _ratio(ratio)
        total += r * Fraction(str(p["close"]))
        sessions.append(p["session"])
        comps.append(
            {"kind": "stock", "ticker": ticker, "ratio": ratio, "ratio_value": float(r), **p}
        )
    out["components"] = comps
    if res and res.get("bounds"):
        b = res["bounds"]
        r_, t_, a_ = b["all_stock"]
        p_ = valuation(t_, a_, eff["date"], last_usable, ev)
        out["bounds"] = {"all_cash": float(_ratio(b["all_cash"])),
                         "all_stock": round(float(_ratio(r_) * Fraction(str(p_["close"]))), 6) if p_ else None}  # fmt: skip
    out["valuation_session"] = (
        max(sessions) if sessions else _first_session_after(eff["date"], last_usable)
    )
    out["value_per_share"] = round(float(total), 6)
    out["status"] = {"DOCUMENTED": "VALUED", "DEFAULT_NO_ELECTION": "VALUED_DEFAULT_NO_ELECTION",
                     "APPROXIMATE": "VALUED_APPROXIMATE", "CONDITIONAL_TENDER": "VALUED_IF_TENDERED",
                     "PARTIAL_CVR_UNVALUED": "LOWER_BOUND_CVR_UNVALUED"}[basis]  # fmt: skip
    return out


def _first_session_after(eff: str, last: str) -> str:
    days = [
        d
        for d in _sessions(eff, (date.fromisoformat(eff) + timedelta(days=10)).isoformat())
        if d > last
    ]
    return days[0] if days else eff


# ---------------------------------------------------------------------- spin-offs
SPINOFFS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def spin_off_review(slots: list[dict[str, Any]], ev: Evidence) -> list[dict[str, Any]]:
    """Each suspected spin-off: documented terms, the distributed securities' valuation, and the
    consistency of the provider's fractional record with them."""
    out = []
    for sp in SPINOFFS:
        for acc, q in sp["sources"]:
            if not ev.quote_ok(acc, q):
                raise ValueError(f"{sp['parent']} {sp['ex_date']}: quote not in {acc}: {q!r}")
        holders = [
            s
            for s in slots
            if s["ticker"] == sp["parent"] and s["window"][0] <= sp["ex_date"] <= s["end"]
        ]
        rec = None
        for s in holders:
            for r in s["splits"].get("records", []):
                if r["ex_date"] == sp["ex_date"]:
                    rec = r
        item: dict[str, Any] = {
            "parent": sp["parent"], "ex_date": sp["ex_date"], "basis": sp["basis"],
            "share_factor": sp["share_factor"], "slots": [s["slot"] for s in holders],
            "sources": sp["sources"],
            "provider_record": {k: rec[k] for k in ("new_rate", "old_rate", "record_ratio", "verdict")} if rec else None,
        }  # fmt: skip
        comps, value = [], Fraction(0)
        for ratio, ticker, asof in sp["components"]:
            p = valuation(ticker, asof, sp["ex_date"], "0000-00-00", ev)
            if p is None:
                comps.append({"ticker": ticker, "ratio": ratio, "price": None})
                value = Fraction(-1)
                break
            value += _ratio(ratio) * Fraction(str(p["close"]))
            comps.append({"ticker": ticker, "ratio": ratio, **p})
        item["components"] = comps
        item["distribution_value_per_share"] = (
            round(float(value), 6) if comps and value >= 0 else None
        )
        if holders and item["distribution_value_per_share"] is not None:
            rows = {r["session"]: r for r in _rows(holders[0])}
            days = sorted(rows)
            if sp["ex_date"] in rows:
                i = days.index(sp["ex_date"])
                prev, ex = float(rows[days[i - 1]]["close"]), float(rows[sp["ex_date"]]["close"])
                sf = float(_ratio(sp["share_factor"]))
                implied = (ex * sf + item["distribution_value_per_share"]) / (ex * sf)
                item["consistency"] = {
                    "parent_prev_close": prev, "parent_ex_close": ex,
                    "documented_factor": round(implied, 6),
                    # The provider's record folds any share-count change into one ratio.
                    "provider_factor": round(float(rec["record_ratio"]) / sf, 6) if rec else None,
                    "relative_difference": round(implied / (float(rec["record_ratio"]) / sf) - 1, 6) if rec else None,
                }  # fmt: skip
                if rec:
                    item["consistency"].update(reconcile_provider(item, rec, prev, ex, sf, ev))
        item["tr_treatment"] = tr_treatment(item)
        out.append(item)
    return out


def reconcile_provider(item: dict[str, Any], rec: dict[str, Any], prev: float, ex: float,
                       sf: float, ev: Evidence) -> dict[str, Any]:  # fmt: skip
    """The distribution value the provider's factor implies, under both readings of the factor
    (F = (ex + D) / ex, and F = prev / (prev - D)), against the distributed securities' traded
    range on the ex-date session (sum of ratio * low .. ratio * high)."""
    f = float(rec["record_ratio"]) / sf
    implied_ex = (f - 1) * ex * sf
    implied_prev = prev * (1 - 1 / f) if sf == 1 else None
    lo = hi = 0.0
    for c in item["components"]:
        bar = ev.bars(c["ticker"], c["capture"].split("_", 1)[1])[c["session"]]
        r = float(_ratio(c["ratio"]))
        lo += r * bar["l"]
        hi += r * bar["h"]
    inside = [
        x for x in (implied_ex, implied_prev) if x is not None and lo - 1e-9 <= x <= hi + 1e-9
    ]
    return {"provider_implied_value_ex_reading": round(implied_ex, 6),
            "provider_implied_value_prev_reading": round(implied_prev, 6) if implied_prev is not None else None,
            "distributed_range_on_ex_date": [round(lo, 6), round(hi, 6)],
            "provider_reconciled": bool(inside)}  # fmt: skip


def tr_treatment(item: dict[str, Any]) -> str:
    if item["basis"] == "RATIO_UNDOCUMENTED" or item["distribution_value_per_share"] is None:
        return "RESTRICTED_RATIO_UNDOCUMENTED"
    if item["basis"] == "APPROXIMATE":
        return "RESTRICTED_APPROXIMATE_RATIO"
    if not (item.get("consistency") or {}).get("provider_reconciled"):
        return "RESTRICTED_PROVIDER_UNRECONCILED"
    return "APPLIED"


# ---------------------------------------------------------------------- ledger
def slot_ledger(s: dict[str, Any], v: dict[str, Any], terminal: dict[str, Any] | None,
                spins: list[dict[str, Any]]) -> list[dict[str, Any]]:  # fmt: skip
    ev: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    for d in sorted(s["dividends"], key=lambda x: (x["ex_date"], x.get("id") or "")):
        k = (d["symbol"], d["ex_date"], float(d["rate"]))
        dup = k in seen
        seen.add(k)
        ev.append({"type": "CASH_DIVIDEND", "date": d["ex_date"], "ex_date": d["ex_date"],
                   "record_date": None, "pay_date": None, "rate": d["rate"], "special": d.get("special"),
                   "symbol": d["symbol"], "provider_id": d.get("id"), "source": d.get("artifact"),
                   "status": "DUPLICATE_NOT_COUNTED" if dup else "COUNTED"})  # fmt: skip
    for d in s.get("excluded_dividends", []):
        ev.append({"type": "CASH_DIVIDEND", "date": d["ex_date"], "ex_date": d["ex_date"], "rate": d["rate"],
                   "symbol": d["symbol"], "provider_id": d.get("id"), "status": "EXCLUDED_" + d["reason"]})  # fmt: skip
    spin_dates = {x["ex_date"]: x for x in spins if s["slot"] in x["slots"]}
    for r in s["splits"].get("records", []):
        if r["ex_date"] in spin_dates:
            sp = spin_dates[r["ex_date"]]
            ev.append({"type": "SPIN_OFF", "date": r["ex_date"], "basis": sp["basis"],
                       "components": sp["components"], "share_factor": sp["share_factor"],
                       "distribution_value_per_share": sp["distribution_value_per_share"],
                       "tr_treatment": sp["tr_treatment"],
                       "provider_fractional_record": "NOT_APPLIED_AS_SPLIT", "sources": sp["sources"]})  # fmt: skip
        else:
            kind = "STOCK_SPLIT" if float(r["record_ratio"]) > 1 else "REVERSE_SPLIT"
            ev.append({"type": kind if r["verdict"] == "VERIFIED_BY_RAW_PRICES" else "SPLIT_RECORD_REJECTED",
                       "date": r["ex_date"], "ratio": r["record_ratio"], "verdict": r["verdict"],
                       "source": r.get("artifact")})  # fmt: skip
    if terminal:
        kind = {"CASH": "MERGER_CASH", "CASH_AND_STOCK": "MERGER_CASH_AND_STOCK", "STOCK": "MERGER_STOCK",
                "CONTINUATION_1_TO_1": "SUCCESSOR_CONTINUATION", "CASH_STOCK_AND_CVR": "MERGER_CASH_STOCK_CVR",
                "ELECTION_OR_TENDER": "MERGER_ELECTION_OR_TENDER", "UNKNOWN": "LIQUIDATION_OR_UNKNOWN",
                "SUCCESSOR_BOUNDARY_UNDATED": "SUCCESSOR_REORGANISATION"}[terminal["x461_kind"]]  # fmt: skip
        ev.append({"type": kind, "date": terminal["effective"].get("date"), **{k: terminal[k] for k in
                   ("basis", "status", "value_per_share", "effective", "bounds", "conditional_value") if k in terminal},
                   "components": terminal.get("components", []), "valuation_session": terminal.get("valuation_session")})  # fmt: skip
        notices = ((v.get("checks") or {}).get("exit_evidence") or {}).get("notices", [])
        for n in notices:
            ev.append(
                {
                    "type": "DELISTING",
                    "date": n["filed"],
                    "form": n["form"],
                    "accession": n["accession"],
                }
            )
    lo, hi = v.get("usable_range") or [None, None]
    for e in ev:
        e["in_usable_range"] = bool(lo and e.get("date") and lo <= e["date"] <= hi)
    return sorted(ev, key=lambda e: (e.get("date") or "", e["type"]))


# ---------------------------------------------------------------------- total returns
def total_return_series(s: dict[str, Any], v: dict[str, Any], ledger: list[dict[str, Any]],
                        terminal: dict[str, Any] | None) -> dict[str, Any]:  # fmt: skip
    """Daily gross total-return factors inside the usable range; terminal step where valued."""
    lo, hi = v["usable_range"]
    excluded = set(v.get("excluded_sessions") or [])
    # A date X4.5 called an unusable *price* return is usable for total return when a counted
    # dividend or a documented distribution explains it; an X2 session never is.
    x2: set[str] = set()
    unusable: set[str] = set()
    for r in v.get("restrictions", []):
        if r["code"] == "X2_EXCLUSION":
            for e in r["events"]:
                if e.get("blocked_from"):
                    x2 |= {d for d in _sessions(e["blocked_from"], e["blocked_to"])}
        elif r["code"] == "UNUSABLE_RETURN_DATE":
            unusable |= set(r["dates"])
    rows = {r["session"]: r for r in _rows(s) if float(r["volume"] or 0) > 0}
    cal = _sessions(lo, hi)
    divs: dict[str, float] = defaultdict(float)
    splits: dict[str, float] = {}
    spins: dict[str, dict[str, Any]] = {}
    for e in ledger:
        if e["type"] == "CASH_DIVIDEND" and e["status"] == "COUNTED":
            divs[e["date"]] += float(e["rate"])
        elif e["type"] in ("STOCK_SPLIT", "REVERSE_SPLIT"):
            splits[e["date"]] = float(e["ratio"])
        elif e["type"] == "SPIN_OFF":
            spins[e["date"]] = e
    explained = {d for d in unusable if d not in x2 and (d in divs or (d in spins and
                 spins[d]["tr_treatment"] == "APPLIED"))}  # fmt: skip
    excluded -= explained
    out: list[list[Any]] = []
    breaks: list[list[str]] = []
    flags: Counter[str] = Counter()
    if explained:
        flags["UNUSABLE_PRICE_RETURN_EXPLAINED"] = len(explained)
    for a, b in itertools.pairwise(cal):
        if a not in rows or b not in rows or a in excluded or b in excluded:
            breaks.append([b, "MISSING_OR_EXCLUDED"])
            continue
        pa, pb = float(rows[a]["close"]), float(rows[b]["close"])
        if b in spins:
            sp = spins[b]
            if sp["tr_treatment"] != "APPLIED":
                breaks.append([b, sp["tr_treatment"]])
                continue
            sf = float(_ratio(sp["share_factor"]))
            g = (pb * sf + sp["distribution_value_per_share"]) / pa
            flags["SPIN_OFF_" + sp["basis"]] += 1
        else:
            g = (splits.get(b, 1.0) * (pb + divs.get(b, 0.0))) / pa
            if b in divs:
                flags["DIVIDEND"] += 1
            if b in splits:
                flags["SPLIT"] += 1
        out.append([b, round(g, 10)])
    term = None
    last = last_usable_session(s, v)
    if terminal and terminal.get("status") in TERMINAL_APPLIED and last in rows:
        g = terminal["value_per_share"] / float(rows[last]["close"])
        term = {"from": last, "session": terminal.get("valuation_session"), "gross": round(g, 10),
                "status": terminal["status"]}  # fmt: skip
    cum = 1.0
    for _, g in out:
        cum *= g
    return {"returns": out, "breaks": breaks, "flags": dict(sorted(flags.items())),
            "terminal_step": term, "cumulative_in_range": round(cum, 10),
            "cumulative_with_terminal": round(cum * term["gross"], 10) if term else None}  # fmt: skip


def security_return(
    chain: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]], start: str, end: str
) -> dict[str, Any]:
    """A security's total return from the close of ``start`` to the close of ``end`` across its
    consecutive slots (same Ledger security): the per-slot method, plus the step joining two
    windows (close of one window's last session to the next window's first session)."""
    rows: dict[str, dict[str, Any]] = {}
    divs: dict[str, float] = defaultdict(float)
    splits: dict[str, float] = {}
    for s, _v, led in chain:
        for r in _rows(s):
            if float(r["volume"] or 0) > 0:
                rows[r["session"]] = r
        for e in led:
            if e["type"] == "CASH_DIVIDEND" and e["status"] == "COUNTED":
                divs[e["date"]] += float(e["rate"])
            elif e["type"] in ("STOCK_SPLIT", "REVERSE_SPLIT"):
                splits[e["date"]] = float(e["ratio"])
    cal = [d for d in _sessions(start, end)]
    g, missing = 1.0, []
    for a, b in itertools.pairwise(cal):
        if a not in rows or b not in rows:
            missing.append(b)
            continue
        g *= (
            splits.get(b, 1.0)
            * (float(rows[b]["close"]) + divs.get(b, 0.0))
            / float(rows[a]["close"])
        )
    return {
        "start": start,
        "end": end,
        "total_return": round(g - 1, 6),
        "missing_sessions": missing,
    }


# ---------------------------------------------------------------------- verification
VERIFY_MERGER_VALUES = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
VERIFY_PRICES = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
VERIFY_TOTAL_RETURNS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
YEAR_END = {"2017": "2017-12-29", "2018": "2018-12-31", "2019": "2019-12-31"}


def verification(slots: list[dict[str, Any]], vals: dict[str, dict[str, Any]],
                 ledgers: dict[str, list[dict[str, Any]]], ev: Evidence) -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {"merger_values": [], "quoted_prices": [], "total_returns": []}
    for m in VERIFY_MERGER_VALUES:
        ok = ev.quote_ok(*m["source"])
        close = ev.prices(m["ticker"], m["asof"])[m["price_session"]][0]
        value = float(_ratio(m["cash"]) + _ratio(m["ratio"]) * Fraction(str(close)))
        out["merger_values"].append({**{k: m[k] for k in ("slot", "cash", "ratio", "ticker", "price_session", "stated")},
                                     "close": close, "computed": round(value, 4), "source": m["source"], "quote_verified": ok,
                                     "agrees": ok and abs(value - m["stated"]) <= 0.005 + 1e-9})  # fmt: skip
    for p in VERIFY_PRICES:
        ok = ev.quote_ok(*p["source"])
        close = ev.prices(p["ticker"], p["asof"])[p["session"]][0]
        out["quoted_prices"].append({**{k: p[k] for k in ("ticker", "session", "stated")}, "recorded_close": close,
                                     "quote_verified": ok, "agrees": ok and abs(close - p["stated"]) < 1e-9})  # fmt: skip
    by_ticker: dict[str, list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]]] = (
        defaultdict(list)
    )
    for s in slots:
        if s["slot"] in vals and s["pool"] == "STOCK":
            by_ticker[s["ticker"]].append((s, vals[s["slot"]], ledgers.get(s["slot"], [])))
    for t in VERIFY_TOTAL_RETURNS:
        ok = ev.quote_ok(*t["source"])
        chain = sorted(by_ticker.get(t["ticker"], []), key=lambda x: x[0]["formation"])
        years = []
        for y0, y1 in (("2017", "2018"), ("2018", "2019")):
            r = security_return(chain, YEAR_END[y0], YEAR_END[y1])
            stated = t["years"][y1] / t["years"][y0] - 1
            band = (
                t["precision"] / t["years"][y0] + t["precision"] / t["years"][y1]
            )  # table rounding
            years.append({"year": y1, "computed": r["total_return"], "stated": round(stated, 6),
                          "difference": round(r["total_return"] - stated, 6), "rounding_band": round(band, 6),
                          "missing_sessions": r["missing_sessions"],
                          "agrees_within_rounding_plus_0_1pct": abs(r["total_return"] - stated) <= band + 0.001})  # fmt: skip
        out["total_returns"].append({"ticker": t["ticker"], "source": t["source"], "quote_verified": ok,
                                     "slots": [c[0]["slot"] for c in chain], "years": years})  # fmt: skip
    return out


# ---------------------------------------------------------------------- build
def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path: str) -> dict[str, Any]:
    if _sha(path) != PINNED[path]:
        raise ValueError(f"{path}: not the pinned X4.6.1 artifact")
    doc: dict[str, Any] = x4.read_json(Path(path))
    return doc


def build(stores: dict[str, x4.Store]) -> tuple[dict[str, Any], dict[str, Any]]:
    pins = {p: {"expected": h, "found": _sha(p)} for p, h in PINNED.items()}
    if any(v["expected"] != v["found"] for v in pins.values()):
        raise ValueError(f"pinned inputs differ: {pins}")
    data = load("config/simulation/lifecycle/x45_member_data_v2.json.gz")
    val = load("config/simulation/lifecycle/x46_validation_v2.json.gz")
    ev = Evidence(stores)
    V = {r["slot"]: r for r in val["slots"]}
    slots = data["slots"]
    spins = spin_off_review(slots, ev)
    terminals: dict[str, dict[str, Any]] = {}
    for s in slots:
        if s.get("terminal"):
            terminals[s["slot"]] = terminal_x5(s["slot"], s, V[s["slot"]], ev)
    ledgers = {
        s["slot"]: slot_ledger(s, V[s["slot"]], terminals.get(s["slot"]), spins) for s in slots
    }
    usable = {
        s["slot"]: V[s["slot"]]
        for s in slots
        if V[s["slot"]]["status"] in USABLE and V[s["slot"]].get("usable_range")
    }
    series = {slot: total_return_series(next(x for x in slots if x["slot"] == slot), v, ledgers[slot], terminals.get(slot))
              for slot, v in sorted(usable.items())}  # fmt: skip
    ver = verification(slots, usable, ledgers, ev)
    ver["accounting_examples"] = accounting_examples(slots, ledgers, series)
    x5_manifest = _sha(X5_MANIFEST)
    inputs = {"pinned": PINNED, "x5_manifest_sha256": x5_manifest,
              "data_v2_content_sha256": data["content_sha256"], "validation_v2_content_sha256": val["content_sha256"]}  # fmt: skip
    ledger_doc: dict[str, Any] = {
        "kind": "x5_corporate_actions", "decision": DECISION, "code": "X5_CORPORATE_ACTIONS_V1",
        "methodology": METHODOLOGY, "inputs": inputs,
        "slots": {slot: ledgers[slot] for slot in sorted(ledgers)},
        "terminal_values": dict(sorted(terminals.items())),
        "spin_offs": spins,
        "summary": ledger_summary(ledgers, terminals, spins),
    }  # fmt: skip
    ledger_doc["evidence_read"] = dict(sorted(ev.read.items()))
    ledger_doc["evidence_hash_failures"] = sorted(set(ev.failures))
    ledger_doc["content_sha256"] = content_sha256(ledger_doc)
    tr_doc: dict[str, Any] = {
        "kind": "x5_total_returns", "decision": DECISION, "code": "X5_TOTAL_RETURNS_V1",
        "methodology": METHODOLOGY, "inputs": {**inputs, "ledger_content_sha256": ledger_doc["content_sha256"]},
        "series": series, "verification": ver, "summary": tr_summary(series, val),
    }  # fmt: skip
    tr_doc["content_sha256"] = content_sha256(tr_doc)
    return ledger_doc, tr_doc


def accounting_examples(*_args: object, **_kwargs: object) -> object:
    raise NotImplementedError("REDACTED_FOR_GITHUB")


def position_accounting(
    ledger: list[dict[str, Any]], shares: float, start: str, end: str
) -> dict[str, Any]:
    """The portfolio cash rules (METHODOLOGY['portfolio_cash']) applied to a position held from
    the close of ``start`` through ``end``. ``spendable`` holds only proceeds of market sales;
    dividends and exit cash are ``receivables`` (value, never spendable: pay dates unknown)."""
    spendable = 0.0
    receivables = 0.0
    held = float(shares)
    frozen = None
    steps: list[dict[str, Any]] = []
    for e in sorted(ledger, key=lambda x: (x.get("date") or "", x["type"])):
        d = e.get("date")
        if not d or not (start < d <= end) or held == 0 or frozen:
            continue
        if e["type"] == "CASH_DIVIDEND" and e.get("status") == "COUNTED":
            amount = held * float(e["rate"])
            receivables += amount
            steps.append({"date": d, "event": "dividend_receivable", "amount": round(amount, 6)})
        elif e["type"] in ("STOCK_SPLIT", "REVERSE_SPLIT"):
            held *= float(e["ratio"])
            steps.append({"date": d, "event": "split", "shares": held})
        elif e["type"] == "SPIN_OFF":
            if e.get("tr_treatment") != "APPLIED":
                frozen = {"date": d, "reason": e.get("tr_treatment")}
                steps.append(
                    {"date": d, "event": "spin_off_restricted", "reason": e.get("tr_treatment")}
                )
                continue
            proceeds = held * float(e["distribution_value_per_share"])
            spendable += proceeds
            held *= float(_ratio(e["share_factor"]))
            steps.append(
                {
                    "date": d,
                    "event": "spin_off_sold",
                    "proceeds": round(proceeds, 6),
                    "shares": held,
                }
            )
        elif (
            "value_per_share" in e and e.get("valuation_session") and e["valuation_session"] <= end
        ):
            if e.get("status") not in TERMINAL_APPLIED:
                frozen = {"date": d, "reason": e.get("status")}
                steps.append({"date": d, "event": "exit_unvalued", "status": e.get("status")})
                continue
            cash_part = sum(c["per_share"] for c in e["components"] if c["kind"] == "cash")
            stock_part = sum(
                c["ratio_value"] * c["close"] for c in e["components"] if c["kind"] == "stock"
            )
            receivables += held * cash_part
            spendable += held * stock_part
            steps.append({"date": e["valuation_session"], "event": "exit",
                          "receivable": round(held * cash_part, 6), "proceeds": round(held * stock_part, 6)})  # fmt: skip
            held = 0.0
    return {"shares": held, "spendable": round(spendable, 6), "receivables": round(receivables, 6),
            "value_excluding_shares": round(spendable + receivables, 6), "frozen": frozen, "steps": steps}  # fmt: skip


def ledger_summary(ledgers: dict[str, list[dict[str, Any]]], terminals: dict[str, dict[str, Any]],
                   spins: list[dict[str, Any]]) -> dict[str, Any]:  # fmt: skip
    types: Counter[str] = Counter()
    status: Counter[str] = Counter()
    for evs in ledgers.values():
        for e in evs:
            types[e["type"]] += 1
            if e["type"] == "CASH_DIVIDEND":
                status[e["status"]] += 1
    return {
        "events_by_type": dict(sorted(types.items())),
        "dividends_by_status": dict(sorted(status.items())),
        "terminal_by_status": dict(
            sorted(Counter(t["status"] for t in terminals.values()).items())
        ),
        "terminal_unvalued": sorted(
            k for k, t in terminals.items() if t.get("value_per_share") is None
        ),
        "spin_offs_by_basis": dict(sorted(Counter(s["basis"] for s in spins).items())),
        "spin_offs_by_tr_treatment": dict(
            sorted(Counter(s["tr_treatment"] for s in spins).items())
        ),
    }


def tr_summary(series: dict[str, dict[str, Any]], val: dict[str, Any]) -> dict[str, Any]:
    n = sum(len(x["returns"]) for x in series.values())
    flags: Counter[str] = Counter()
    for x in series.values():
        for k, c in x["flags"].items():
            flags[k] += c
    return {
        "slots_with_series": len(series),
        "daily_returns": n,
        "breaks": sum(len(x["breaks"]) for x in series.values()),
        "flags": dict(sorted(flags.items())),
        "terminal_steps": dict(
            sorted(
                Counter(
                    x["terminal_step"]["status"] for x in series.values() if x["terminal_step"]
                ).items()
            )
        ),
        "slots_without_series": sorted(
            r["slot"] for r in val["slots"] if r["status"] not in USABLE
        ),
    }


def content_sha256(doc: dict[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "content_sha256"}
    return hashlib.sha256(x4.canonical(rs._plain(body))).hexdigest()


def write(doc: dict[str, Any], path: Path) -> str:
    if path.exists():
        raise FileExistsError(f"{path} exists: X5 artifacts are written once")
    data = x4.gz(x4.canonical(rs._plain(doc)))
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def stores() -> dict[str, x4.Store]:
    from app.research import x5_capture, x461_corrections

    return {"x4": x4.Store(), "x461": x461_corrections.Store(), "x5": x5_capture.Store()}


def verify() -> dict[str, Any]:
    ledger, tr = build(stores())
    stored_l, stored_t = x4.read_json(LEDGER_PATH), x4.read_json(TR_PATH)
    return {
        "ledger": [stored_l["content_sha256"], content_sha256(stored_l), ledger["content_sha256"]],
        "total_returns": [
            stored_t["content_sha256"],
            content_sha256(stored_t),
            tr["content_sha256"],
        ],
        "files": {
            LEDGER_PATH.as_posix(): _sha(LEDGER_PATH.as_posix()),
            TR_PATH.as_posix(): _sha(TR_PATH.as_posix()),
        },
        "evidence_hash_failures": ledger["evidence_hash_failures"],
        "ok": len({stored_l["content_sha256"], content_sha256(stored_l), ledger["content_sha256"]})
        == 1
        and len({stored_t["content_sha256"], content_sha256(stored_t), tr["content_sha256"]}) == 1
        and not ledger["evidence_hash_failures"],
    }
