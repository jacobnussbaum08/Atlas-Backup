# ruff: noqa: E501  (assumption tables and journal lines read as one row each)
"""X6 (D-255): the historical portfolio ledger: cash, receivables, positions, P&L and equity.

A double-entry journal (spec 16) in exact decimals. Every entry balances and none is changed
after it is posted. Positions, cash and receivables are derived views, reconciled with the
journal after every session (:meth:`Portfolio.check_invariants`).

The session cycle, in order (all times XNYS, UTC in records):

1. :meth:`Portfolio.begin_session` (the open): corporate actions due at the open for positions
   held at the previous close (cash dividends, splits, distributions' share factors), the
   X5 breaks (a held position is frozen, never bridged), exits (past the usable range), then
   orders executing at the open.
2. :meth:`Portfolio.close_session` (the close): orders executing at the VWAP or the close,
   protective stops, sales valued at this close (distributed and received shares), exit
   conversions, marks, borrow fees, the window boundary, the snapshot and the checks.
3. :meth:`Portfolio.decide` (after the close): order intents are checked against every limit
   one at a time, in a fixed order, against the state *including* earlier reservations, and
   either reserved or rejected whole (the in-memory form of spec 14.3's atomic reservation).

Cash rules (X5, D-254):

* **Spendable cash** is cash less reservations and short margin. Only market sales create
  it: the sale of a position, of distributed shares or of received shares.
* **Receivables** (dividends; the cash part of an exit; cash in lieu of a fraction) are
  counted in equity and never spendable. The evidence has no pay dates.
* **Short-sale proceeds** are restricted collateral, released only by covering.
* Amounts a short position owes (a dividend; a distribution; an exit's consideration) are
  paid from cash at once: conservative, since their pay dates are unknown.

A position whose value the evidence does not settle (a restricted spin-off, a missing or
excluded session, an exit without a valued terminal step, a window end without data) is
frozen at its last valid mark. The snapshot says so (``equity_complete`` false); nothing is
valued from a later price across the gap.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from fractions import Fraction
from typing import Any

from app.research import x5_accounting as acc
from app.research import x6_execution as ex
from app.research.x6_data import (
    DATA_END,
    TERMINAL_APPLIED,
    Bar,
    Market,
    SlotData,
    dec,
    formation_step,
    share_ratios,
)
from app.research.x6_execution import BPS, ONE, ZERO, Side, X6Config

D = Decimal

ASSETS = ("CASH", "SHORT_PROCEEDS_RESTRICTED", "RECEIVABLE_DIVIDENDS", "RECEIVABLE_EXIT_CASH",
          "LONG_POSITIONS_AT_COST")  # fmt: skip
LIABILITIES = ("SHORT_POSITIONS_AT_COST",)
EQUITY_ACCOUNTS = ("CAPITAL", "REALIZED_PNL")
INCOME = ("DIVIDEND_INCOME", "INTEREST_INCOME")
EXPENSES = ("COMMISSION_EXPENSE", "FEE_EXPENSE", "REGULATORY_FEE_EXPENSE", "BORROW_EXPENSE",
            "SHORT_DIVIDEND_EXPENSE", "WITHHOLDING_EXPENSE")  # fmt: skip
ACCOUNTS = frozenset(ASSETS + LIABILITIES + EQUITY_ACCOUNTS + INCOME + EXPENSES)
RECEIVABLE_ACCOUNTS = ("RECEIVABLE_DIVIDENDS", "RECEIVABLE_EXIT_CASH")
MICRO = D("0.000001")
PRECISION = D("1E-12")  # every journal amount is exact at this scale


def exact_shares(quantity: D, ratio: Fraction) -> tuple[D, Fraction]:
    """Whole shares and the remaining fraction of ``quantity`` x ``ratio``, exactly (a share
    factor such as 1/3 is a repeating decimal: 600 x 1/3 is 200, never 199.99...)."""
    exact = Fraction(str(quantity)) * ratio
    whole = math.floor(exact) if exact >= 0 else -math.floor(-exact)
    return D(whole), exact - whole


def money_of(fraction: Fraction, price: D) -> D:
    """``fraction`` x ``price``, rounded to MICRO (cash in lieu of a fractional share)."""
    return (D(fraction.numerator) * price / D(fraction.denominator)).quantize(MICRO)


class LedgerError(ValueError):
    """An entry that does not balance, or an invariant that does not hold."""


class Journal:
    """Append-only double-entry journal; amounts are debits (+) and credits (-)."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.balances: dict[str, D] = defaultdict(D)

    def post(self, session: str, kind: str, lines: dict[str, D], **ref: Any) -> None:
        lines = {k: v for k, v in lines.items() if v != 0}
        unknown = set(lines) - ACCOUNTS
        if unknown:
            raise LedgerError(f"unknown accounts {sorted(unknown)}")
        inexact = {k: v for k, v in lines.items() if v != v.quantize(PRECISION)}
        if inexact:  # a repeating decimal would lose digits as balances accumulate
            raise LedgerError(f"{kind} on {session}: amounts finer than {PRECISION}: {inexact}")
        if sum(lines.values(), ZERO) != 0:
            raise LedgerError(f"{kind} on {session} does not balance: {lines}")
        self.entries.append({"seq": len(self.entries), "session": session, "kind": kind,
                             "lines": {k: str(v) for k, v in sorted(lines.items())}, **ref})  # fmt: skip
        for k, v in lines.items():
            self.balances[k] += v

    def balance(self, account: str) -> D:
        return self.balances.get(account, ZERO)


@dataclass
class Position:
    slot: str
    ticker: str
    quantity: D  # signed: short < 0
    cost: D  # signed: long = asset at cost; short = liability at proceeds (negative)
    mark: D
    mark_session: str
    opened: str
    state: str = "OPEN"  # OPEN, AWAITING_EXIT, FROZEN
    reason: str | None = None
    stop: D | None = None
    borrow_rate: D | None = None

    @property
    def market_value(self) -> D:
        return self.quantity * self.mark

    @property
    def unrealized(self) -> D:
        return self.market_value - self.cost

    def as_json(self) -> dict[str, Any]:
        return {"slot": self.slot, "quantity": str(self.quantity), "cost": str(self.cost),
                "mark": str(self.mark), "mark_session": self.mark_session, "state": self.state,
                "reason": self.reason, "market_value": str(self.market_value)}  # fmt: skip


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    slot: str
    side: Side
    quantity: D
    limit_price: D | None = None
    stop_price: D | None = None
    reason: str = "STRATEGY"


