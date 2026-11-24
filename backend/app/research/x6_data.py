"""X6 (D-255): the pinned historical inputs of execution and portfolio accounting.

X6 consumes exactly the final X5 artifacts and the X4.6.1 V2 artifacts they were built from.
Every file is checked against its pinned SHA-256 before use, and both X5 documents' content
hashes are recomputed. Nothing is fetched, nothing is written to a database, and no earlier
artifact changes.

What this module provides (all point-in-time, all from the artifacts):

* :class:`SlotData`: one member slot's traded bars (raw, Decimal), its X4.6.1 status, usable
  range and excluded sessions, its X5 corporate-action events, terminal value and series
  breaks, and its formation evidence (the X4.4 V2 record's formation close and median dollar
  volume).
* :class:`Market`: every slot, the security-level history used for liquidity estimates, and
  the next formation's slot of the same security (the only way a position can continue
  across a window boundary).
* :func:`formation_step`: the explicit return step from the formation close to the first
  session of a slot's window, which no X5 series contains (X5 limitation 5).

Validity rules are X5's, replicated and checked against X5's own series at build time
(:func:`check_against_x5`): a session is executable only inside the usable range, with a
recorded trade, and not excluded (an X4.5 unusable-return date counts only when a counted
dividend or an applied distribution explains it; an X2 session never does).
"""

from __future__ import annotations

import hashlib
import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.data_engine.calendar import next_trading_day
from app.research import x4_evidence as x4
from app.research import x5_accounting as acc

DECISION = "D-255"
LIFECYCLE = "config/simulation/lifecycle"
UNIVERSE = f"{LIFECYCLE}/x44_historical_universe_v2.json.gz"
MEMBER_DATA = f"{LIFECYCLE}/x45_member_data_v2.json.gz"
VALIDATION = f"{LIFECYCLE}/x46_validation_v2.json.gz"
X5_LEDGER = f"{LIFECYCLE}/x5_corporate_actions_v1.json.gz"
X5_TOTAL_RETURNS = f"{LIFECYCLE}/x5_total_returns_v1.json.gz"
# File SHA-256 of every input. The X4.6.1 V2 pins are X5's own; the X5 pins are the final X5
# artifacts (D-254 report section 6).
PINNED: dict[str, str] = {
    UNIVERSE: acc.PINNED[UNIVERSE],
    MEMBER_DATA: acc.PINNED[MEMBER_DATA],
    VALIDATION: acc.PINNED[VALIDATION],
    X5_LEDGER: "c320e96dc57f726231d7120075ddc743f0f852d7c45976639b917af0a2b58d94",
    X5_TOTAL_RETURNS: "eef8fb05d2d64d4d5e637e44cafb2361616d79fc5dcbaca94c848d675d310642",
}
X5_CONTENT: dict[str, str] = {
    X5_LEDGER: "c303c90ed7d113bbd4543d40120c0a444ecacf0e3fc1d808af7741a8a96d7ebc",
    X5_TOTAL_RETURNS: "e6f1808063a70e2c000b429e83cd65a308a63505ec569105849b53c4536b65cf",
}
USABLE = acc.USABLE
TERMINAL_APPLIED = acc.TERMINAL_APPLIED
D0 = Decimal(0)


def dec(value: Any) -> Decimal:
    """The exact decimal of a stored number (its shortest repr), never a binary expansion."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PinError(ValueError):
    """An input is not the pinned artifact: X6 refuses to run on it."""


def verify_pins(paths: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """File SHA-256 of every pinned input; raises :class:`PinError` on any difference."""
    found = {p: {"expected": h, "found": file_sha256(p)} for p, h in (paths or PINNED).items()}
    bad = {p: v for p, v in found.items() if v["expected"] != v["found"]}
    if bad:
        raise PinError(f"inputs differ from their pins: {bad}")
    return found


def load_documents() -> dict[str, dict[str, Any]]:
    """The five pinned documents, with the X5 content hashes recomputed and cross-linked."""
    verify_pins()
    docs: dict[str, dict[str, Any]] = {p: x4.read_json(Path(p)) for p in PINNED}
    for path, expected in X5_CONTENT.items():
        doc = docs[path]
        if not (doc["content_sha256"] == acc.content_sha256(doc) == expected):
            raise PinError(f"{path}: content hash does not match the final X5 artifact")
    tr_inputs = docs[X5_TOTAL_RETURNS]["inputs"]
    if tr_inputs["ledger_content_sha256"] != docs[X5_LEDGER]["content_sha256"]:
        raise PinError("X5 total returns were not built from the pinned X5 ledger")
    for path in (X5_LEDGER, X5_TOTAL_RETURNS):
        inputs = docs[path]["inputs"]
        if inputs["data_v2_content_sha256"] != docs[MEMBER_DATA]["content_sha256"]:
            raise PinError(f"{path}: built from other member data")
        if inputs["validation_v2_content_sha256"] != docs[VALIDATION]["content_sha256"]:
            raise PinError(f"{path}: built from another validation")
    return docs


# ---------------------------------------------------------------------- slot data
@dataclass(frozen=True)
class Bar:
    """One traded daily bar, raw (unadjusted), as recorded in member data V2."""

    session: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None


@dataclass
class SlotData:
    slot: str
    ticker: str
    ledger_key: str
    pool: str  # STOCK or FUND
    formation: str
    window: tuple[str, str]
    status: str  # X4.6.1 V2 validation status
    usable_range: tuple[str, str] | None
    bars: dict[str, Bar]  # traded sessions only (volume > 0)
    not_executable: frozenset[str]  # excluded sessions after X5's explanation rule, X2 included
    breaks: dict[str, str]  # X5 series breaks: session -> reason
    events: list[dict[str, Any]]  # the X5 ledger events
    terminal: dict[str, Any] | None  # the X5 terminal value
    has_series: bool  # X5 produced a total-return series (usable slot)
    formation_close: (
        Decimal | None
    )  # X4.4 V2 record: last close, when its last bar is the formation
    formation_median_dollar_volume: Decimal | None
    last_usable: str | None = None
    by_date: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def asset_class(self) -> str:
        return "ETF" if self.pool == "FUND" else "EQUITY"

    @property
    def security(self) -> tuple[str, str]:
        """Same ledger security *and* same ticker: a ledger key alone can join share classes
        (BRK.A / BRK.B, RDS.A / RDS.B), whose prices differ."""
        return (self.ledger_key, self.ticker)

    def in_usable_range(self, session: str) -> bool:
        return bool(self.usable_range and self.usable_range[0] <= session <= self.usable_range[1])

    def executable(self, session: str) -> bool:
        """A trade at this session is possible on recorded, usable data."""
        return (
            self.has_series
            and self.in_usable_range(session)
            and session in self.bars
            and session not in self.not_executable
        )

    def member_effective(self, session: str) -> bool:
        """Membership is effective from the session after the formation to the window's end."""
        return self.window[0] <= session <= self.window[1]


def _sessions(lo: str, hi: str) -> list[str]:
    return acc._sessions(lo, hi)


def _not_executable(v: dict[str, Any], events: list[dict[str, Any]]) -> frozenset[str]:
    """X5's rule (``total_return_series``): excluded sessions, less the X4.5 unusable-return
    dates a counted dividend or an applied distribution explains; X2 sessions always."""
    x2: set[str] = set()
    unusable: set[str] = set()
    for r in v.get("restrictions", []):
        if r["code"] == "X2_EXCLUSION":
            for e in r["events"]:
                if e.get("blocked_from"):
                    x2 |= set(_sessions(e["blocked_from"], e["blocked_to"]))
        elif r["code"] == "UNUSABLE_RETURN_DATE":
            unusable |= set(r["dates"])
    divs = {e["date"] for e in events if e["type"] == "CASH_DIVIDEND" and e["status"] == "COUNTED"}
    applied = {
        e["date"] for e in events if e["type"] == "SPIN_OFF" and e["tr_treatment"] == "APPLIED"
    }
    explained = {d for d in unusable if d not in x2 and (d in divs or d in applied)}
    return frozenset((set(v.get("excluded_sessions") or []) - explained) | x2)