@dataclass
class Reservation:
    intent: OrderIntent
    quantity: D
    decided: str
    execute_on: str
    reference: D
    liquidity: ex.Liquidity
    cash: D
    margin: D
    notional: D  # worst-case notional (exposure while pending)


@dataclass
class Portfolio:
    market: Market
    config: X6Config
    capital: D
    start: str
    journal: Journal = field(default_factory=Journal)
    positions: dict[str, Position] = field(default_factory=dict)
    reservations: list[Reservation] = field(default_factory=list)
    receivables: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    closed: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    seen_intents: set[str] = field(default_factory=set)
    blocked: dict[str, str] = field(default_factory=dict)
    pending_close: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    session: str = ""
    phase: str = "CLOSED"
    peak: D = ZERO
    day_start_equity: D = ZERO
    week_start_equity: D = ZERO

    def __post_init__(self) -> None:
        if self.capital <= 0:
            raise ValueError("capital must be positive")
        self.session = self.start
        self.journal.post(self.start, "CAPITAL", {"CASH": self.capital, "CAPITAL": -self.capital})
        self.peak = self.day_start_equity = self.week_start_equity = self.capital
        self.snapshots.append(self.snapshot())

    # ------------------------------------------------------------------ views
    @property
    def cash(self) -> D:
        return self.journal.balance("CASH")

    def receivables_total(self) -> D:
        return sum((self.journal.balance(a) for a in RECEIVABLE_ACCOUNTS), ZERO)

    def margin_required(self) -> D:
        rate = self.config.hard_limits.min_short_margin_rate
        return sum(
            (-p.market_value * rate for p in self.positions.values() if p.quantity < 0), ZERO
        )

    def spendable(self) -> D:
        """Cash less reservations and short margin. Receivables and restricted short proceeds
        are not cash at all, so they are never part of it."""
        reserved = sum((r.cash + r.margin for r in self.reservations), ZERO)
        return self.cash - reserved - self.margin_required()

    def equity(self) -> D:
        return (
            self.cash
            + self.journal.balance("SHORT_PROCEEDS_RESTRICTED")
            + self.receivables_total()
            + sum((p.market_value for p in self.positions.values()), ZERO)
        )

    def _slot(self, slot: str) -> SlotData:
        return self.market.slots[slot]

    def _event(self, kind: str, **kw: Any) -> None:
        self.events.append({"session": self.session, "event": kind, **kw})

    # ------------------------------------------------------------------ the open
    def begin_session(self, session: str) -> None:
        if self.phase != "CLOSED":
            raise LedgerError("begin_session before the previous session closed")
        if session != ex.next_session(self.session):
            raise LedgerError(f"{session} is not the session after {self.session}")
        previous = self.session
        last = self.snapshots[-1]["equity"]
        self.day_start_equity = D(last)
        if (
            date.fromisoformat(session).isocalendar()[:2]
            != date.fromisoformat(previous).isocalendar()[:2]
        ):
            self.week_start_equity = D(last)
        self.session, self.phase = session, "OPEN"
        for slot in sorted(self.positions):
            self._open_events(self.positions[slot], session)
        if self.config.timing == ex.Timing.NEXT_OPEN:
            self._execute_due()

    def _open_events(self, pos: Position, s: str) -> None:
        if pos.state != "OPEN":
            return
        sd = self._slot(pos.slot)
        if sd.usable_range is None:
            raise LedgerError(f"{pos.slot}: a position in a slot without a usable range")
        term = sd.terminal
        # An exit starts after X5's last usable session (its terminal step's origin): the
        # sessions between it and the valuation session carry no usable price.
        past_last = bool(term and sd.last_usable and s > sd.last_usable)
        if past_last or s > sd.usable_range[1] or s > sd.window[1]:
            if term and term.get("status") in TERMINAL_APPLIED and term.get("valuation_session"):
                pos.state, pos.reason = "AWAITING_EXIT", term["status"]
                self.pending_close[term["valuation_session"]].append(
                    {"kind": "EXIT", "slot": pos.slot}
                )
                self._event("AWAITING_EXIT", slot=pos.slot, status=term["status"],
                            valuation_session=term["valuation_session"], mark=str(pos.mark))  # fmt: skip
            else:
                reason = term["status"] if term else "USABLE_RANGE_ENDED"
                self._freeze(pos, reason, bounds=(term or {}).get("bounds"),
                             conditional_value=(term or {}).get("conditional_value"))  # fmt: skip
            return
        if s in sd.breaks:  # a gap or a restricted distribution: never bridged
            self._freeze(pos, "BREAK_" + sd.breaks[s])
            return
        for e in sorted(
            sd.by_date.get(s, []), key=lambda x: (x["type"], x.get("provider_id") or "")
        ):
            if e["type"] == "CASH_DIVIDEND" and e["status"] == "COUNTED":
                self._dividend(pos, e)
            elif e["type"] in ("STOCK_SPLIT", "REVERSE_SPLIT"):
                self._split(pos, Fraction(str(dec(e["ratio"]))), e["type"])
            elif e["type"] == "SPIN_OFF" and e["tr_treatment"] == "APPLIED":
                self._spin_off_open(pos, e)

    def _freeze(self, pos: Position, reason: str, **kw: Any) -> None:
        pos.state, pos.reason = "FROZEN", reason
        self.reservations = [
            r for r in self.reservations if r.intent.slot != pos.slot or r.intent.side.opens
        ]
        self._event("FROZEN", slot=pos.slot, reason=reason, mark=str(pos.mark),
                    mark_session=pos.mark_session, **{k: v for k, v in kw.items() if v})  # fmt: skip

    def _dividend(self, pos: Position, e: dict[str, Any]) -> None:
        rate = dec(e["rate"])
        amount = abs(pos.quantity) * rate
        if pos.quantity > 0:
            w = self.config.withholding_rates.get(pos.ticker, ZERO) * amount
            self.journal.post(self.session, "DIVIDEND_RECEIVABLE",
                              {"RECEIVABLE_DIVIDENDS": amount - w, "WITHHOLDING_EXPENSE": w,
                               "DIVIDEND_INCOME": -amount}, slot=pos.slot, rate=str(rate))  # fmt: skip
            self.receivables.append({"kind": "DIVIDEND", "slot": pos.slot, "ex_date": self.session,
                                     "amount": str(amount - w), "pay_date": None,
                                     "provider_id": e.get("provider_id")})  # fmt: skip
            self._event(
                "DIVIDEND_RECEIVABLE", slot=pos.slot, amount=str(amount - w), withheld=str(w)
            )
        else:
            self.journal.post(self.session, "SHORT_DIVIDEND_PAID",
                              {"SHORT_DIVIDEND_EXPENSE": amount, "CASH": -amount}, slot=pos.slot)  # fmt: skip
            self._event("SHORT_DIVIDEND_PAID", slot=pos.slot, amount=str(amount))

    def _split(self, pos: Position, ratio: Fraction, kind: str) -> None:
        before = pos.quantity
        whole, fraction = exact_shares(before, ratio)
        pos.quantity = whole
        scale = D(ratio.denominator) / D(ratio.numerator)
        pos.mark = (pos.mark * scale).quantize(D("1E-10"))  # until this session's close
        if pos.stop is not None:
            pos.stop = (pos.stop * scale).quantize(D("1E-10"))
        self._event(kind, slot=pos.slot, ratio=str(ratio), before=str(before), after=str(whole))
        if fraction:
            self.pending_close[self.session].append(
                {"kind": "CASH_IN_LIEU", "slot": pos.slot, "fraction": fraction}
            )

    def _spin_off_open(self, pos: Position, e: dict[str, Any]) -> None:
        before = pos.quantity
        f = acc._ratio(e["share_factor"])
        if f != 1:
            self._split(pos, f, "SPIN_OFF_SHARE_FACTOR")
        for c in e["components"]:
            self.pending_close[c["session"]].append({"kind": "DISTRIBUTION", "slot": pos.slot,
                                                     "component": c, "parent_shares": before})  # fmt: skip
        self._event("SPIN_OFF_ENTITLEMENT", slot=pos.slot, parent_shares=str(before),
                    value_per_share=str(e["distribution_value_per_share"]))  # fmt: skip

    # ------------------------------------------------------------------ the close
    def close_session(self) -> dict[str, Any]:
        if self.phase != "OPEN":
            raise LedgerError("close_session without begin_session")
        s = self.session
        if self.config.timing != ex.Timing.NEXT_OPEN:
            self._execute_due()
        self._stops(s)
        for item in self.pending_close.pop(s, []):
            self._close_item(item)
        for pos in self.positions.values():
            sd = self._slot(pos.slot)
            if pos.state == "OPEN" and sd.executable(s):
                pos.mark, pos.mark_session = sd.bars[s].close, s
        # Overnight financing, charged at the close for the calendar nights to the next session
        # (a Friday close pays three): borrow on short value at the close mark; interest only
        # when a rate is configured.
        nights = (date.fromisoformat(ex.next_session(s)) - date.fromisoformat(s)).days
        for pos in self.positions.values():
            if pos.quantity < 0:
                fee = ex.borrow_fee(-pos.market_value, pos.borrow_rate or ZERO, nights, self.config)
                if fee:
                    self.journal.post(
                        s, "BORROW_FEE", {"BORROW_EXPENSE": fee, "CASH": -fee}, slot=pos.slot
                    )
        if self.config.cash_interest_rate_annual and self.cash > 0:
            interest = (self.cash * self.config.cash_interest_rate_annual * nights / 360).quantize(
                D("0.01"), rounding=ROUND_FLOOR
            )
            self.journal.post(s, "INTEREST", {"CASH": interest, "INTEREST_INCOME": -interest})
        self._window_boundary(s)
        self._breakers(self.equity())
        snap = self.snapshot()
        self.snapshots.append(snap)
        self.phase = "CLOSED"
        self.check_invariants()
        return snap

    def _sale_bps(self) -> D:
        p = self.config.preferences
        return (p.fallback_spread_bps / 2 + p.slippage_bps) if self.config.costs_enabled else ZERO

    def _sell_received(self, quantity: D, ratio: Fraction, close: D) -> tuple[D, D, D]:
        """Whole received shares (``quantity`` x ``ratio``) sold at their valuation close
        (assumed half spread and slippage, commission); the fraction becomes cash in lieu at
        the close (receivable)."""
        whole, fraction = exact_shares(quantity, ratio)
        net = (whole * close * (ONE - self._sale_bps() / BPS)).quantize(MICRO)
        return net, ex.commission(whole, self.config), money_of(fraction, close)

    def _buy_owed(self, quantity: D, ratio: Fraction, close: D) -> tuple[D, D]:
        """A short's delivery of ``quantity`` x ``ratio`` shares bought at the close."""
        whole, fraction = exact_shares(quantity, ratio)
        cost = (
            money_of(Fraction(whole) + fraction, close) * (ONE + self._sale_bps() / BPS)
        ).quantize(MICRO)
        return cost, ex.commission(whole + (1 if fraction else 0), self.config)

    def _close_item(self, item: dict[str, Any]) -> None:
        pos = self.positions.get(item["slot"])
        s = self.session
        if item["kind"] == "CASH_IN_LIEU":
            if pos is None:
                return
            value = (
                money_of(abs(item["fraction"]), self._slot(pos.slot).bars[s].close)
                if s in self._slot(pos.slot).bars
                else ZERO
            )
            if pos.quantity > 0 and value:
                self.journal.post(s, "CASH_IN_LIEU_RECEIVABLE", {"RECEIVABLE_EXIT_CASH": value, "REALIZED_PNL": -value},
                                  slot=pos.slot)  # fmt: skip
                self.receivables.append(
                    {
                        "kind": "CASH_IN_LIEU",
                        "slot": pos.slot,
                        "date": s,
                        "amount": str(value),
                        "pay_date": None,
                    }
                )
            elif value:
                self.journal.post(
                    s, "CASH_IN_LIEU_PAID", {"REALIZED_PNL": value, "CASH": -value}, slot=pos.slot
                )
            return
        if item["kind"] == "DISTRIBUTION":
            if pos is None:
                return
            c = item["component"]
            f = acc._ratio(c["ratio"])
            parent = abs(item["parent_shares"])
            shares = D(f.numerator) * parent / D(f.denominator)  # reported only
            close = dec(c["close"])
            if item["parent_shares"] > 0:
                net, fee, lieu = self._sell_received(parent, f, close)
                self.journal.post(s, "DISTRIBUTION_SOLD",
                                  {"CASH": net - fee, "COMMISSION_EXPENSE": fee, "RECEIVABLE_EXIT_CASH": lieu,
                                   "REALIZED_PNL": -(net + lieu)}, slot=pos.slot, ticker=c["ticker"])  # fmt: skip
                if lieu:
                    self.receivables.append(
                        {
                            "kind": "CASH_IN_LIEU",
                            "slot": pos.slot,
                            "date": s,
                            "amount": str(lieu),
                            "pay_date": None,
                        }
                    )
                self._event("DISTRIBUTION_SOLD", slot=pos.slot, ticker=c["ticker"], shares=str(shares),
                            proceeds=str(net - fee), cash_in_lieu=str(lieu))  # fmt: skip
            else:  # a short owes the distribution: bought at the close and delivered
                cost, fee = self._buy_owed(parent, f, close)
                self.journal.post(s, "DISTRIBUTION_DELIVERED", {"REALIZED_PNL": cost, "COMMISSION_EXPENSE": fee,
                                  "CASH": -(cost + fee)}, slot=pos.slot)  # fmt: skip
                self._event(
                    "DISTRIBUTION_DELIVERED",
                    slot=pos.slot,
                    ticker=c["ticker"],
                    cost=str(cost + fee),
                )
            return
        if pos is None or pos.state != "AWAITING_EXIT":
            return
        self._exit(pos)

    def _exit(self, pos: Position) -> None:
        sd = self._slot(pos.slot)
        term = sd.terminal
        if term is None:
            raise LedgerError(f"{pos.slot}: an exit without a terminal value")
        s = self.session
        q = abs(pos.quantity)
        cash_ps = sum(
            (dec(c["per_share"]) for c in term["components"] if c["kind"] == "cash"), ZERO
        )
        cash_part = q * cash_ps
        sold = lieu = fee = ZERO
        for c in term["components"]:
            if c["kind"] != "stock":
                continue
            f = acc._ratio(c["ratio"])
            close = dec(c["close"])
            if pos.quantity > 0:
                net, c_fee, c_lieu = self._sell_received(q, f, close)
                sold, fee, lieu = sold + net, fee + c_fee, lieu + c_lieu
            else:
                c_cost, c_fee = self._buy_owed(q, f, close)
                sold, fee = sold + c_cost, fee + c_fee
        if pos.quantity > 0:
            self.journal.post(s, "EXIT_CONVERTED",
                              {"RECEIVABLE_EXIT_CASH": cash_part + lieu, "CASH": sold - fee, "COMMISSION_EXPENSE": fee,
                               "LONG_POSITIONS_AT_COST": -pos.cost,
                               "REALIZED_PNL": -(cash_part + lieu + sold - pos.cost)}, slot=pos.slot)  # fmt: skip
            if cash_part + lieu:
                self.receivables.append({"kind": "EXIT_CASH", "slot": pos.slot, "date": s,
                                         "amount": str(cash_part + lieu), "pay_date": None})  # fmt: skip
        else:
            owed = cash_part + sold
            self.journal.post(s, "EXIT_DELIVERED",
                              {"SHORT_POSITIONS_AT_COST": -pos.cost, "SHORT_PROCEEDS_RESTRICTED": pos.cost,
                               "CASH": -pos.cost - owed - fee, "COMMISSION_EXPENSE": fee,
                               "REALIZED_PNL": owed + pos.cost}, slot=pos.slot)  # fmt: skip
        self._event("EXIT", slot=pos.slot, status=term["status"], value_per_share=str(term["value_per_share"]),
                    receivable=str(cash_part + lieu), proceeds=str(sold - fee), shares=str(pos.quantity))  # fmt: skip
        self._close_position(pos, "EXIT")

    def _close_position(self, pos: Position, why: str) -> None:
        self.closed.append({**pos.as_json(), "closed": self.session, "why": why})
        del self.positions[pos.slot]

    def _window_boundary(self, s: str) -> None:
        for slot in sorted(self.positions):
            pos = self.positions[slot]
            sd = self._slot(slot)
            if pos.state != "OPEN" or sd.window[1] != s or s == DATA_END:
                continue
            if self.config.window_end_policy == ex.WindowEndPolicy.ROLL_OVER_IF_MEMBER:
                nxt = self.market.successor_slot(slot)
                step = formation_step(nxt, self.market) if nxt else None
                if (
                    nxt
                    and step
                    and step["status"] == "COMPUTED"
                    and nxt.formation_close == pos.mark
                ):
                    del self.positions[slot]
                    pos.slot = nxt.slot
                    if nxt.slot in self.positions:  # pragma: no cover  # entries need membership
                        raise LedgerError(f"{nxt.slot} already held")
                    self.positions[nxt.slot] = pos
                    self._event("ROLLED_OVER", slot=slot, to=nxt.slot, formation_close=str(nxt.formation_close),
                                formation_step=step["gross"])  # fmt: skip
                    continue
                self._freeze(pos, "WINDOW_END_NOT_A_MEMBER_NO_DATA")
            else:
                self._freeze(pos, "WINDOW_END_EXIT_UNFILLED")

    def _stops(self, s: str) -> None:
        for slot in sorted(self.positions):
            pos = self.positions[slot]
            sd = self._slot(slot)
            if pos.state != "OPEN" or pos.stop is None or not sd.executable(s):
                continue
            bar = sd.bars[s]
            long = pos.quantity > 0
            if (long and bar.low > pos.stop) or (not long and bar.high < pos.stop):
                continue
            gap = (long and bar.open <= pos.stop) or (not long and bar.open >= pos.stop)
            reference = bar.open if gap else pos.stop
            side = Side.SELL if long else Side.BUY_TO_COVER
            prev = self.snapshots[-1]["session"]
            liq = self._liquidity(slot, prev)
            intent = OrderIntent(f"stop:{slot}:{s}", slot, side, abs(pos.quantity), reason="STOP")
            fill = ex.quote_fill(
                side, abs(pos.quantity), bar, liq, self.config, reference=reference
            )
            fill = self._fit_cash(fill, Reservation(intent, fill.requested, prev, s, reference, liq, ZERO, ZERO, ZERO), bar,
                                  reference=reference)  # fmt: skip
            if fill.quantity > 0:
                self._apply_fill(
                    fill, intent, decided=prev, extra={"stop": str(pos.stop), "gap": gap}
                )

    # ------------------------------------------------------------------ fills
    def _liquidity(self, slot: str, asof: str) -> ex.Liquidity:
        sd = self._slot(slot)
        return ex.estimate_liquidity(self.market.history(slot, asof), asof, self.config,
                                     share_ratios(self.market, slot),
                                     (sd.formation_close, sd.formation_median_dollar_volume))  # fmt: skip

    def _execute_due(self) -> None:
        s = self.session
        due = [r for r in self.reservations if r.execute_on == s]
        due.sort(key=lambda r: (r.intent.side.opens, r.intent.intent_id))  # exits first
        for r in due:
            self._execute(r)
            self.reservations.remove(r)

    def _no_fill(self, r: Reservation, reason: str) -> None:
        self.fills.append({"intent_id": r.intent.intent_id, "slot": r.intent.slot, "session": self.session,
                           "status": "NO_FILL", "reasons": [reason]})  # fmt: skip

    def _execute(self, r: Reservation) -> None:
        s, it = self.session, r.intent
        sd = self._slot(it.slot)
        pos = self.positions.get(it.slot)
        if not sd.executable(s):
            return self._no_fill(r, "NOT_EXECUTABLE_SESSION")
        if it.side.opens:
            if not sd.member_effective(s):
                return self._no_fill(r, "NOT_A_MEMBER")
            if pos is not None and pos.state != "OPEN":
                return self._no_fill(r, "POSITION_NOT_TRADABLE")
        else:
            if pos is None or pos.state != "OPEN":
                return self._no_fill(r, "POSITION_NOT_TRADABLE")
        qty = r.quantity
        if not it.side.opens:
            qty = min(qty, abs(pos.quantity)) if pos else ZERO
        bar = sd.bars[s]
        if it.side == Side.SELL_SHORT:
            borrow = ex.borrow_for(sd.ticker, s, self.config)
            if borrow is None:
                return self._no_fill(r, "SHORT_BORROW_UNAVAILABLE")
            if self.config.rule_201:
                hist = self.market.history(it.slot, r.decided)
                prev = hist[-1] if hist and hist[-1].session == r.decided else None
                prior = (
                    hist[-2].close
                    if prev and len(hist) > 1 and ex.next_session(hist[-2].session) == prev.session
                    else None
                )
                why = ex.rule_201_restricted(prior, prev, bar, self.config.timing)
                if why:
                    return self._no_fill(r, why)
        fill = ex.quote_fill(it.side, qty, bar, r.liquidity, self.config, limit=it.limit_price)
        fill = self._fit_cash(fill, r, bar)
        if fill.quantity <= 0:
            return self._no_fill(r, fill.reasons[-1] if fill.reasons else "NO_FILL")
        self._apply_fill(fill, it, decided=r.decided)

    def _fit_cash(
        self, fill: ex.Fill, r: Reservation, bar: Bar, reference: D | None = None
    ) -> ex.Fill:
        """Shrink an order to the cash it may use: never a negative cash balance."""
        available = self.spendable() + r.cash + r.margin
        pos = self.positions.get(r.intent.slot)
        rate = self.config.hard_limits.min_short_margin_rate

        def need(f: ex.Fill) -> D:
            if f.side == Side.BUY:
                return f.notional + f.charges
            if f.side == Side.SELL_SHORT:
                return f.notional * rate + f.charges
            if f.side == Side.BUY_TO_COVER and pos is not None:
                released = -pos.cost * f.quantity / -pos.quantity
                return f.notional + f.charges - released - f.notional * rate
            return ZERO

        q = fill.quantity
        f = fill
        while q > 0 and need(f) > available:
            q -= max(ONE, ((need(f) - available) / f.price).to_integral_value(ROUND_FLOOR))
            q = max(q, ZERO)
            f = ex.quote_fill(
                fill.side,
                q,
                bar,
                r.liquidity,
                self.config,
                limit=r.intent.limit_price,
                reference=reference,
            )
            f = ex.Fill(
                **{
                    **f.__dict__,
                    "requested": fill.requested,
                    "reasons": (*f.reasons, "RESIZED_TO_CASH"),
                }
            )
        return f

    def _apply_fill(
        self, fill: ex.Fill, it: OrderIntent, decided: str, extra: dict[str, Any] | None = None
    ) -> None:
        s = self.session
        sd = self._slot(it.slot)
        q, n = fill.quantity, fill.notional
        costs = {"COMMISSION_EXPENSE": fill.commission, "FEE_EXPENSE": fill.fees,
                 "REGULATORY_FEE_EXPENSE": fill.regulatory_fee}  # fmt: skip
        pos = self.positions.get(it.slot)
        if fill.side in (Side.BUY, Side.SELL_SHORT) and pos is None:
            pos = Position(it.slot, sd.ticker, ZERO, ZERO, fill.price, s, s)
            self.positions[it.slot] = pos
        if pos is None:
            raise LedgerError(f"{it.slot}: an exit fill without a position")
        if fill.side == Side.BUY:
            self.journal.post(
                s,
                "BUY",
                {"LONG_POSITIONS_AT_COST": n, "CASH": -n - fill.charges, **costs},
                slot=it.slot,
            )
            pos.quantity += q
            pos.cost += n
            if it.stop_price is not None:
                pos.stop = it.stop_price
        elif fill.side == Side.SELL:
            basis = pos.cost if q == pos.quantity else (pos.cost * q / pos.quantity).quantize(MICRO)
            self.journal.post(s, "SELL", {"CASH": n - fill.charges, "LONG_POSITIONS_AT_COST": -basis,
                              "REALIZED_PNL": basis - n, **costs}, slot=it.slot)  # fmt: skip
            pos.quantity -= q
            pos.cost -= basis
        elif fill.side == Side.SELL_SHORT:
            borrow = ex.borrow_for(sd.ticker, s, self.config)
            self.journal.post(s, "SELL_SHORT", {"SHORT_PROCEEDS_RESTRICTED": n, "SHORT_POSITIONS_AT_COST": -n,
                              "CASH": -fill.charges, **costs}, slot=it.slot)  # fmt: skip
            pos.quantity -= q
            pos.cost -= n
            pos.borrow_rate = borrow.annual_rate if borrow else pos.borrow_rate
            if it.stop_price is not None:
                pos.stop = it.stop_price
        else:  # BUY_TO_COVER
            basis = (
                -pos.cost if q == -pos.quantity else (-pos.cost * q / -pos.quantity).quantize(MICRO)
            )
            self.journal.post(s, "BUY_TO_COVER", {"SHORT_POSITIONS_AT_COST": basis, "SHORT_PROCEEDS_RESTRICTED": -basis,
                              "CASH": basis - n - fill.charges, "REALIZED_PNL": n - basis, **costs}, slot=it.slot)  # fmt: skip
            pos.quantity += q
            pos.cost += basis
        times = ex.execution_times(decided, s, self.config.timing)
        self.fills.append({"intent_id": it.intent_id, "slot": it.slot, "status": "FILLED" if q == fill.requested else "PARTIAL",
                           "reason": it.reason, **fill.as_json(), **times, **(extra or {})})  # fmt: skip
        if pos.quantity == 0:
            self._close_position(pos, fill.side.value)

    # ------------------------------------------------------------------ decisions
    def decide(
        self, intents: list[OrderIntent] | tuple[OrderIntent, ...] = ()
    ) -> list[dict[str, Any]]:
        """Risk-check and reserve intents decided at this session's close, atomically each."""
        if self.phase != "CLOSED":
            raise LedgerError("decisions are taken after the session's close")
        s = self.session
        e = ex.next_session(s)
        auto: list[OrderIntent] = []
        for slot in sorted(self.positions):
            pos = self.positions[slot]
            sd = self._slot(slot)
            if pos.state != "OPEN":
                continue
            side = Side.SELL if pos.quantity > 0 else Side.BUY_TO_COVER
            if (self.config.window_end_policy == ex.WindowEndPolicy.EXIT_BEFORE_WINDOW_END
                    and sd.window[1] == e and e != DATA_END):  # fmt: skip
                auto.append(
                    OrderIntent(
                        f"auto:window-end:{slot}:{s}",
                        slot,
                        side,
                        abs(pos.quantity),
                        reason="WINDOW_END",
                    )
                )
            elif pos.quantity < 0 and ex.borrow_for(pos.ticker, e, self.config) is None:
                auto.append(
                    OrderIntent(
                        f"auto:borrow-end:{slot}:{s}",
                        slot,
                        side,
                        abs(pos.quantity),
                        reason="BORROW_ENDED",
                    )
                )
        out = []
        for it in sorted([*auto, *intents], key=lambda i: (i.side.opens, i.intent_id)):
            status, reasons, res = self._decide_one(it, s, e)
            rec = {"intent_id": it.intent_id, "slot": it.slot, "side": it.side.value, "session": s,
                   "execute_on": e, "requested": str(it.quantity), "status": status, "reasons": reasons,
                   "quantity": None if res is None else str(res.quantity),
                   "liquidity": None if res is None else res.liquidity.as_json()}  # fmt: skip
            if res is not None:
                self.reservations.append(res)
            self.decisions.append(rec)
            out.append(rec)
        return out

    def _exposures(self) -> dict[str, Any]:
        long = short = ZERO
        per_slot: dict[str, D] = defaultdict(D)
        per_class: dict[str, D] = defaultdict(D)
        for p in self.positions.values():
            mv = p.market_value
            per_slot[p.slot] += abs(mv)
            per_class[self._slot(p.slot).asset_class] += abs(mv)
            if mv >= 0:
                long += mv
            else:
                short -= mv
        for r in self.reservations:
            if not r.intent.side.opens:
                continue
            per_slot[r.intent.slot] += r.notional
            per_class[self._slot(r.intent.slot).asset_class] += r.notional
            if r.intent.side == Side.BUY:
                long += r.notional
            else:
                short += r.notional
        return {"long": long, "short": short, "slot": per_slot, "class": per_class}

    def _decide_one(
        self, it: OrderIntent, s: str, e: str
    ) -> tuple[str, list[str], Reservation | None]:
        h, cfg = self.config.hard_limits, self.config
        if it.intent_id in self.seen_intents:
            return "REJECTED", ["DUPLICATE_INTENT_ID"], None
        self.seen_intents.add(it.intent_id)
        sd = self.market.slots.get(it.slot)
        if sd is None or not sd.has_series:
            return "REJECTED", ["SLOT_NOT_USABLE"], None
        q = it.quantity.to_integral_value(ROUND_FLOOR)
        if q <= 0:
            return "REJECTED", ["INVALID_QUANTITY"], None
        pos = self.positions.get(it.slot)
        if not it.side.opens and (pos is None or pos.state != "OPEN"):
            return "REJECTED", ["NO_TRADABLE_POSITION"], None
        if sd.executable(s):
            ref = sd.bars[s].close
        elif s == sd.formation and sd.formation_close is not None:
            ref = sd.formation_close  # decided at the formation close (a new or rolled-over member)
        else:
            return "REJECTED", ["NO_DECISION_PRICE"], None
        pending = [r for r in self.reservations if r.intent.slot == it.slot]
        reasons: list[str] = []
        if it.side.opens:
            if not sd.member_effective(e):
                return "REJECTED", ["NOT_A_MEMBER_AT_EXECUTION"], None
            if self.blocked:
                return "REJECTED", ["CIRCUIT_BREAKER_" + k for k in sorted(self.blocked)], None
            if pos is not None and pos.state != "OPEN":
                return "REJECTED", ["POSITION_NOT_TRADABLE"], None
            long = it.side == Side.BUY
            if (pos is not None and (pos.quantity > 0) != long) or any(
                r.intent.side.opens and r.intent.side != it.side for r in pending
            ):
                return "REJECTED", ["OPPOSITE_POSITION_OR_ORDER"], None
            if not long:
                if not h.shorts_enabled:
                    return "REJECTED", ["SHORTS_DISABLED"], None
                if (
                    ex.borrow_for(sd.ticker, s, cfg) is None
                    or ex.borrow_for(sd.ticker, e, cfg) is None
                ):
                    return "REJECTED", ["SHORT_BORROW_UNAVAILABLE"], None
            if h.require_stop and it.stop_price is None:
                return "REJECTED", ["STOP_REQUIRED"], None
            if it.stop_price is not None:
                wrong = it.stop_price >= ref if long else it.stop_price <= ref
                if wrong or abs(ref - it.stop_price) / ref * BPS < h.min_stop_distance_bps:
                    return "REJECTED", ["STOP_INVALID"], None
        else:
            if pos is None:  # checked above; kept for the type checker
                return "REJECTED", ["NO_TRADABLE_POSITION"], None
            if (pos.quantity > 0) != (it.side == Side.SELL):
                return "REJECTED", ["SIDE_DOES_NOT_MATCH_POSITION"], None
            held = abs(pos.quantity) - sum(
                (r.quantity for r in pending if not r.intent.side.opens), ZERO
            )
            if q > held:
                q = held
                reasons.append("RESIZED_TO_POSITION")
            if q <= 0:
                return "REJECTED", ["NOTHING_LEFT_TO_EXIT"], None
        liq = self._liquidity(it.slot, s)
        caps: dict[str, D] = {}
        if it.side.opens:
            if liq.adv_shares is None:
                return "REJECTED", ["NO_LIQUIDITY_ESTIMATE"], None
            if liq.spread_bps > h.max_spread_bps:
                return "REJECTED", ["SPREAD_ABOVE_LIMIT"], None
            caps["ADV_PARTICIPATION"] = (
                liq.adv_shares * h.max_adv_participation_pct
            ).to_integral_value(ROUND_FLOOR)
        bps = ex.cost_bps(q, liq, cfg)
        worst = ref * (ONE + (bps["total"] + cfg.reserve_buffer_bps) / BPS)
        res_cash = res_margin = ZERO
        if it.side.opens:
            equity = self.equity()
            x = self._exposures()
            rate = h.min_short_margin_rate
            per_share = (
                worst * (ONE + cfg.preferences.fee_bps / BPS) + cfg.preferences.commission_per_unit
            )
            room = {
                "SINGLE_ASSET": h.max_single_asset_pct * equity - x["slot"][it.slot],
                "GROSS_EXPOSURE": h.max_gross_exposure_pct * equity - x["long"] - x["short"],
                "ASSET_CLASS": h.asset_class_limit(sd.asset_class) * equity
                - x["class"][sd.asset_class],
                "NET_EXPOSURE": h.max_net_exposure_pct * equity - abs(x["long"] - x["short"]),
            }
            if it.side == Side.BUY:
                room["CASH"] = self.spendable() - cfg.preferences.min_commission
                room["NET_EXPOSURE"] = h.max_net_exposure_pct * equity - (x["long"] - x["short"])
            else:
                room["NET_EXPOSURE"] = h.max_net_exposure_pct * equity - (x["short"] - x["long"])
                room["SHORT_EXPOSURE"] = h.max_short_exposure_pct * equity - x["short"]
                margin_used = self.margin_required() + sum(
                    (r.margin for r in self.reservations), ZERO
                )
                room["MARGIN_USAGE"] = (
                    (h.max_margin_usage_pct * equity - margin_used) / rate if rate else equity
                )
                room["CASH"] = (
                    (self.spendable() - cfg.preferences.min_commission) / rate if rate else equity
                )
            for k, v in room.items():
                caps[k] = max(ZERO, (v / per_share).to_integral_value(ROUND_FLOOR))
            if it.stop_price is not None:
                per_risk = (
                    abs(ref - it.stop_price)
                    + ref * bps["total"] / BPS
                    + 2 * cfg.preferences.commission_per_unit
                )
                budget = (
                    h.risk_per_trade_pct
                    * equity
                    * (ONE if it.side == Side.BUY else h.short_risk_budget_fraction)
                )
                caps["RISK_PER_TRADE"] = (budget / per_risk).to_integral_value(ROUND_FLOOR)
            opened = {p.slot for p in self.positions.values()} | {
                r.intent.slot for r in self.reservations if r.intent.side.opens
            }
            if it.slot not in opened and len(opened) >= h.max_open_positions:
                return "REJECTED", ["MAX_OPEN_POSITIONS"], None
            binding = sorted(k for k, v in caps.items() if v < q)
            if binding:
                if cfg.oversize_policy == ex.OversizePolicy.REJECT:
                    return "REJECTED", [f"EXCEEDS_{k}" for k in binding], None
                q = min(caps[k] for k in binding)
                reasons += [f"RESIZED_TO_{k}" for k in binding]
            if q <= 0:
                return "REJECTED", [*reasons, "NO_ROOM"], None
            notional = q * worst
            charges = ex.commission(q, cfg) + ex.other_fees(notional, cfg)
            if it.side == Side.BUY:
                while q > 0 and notional + charges > self.spendable():
                    q -= 1
                    notional = q * worst
                    charges = ex.commission(q, cfg) + ex.other_fees(notional, cfg)
                res_cash = notional + charges
            else:
                res_margin = notional * rate
                res_cash = charges
            if q <= 0:
                return "REJECTED", [*reasons, "NO_ROOM"], None
        else:
            notional = q * worst
            if it.side == Side.BUY_TO_COVER and pos is not None:
                released = -pos.cost * q / -pos.quantity
                res_cash = max(
                    ZERO, notional + ex.commission(q, cfg) + ex.other_fees(notional, cfg) - released
                )
        status = "RESIZED" if reasons else "APPROVED"
        return status, reasons, Reservation(it, q, s, e, ref, liq, res_cash, res_margin, notional)

    # ------------------------------------------------------------------ breakers
    def _breakers(self, equity: D) -> None:
        h = self.config.hard_limits
        self.peak = max(self.peak, equity)
        self.blocked.pop("DAILY_LOSS", None)
        if (
            self.day_start_equity
            and (self.day_start_equity - equity) / self.day_start_equity >= h.daily_loss_limit_pct
        ):
            self.blocked["DAILY_LOSS"] = self.session
        if (
            self.week_start_equity
            and (self.week_start_equity - equity) / self.week_start_equity
            >= h.weekly_loss_limit_pct
        ):
            self.blocked["WEEKLY_LOSS"] = self.session
        elif (
            "WEEKLY_LOSS" in self.blocked
            and date.fromisoformat(self.blocked["WEEKLY_LOSS"]).isocalendar()[:2]
            != date.fromisoformat(self.session).isocalendar()[:2]
        ):
            self.blocked.pop("WEEKLY_LOSS")
        if self.cash < 0:  # a short's obligations exceeded the cash held: no new entries
            self.blocked["CASH_DEFICIT"] = self.session
        else:
            self.blocked.pop("CASH_DEFICIT", None)
        if (self.peak - equity) / self.peak >= h.max_drawdown_pct:
            self.blocked["DRAWDOWN"] = self.session
        else:
            self.blocked.pop("DRAWDOWN", None)

    # ------------------------------------------------------------------ records
    def snapshot(self) -> dict[str, Any]:
        long = sum((p.market_value for p in self.positions.values() if p.quantity > 0), ZERO)
        short = -sum((p.market_value for p in self.positions.values() if p.quantity < 0), ZERO)
        frozen = sorted(p.slot for p in self.positions.values() if p.state == "FROZEN")
        awaiting = sorted(p.slot for p in self.positions.values() if p.state == "AWAITING_EXIT")
        unrealized = sum((p.unrealized for p in self.positions.values()), ZERO)
        return {
            "session": self.session,
            "cash": str(self.cash),
            "spendable": str(self.spendable()),
            "reserved": str(sum((r.cash + r.margin for r in self.reservations), ZERO)),
            "margin_required": str(self.margin_required()),
            "short_proceeds_restricted": str(self.journal.balance("SHORT_PROCEEDS_RESTRICTED")),
            "receivables": {a: str(self.journal.balance(a)) for a in RECEIVABLE_ACCOUNTS},
            "long_market_value": str(long),
            "short_market_value": str(short),
            "equity": str(self.equity()),
            "realized_pnl": str(-self.journal.balance("REALIZED_PNL")),
            "unrealized_pnl": str(unrealized),
            "positions": len(self.positions),
            "frozen": frozen,
            "awaiting_exit": awaiting,
            "equity_complete": not frozen,
            "entries_blocked": sorted(self.blocked),
        }

    def check_invariants(self) -> None:
        """The journal, the positions and the cash rules agree; raises on any difference."""
        b = self.journal.balance
        errors: list[str] = []
        if sum(self.journal.balances.values(), ZERO) != 0:
            errors.append("journal does not balance")
        long_cost = sum((p.cost for p in self.positions.values() if p.quantity > 0), ZERO)
        short_cost = sum((p.cost for p in self.positions.values() if p.quantity < 0), ZERO)
        if b("LONG_POSITIONS_AT_COST") != long_cost:
            errors.append(f"long cost {b('LONG_POSITIONS_AT_COST')} != positions {long_cost}")
        if b("SHORT_POSITIONS_AT_COST") != short_cost:
            errors.append(f"short cost {b('SHORT_POSITIONS_AT_COST')} != positions {short_cost}")
        if b("SHORT_PROCEEDS_RESTRICTED") != -short_cost:
            errors.append("restricted short proceeds differ from the short liability at cost")
        listed = sum((D(r["amount"]) for r in self.receivables), ZERO)
        if listed != self.receivables_total():
            errors.append(f"receivables {self.receivables_total()} != listed {listed}")
        for a in RECEIVABLE_ACCOUNTS:
            if b(a) < 0:
                errors.append(f"{a} negative")
        income = -sum((b(a) for a in (*EQUITY_ACCOUNTS, *INCOME, *EXPENSES)), ZERO)
        unrealized = sum((p.unrealized for p in self.positions.values()), ZERO)
        if self.equity() != income + unrealized:
            errors.append(f"equity {self.equity()} != capital+P&L {income + unrealized}")
        if any(p.quantity == 0 for p in self.positions.values()):
            errors.append("a zero position is open")
        if errors:
            self.violations += [f"{self.session}: {e}" for e in errors]
            raise LedgerError("; ".join(errors))


def run_hold(
    market: Market, config: X6Config, slot: str, quantity: D, start: str, end: str
) -> Portfolio:
    """Accounting replay of one position held from the close of ``start`` (booked at that
    close, no trade, no cost) through ``end``: the ledger's reconciliation with X5's factors.
    Not a backtest: no decision, no order, no performance figure."""
    sd = market.slots[slot]
    pf = Portfolio(market, config, D("1000000000"), start)
    price = sd.bars[start].close if start in sd.bars else sd.formation_close
    if price is None:
        raise ValueError(f"{slot}: no close at {start}")
    n = quantity * price
    pf.journal.post(start, "OPENING_POSITION", {"LONG_POSITIONS_AT_COST": n, "CASH": -n}, slot=slot)
    pf.positions[slot] = Position(slot, sd.ticker, quantity, n, price, start, start)
    pf.snapshots.append(pf.snapshot())
    for s in itertools.islice(acc._sessions(ex.next_session(start), end), None):
        pf.begin_session(s)
        pf.close_session()
    return pf