def _formation_records(universe: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in universe["formations"].values():
        for r in f["records"]:
            if r["decision"].startswith("MEMBER"):
                out[f"{f['pool']}|{f['formation']}|{r['ticker']}"] = r
    return out


def slot_data(
    s: dict[str, Any],
    v: dict[str, Any],
    events: list[dict[str, Any]],
    terminal: dict[str, Any] | None,
    series: dict[str, Any] | None,
    record: dict[str, Any] | None,
) -> SlotData:
    cols = s["bars"]["columns"]
    bars: dict[str, Bar] = {}
    for row in s["bars"]["rows"]:
        r = dict(zip(cols, row, strict=True))
        if float(r["volume"] or 0) > 0:
            bars[r["session"]] = Bar(
                r["session"],
                dec(r["open"]),
                dec(r["high"]),
                dec(r["low"]),
                dec(r["close"]),
                dec(r["volume"]),
                None if r.get("vwap") is None else dec(r["vwap"]),
            )
    rng = v.get("usable_range")
    has_series = series is not None
    sd = SlotData(
        slot=s["slot"],
        ticker=s["ticker"],
        ledger_key=s["ledger_key"],
        pool=s["pool"],
        formation=s["formation"],
        window=(s["window"][0], s["window"][1]),
        status=v["status"],
        usable_range=(rng[0], rng[1]) if rng and has_series else None,
        bars=bars,
        not_executable=_not_executable(v, events),
        breaks={b: reason for b, reason in (series or {}).get("breaks", [])},
        events=events,
        terminal=terminal,
        has_series=has_series,
        formation_close=(
            dec(record["last_close"])
            if record and record.get("last_bar") == s["formation"] and record.get("last_close")
            else None
        ),
        formation_median_dollar_volume=(
            dec(record["median_dollar_volume"])
            if record and record.get("median_dollar_volume")
            else None
        ),
    )
    sd.last_usable = acc.last_usable_session(s, v) if has_series else None
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e.get("date"):
            by_date[e["date"]].append(e)
    sd.by_date = dict(by_date)
    return sd


class Market:
    """Every member slot, read from the pinned artifacts. Read-only."""

    def __init__(self, slots: dict[str, SlotData], universe_records: int = 0) -> None:
        self.slots = slots
        self.universe_records = universe_records
        self._by_security: dict[tuple[str, str], list[SlotData]] = defaultdict(list)
        for sd in sorted(slots.values(), key=lambda x: x.formation):
            self._by_security[sd.security].append(sd)

    @classmethod
    def from_documents(cls, docs: dict[str, dict[str, Any]]) -> Market:
        led, tr = docs[X5_LEDGER], docs[X5_TOTAL_RETURNS]
        vals = {v["slot"]: v for v in docs[VALIDATION]["slots"]}
        records = _formation_records(docs[UNIVERSE])
        slots = {
            s["slot"]: slot_data(
                s,
                vals[s["slot"]],
                led["slots"].get(s["slot"], []),
                led["terminal_values"].get(s["slot"]),
                tr["series"].get(s["slot"]),
                records.get(s["slot"]),
            )
            for s in docs[MEMBER_DATA]["slots"]
        }
        return cls(slots, len(records))

    def history(self, slot: str, upto: str) -> list[Bar]:
        """Executable bars of this security up to and including ``upto``: this slot's, and an
        earlier formation's slot of the same security before this slot's window."""
        sd = self.slots[slot]
        out: dict[str, Bar] = {}
        for other in self._by_security[sd.security]:
            if other.formation > sd.formation:
                continue
            for d, b in other.bars.items():
                if d > upto or not other.executable(d):
                    continue
                if other.slot != sd.slot and d >= sd.window[0]:
                    continue  # inside this window only this slot's own bars count
                out.setdefault(d, b)
        return [out[d] for d in sorted(out)]

    def successor_slot(self, slot: str) -> SlotData | None:
        """The next formation's slot of the same security (same ledger key and ticker), whose
        formation is this window's last session: the only continuation of a position."""
        sd = self.slots[slot]
        for other in self._by_security[sd.security]:
            if other.formation == sd.window[1] and other.slot != sd.slot:
                return other
        return None

    def previous_slot(self, slot: str) -> SlotData | None:
        sd = self.slots[slot]
        for other in self._by_security[sd.security]:
            if other.window[1] == sd.formation and other.slot != sd.slot:
                return other
        return None


# ---------------------------------------------------------------------- formation step
def formation_step(sd: SlotData, market: Market | None = None) -> dict[str, Any]:
    """The step from the formation close to the first session of the window (X5 limitation 5).

    gross = split ratio on the first session * (first close + counted dividends whose ex-date
    is the first session [+ an applied distribution's value]) / formation close.

    The formation close is the X4.4 V2 record's last close at the formation (the bar that
    admitted the member). Where the previous formation's slot of the same security traded on
    the formation session, its close is compared (``cross_check``). A dividend whose ex-date is
    the first session belongs to holders at the formation close only: a position bought at or
    after the first session's open is not entitled to it.
    """
    first = sd.window[0]
    out: dict[str, Any] = {
        "slot": sd.slot,
        "formation": sd.formation,
        "first_session": first,
        "formation_close": None if sd.formation_close is None else str(sd.formation_close),
        "formation_close_source": "X4.4 V2 formation record (last_close at last_bar = formation)",
    }
    if not sd.has_series:
        return {**out, "status": "SLOT_NOT_USABLE", "gross": None}
    expected_first = next_trading_day(date.fromisoformat(sd.formation)).day.isoformat()
    if first != expected_first:
        return {**out, "status": "WINDOW_NOT_ADJACENT", "gross": None}
    if market is not None:
        prev = market.previous_slot(sd.slot)
        if prev is not None and sd.formation in prev.bars and prev.executable(sd.formation):
            other = prev.bars[sd.formation].close
            out["cross_check"] = {
                "slot": prev.slot,
                "close": str(other),
                "equal": sd.formation_close == other,
            }
            if sd.formation_close is not None and other != sd.formation_close:
                return {**out, "status": "FORMATION_CLOSE_CONFLICT", "gross": None}
    if sd.formation_close is None:
        return {**out, "status": "NO_FORMATION_CLOSE", "gross": None}
    if not sd.executable(first) or (sd.usable_range and sd.usable_range[0] != first):
        return {**out, "status": "FIRST_SESSION_NOT_USABLE", "gross": None}
    events = sd.by_date.get(first, [])
    divs = [e for e in events if e["type"] == "CASH_DIVIDEND" and e["status"] == "COUNTED"]
    splits = [e for e in events if e["type"] in ("STOCK_SPLIT", "REVERSE_SPLIT")]
    spins = [e for e in events if e["type"] == "SPIN_OFF"]
    if any(e["tr_treatment"] != "APPLIED" for e in spins):
        return {**out, "status": "RESTRICTED_SPIN_OFF_ON_FIRST_SESSION", "gross": None}
    p_first = sd.bars[first].close
    dividend = sum((dec(e["rate"]) for e in divs), D0)
    ratio = dec(splits[0]["ratio"]) if splits else Decimal(1)
    distribution = sum((dec(e["distribution_value_per_share"]) for e in spins), D0)
    share_factor = Decimal(1)
    for e in spins:
        f = acc._ratio(e["share_factor"])
        share_factor *= Decimal(f.numerator) / Decimal(f.denominator)
    price = ratio * share_factor * p_first
    gross = (price + ratio * (dividend + distribution)) / sd.formation_close
    return {
        **out,
        "status": "COMPUTED",
        "first_close": str(p_first),
        "dividends_on_first_session": [
            {"rate": str(dec(e["rate"])), "provider_id": e.get("provider_id")} for e in divs
        ],
        "split_ratio": str(ratio),
        "distribution_value": str(distribution),
        "price_gross": str(round(price / sd.formation_close, 10)),
        "gross": str(round(gross, 10)),
    }


# ---------------------------------------------------------------------- checks
def check_against_x5(market: Market, series: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """X6's executable sessions reproduce X5's series exactly: every X5 return date is a pair
    of consecutive executable sessions and every X5 break is not, for every usable slot."""
    mismatches: list[dict[str, Any]] = []
    for slot, ser in sorted(series.items()):
        sd = market.slots[slot]
        if sd.usable_range is None:
            raise ValueError(f"{slot}: X5 has a series but X6 no usable range")
        cal = _sessions(*sd.usable_range)
        pairs = {b for a, b in itertools.pairwise(cal) if sd.executable(a) and sd.executable(b)}
        spin_breaks = {b for b, r in ser["breaks"] if r != "MISSING_OR_EXCLUDED"}
        x5_returns = {b for b, _ in ser["returns"]}
        if x5_returns != pairs - spin_breaks:
            mismatches.append(
                {
                    "slot": slot,
                    "x6_only": sorted(pairs - spin_breaks - x5_returns)[:5],
                    "x5_only": sorted(x5_returns - pairs)[:5],
                }
            )
    return {"slots": len(series), "mismatches": mismatches, "ok": not mismatches}


# The last session of every window's data (the universe's ``until``): not a window boundary.
DATA_END = "2019-12-31"


def share_ratios(market: Market, slot: str) -> dict[str, Decimal]:
    """Session -> share ratio applied on that ex-date, across the security's slots up to this
    one: verified splits and applied distributions' share factors (liquidity restatement)."""
    sd = market.slots[slot]
    out: dict[str, Decimal] = {}
    for other in market._by_security[sd.security]:
        if other.formation > sd.formation:
            continue
        for e in other.events:
            if e["type"] in ("STOCK_SPLIT", "REVERSE_SPLIT"):
                out[e["date"]] = dec(e["ratio"])
            elif e["type"] == "SPIN_OFF" and e["tr_treatment"] == "APPLIED":
                f = acc._ratio(e["share_factor"])
                if f != 1:
                    out[e["date"]] = Decimal(f.numerator) / Decimal(f.denominator)
    return out
