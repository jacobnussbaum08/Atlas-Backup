# ruff: noqa: E501  (assumption tables and journal lines read as one row each)
"""X6 (D-255): historical execution on daily bars: timing, prices, costs, liquidity, shorts.

**Hypothetical backtest execution, not brokerage activity.** Nothing here submits an order to
any broker. Every number is either evidence (a recorded bar, an X5 corporate action, an X4.4
formation record), a published regulatory rule, or an explicit configurable assumption, and
:meth:`X6Config.assumptions` labels each one as such.

Timing (spec 18.1, 15.3): a decision is taken at the close of session *t* with data up to and
including *t*. It executes on the next XNYS session *t+1* (holiday and early-close aware) at
that session's open, VWAP or close (:class:`Timing`). The decision never sees *t+1*; the
execution session's own bar only prices the fill and caps its size (it can only reduce it).

Price: reference (open, VWAP or close of *t+1*) moved adversely by half the estimated spread,
the slippage assumption and a square-root impact estimate, rounded adversely to the tick, then
held inside the session's traded range: a fill is never above the high or below the low.

Liquidity estimates use only executable bars up to *t*: average daily volume (split-adjusted),
the Abdi-Ranaldo (2017) close-high-low spread estimator, and the daily volatility of
close-to-close log returns. Pairs across a gap or a split/distribution ex-date are skipped.
Without enough history, the X4.4 formation record (median dollar volume / formation close)
gives the volume, and the spread and volatility are the configured fallbacks (labelled).

Shorts: never assumed possible. A short sale needs an explicit borrow entry covering both the
decision and the execution session (:class:`BorrowAssumption`; no historical borrow data is in
the evidence, so the default book is empty and every short is rejected). SEC Rule 201 (the
alternative uptick rule) is applied conservatively from the daily bars.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data_engine.calendar import next_trading_day, trading_day
from app.research.x6_data import Bar
from app.risk.config import HardLimits, Preferences

D = Decimal
BPS = D(10000)
CENT = D("0.01")
ZERO = D(0)
ONE = D(1)


class Timing(StrEnum):
    NEXT_OPEN = "NEXT_OPEN"
    NEXT_VWAP = "NEXT_VWAP"
    NEXT_CLOSE = "NEXT_CLOSE"


class WindowEndPolicy(StrEnum):
    # Exit every position of a slot at its window's last session, by an order decided at the
    # close of the session before it (no knowledge of the next formation is needed).
    EXIT_BEFORE_WINDOW_END = "EXIT_BEFORE_WINDOW_END"
    # Hold through the window's last close; at that close (the next formation, known then)
    # continue the position in the same security's next slot, else freeze it (no data).
    ROLL_OVER_IF_MEMBER = "ROLL_OVER_IF_MEMBER"


class OversizePolicy(StrEnum):
    RESIZE = "RESIZE"
    REJECT = "REJECT"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"
    BUY_TO_COVER = "BUY_TO_COVER"

    @property
    def buys(self) -> bool:
        return self in (Side.BUY, Side.BUY_TO_COVER)

    @property
    def opens(self) -> bool:
        return self in (Side.BUY, Side.SELL_SHORT)


class BorrowAssumption(BaseModel):
    """One explicit, sourced statement that a security could be borrowed over a period at a
    rate. Not evidence unless ``source`` names evidence: it is recorded as configured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(min_length=1, max_length=16)
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    annual_rate: D = Field(ge=0, le=5)
    source: str = Field(min_length=3)

    def covers(self, ticker: str, session: str) -> bool:
        return self.ticker == ticker and self.start <= session <= self.end


class FeeRate(BaseModel):
    """A dated sell-side regulatory fee rate (per dollar of proceeds), from a cited source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effective: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    rate: D = Field(ge=0, le=D("0.001"))
    source: str = Field(min_length=3)


class X6Config(BaseModel):
    """The versioned, content-hashed execution and accounting assumptions of X6."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(default="atlas-x6-historical-execution", pattern=r"^[a-z0-9-]{3,64}$")
    version: int = Field(default=1, ge=1)
    timing: Timing = Timing.NEXT_OPEN
    window_end_policy: WindowEndPolicy = WindowEndPolicy.EXIT_BEFORE_WINDOW_END
    # The existing risk limits and cost preferences (spec 14.1/14.4), unchanged.
    hard_limits: HardLimits = Field(default_factory=HardLimits)
    preferences: Preferences = Field(default_factory=Preferences)
    # Liquidity estimation (point in time).
    liquidity_window_sessions: int = Field(default=20, ge=5, le=250)
    min_history_sessions: int = Field(default=10, ge=2, le=250)
    # Share of the execution session's recorded volume one order may take (as the paper
    # broker's bar_participation, execution model v1-v3).
    session_volume_participation: D = Field(default=D("0.05"), gt=0, le=1)
    oversize_policy: OversizePolicy = OversizePolicy.RESIZE
    min_spread_bps: D = Field(default=D("1"), ge=0, le=1000)
    impact_coefficient: D = Field(default=D("1"), ge=0, le=10)
    assumed_daily_volatility: D = Field(default=D("0.02"), gt=0, le=1)
    # Worst-case price buffer for cash reservations (an entry's reserved cash covers its
    # estimated cost plus this; a fill needing more is resized to the cash available).
    reserve_buffer_bps: D = Field(default=D("200"), ge=0, le=5000)
    # Fees that need dated evidence: not modelled unless configured.
    regulatory_fee_schedule: tuple[FeeRate, ...] = ()
    withholding_rates: dict[str, D] = Field(default_factory=dict)
    cash_interest_rate_annual: D = Field(default=ZERO, ge=0, le=1)
    borrow_book: tuple[BorrowAssumption, ...] = ()
    borrow_day_count: int = Field(default=360, ge=360, le=366)
    rule_201: bool = True
    # Costs on/off (off only to reconcile the ledger with X5's cost-free total returns).
    costs_enabled: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> X6Config:
        if self.min_history_sessions > self.liquidity_window_sessions:
            raise ValueError("min_history_sessions cannot exceed liquidity_window_sessions")
        for t, r in self.withholding_rates.items():
            if not (ZERO <= r <= ONE):
                raise ValueError(f"withholding rate for {t} must be a fraction")
        return self

    def definition(self) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(self.model_dump_json())
        return out

    def sha256(self) -> str:
        canonical = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def assumptions(self) -> list[dict[str, Any]]:
        """Every input of a fill or a ledger entry, labelled by what it is."""
        p, h = self.preferences, self.hard_limits
        a = "CONFIGURED_ASSUMPTION"
        rows: list[dict[str, Any]] = [
            {"name": "bars (open/high/low/close/volume/vwap)", "kind": "EVIDENCE",
             "source": "X4.5 member data V2 (provider SIP raw daily bars), pinned"},
            {"name": "corporate actions, terminal values, breaks", "kind": "EVIDENCE",
             "source": "X5 ledger and total returns (D-254), pinned"},
            {"name": "formation close and median dollar volume", "kind": "EVIDENCE",
             "source": "X4.4 universe V2 formation records, pinned"},
            {"name": "trading calendar", "kind": "EVIDENCE",
             "source": "XNYS calendar (data_engine.calendar, cross-checked 2004-2027)"},
            {"name": "timing", "value": self.timing.value, "kind": a},
            {"name": "window_end_policy", "value": self.window_end_policy.value, "kind": a},
            {"name": "spread estimate", "kind": "ESTIMATE",
             "source": "Abdi-Ranaldo (2017) close-high-low estimator on bars up to the decision",
             "floor_bps": str(self.min_spread_bps)},
            {"name": "fallback spread (no history)", "value": str(p.fallback_spread_bps), "kind": a,
             "source": "risk Preferences.fallback_spread_bps"},
            {"name": "slippage_bps", "value": str(p.slippage_bps), "kind": a,
             "source": "risk Preferences.slippage_bps"},
            {"name": "impact", "kind": a, "value": f"{self.impact_coefficient} * daily vol * sqrt(qty / ADV)",
             "source": "square-root impact form; the coefficient is an assumption"},
            {"name": "assumed_daily_volatility (no history)", "value": str(self.assumed_daily_volatility), "kind": a},
            {"name": "session_volume_participation", "value": str(self.session_volume_participation), "kind": a},
            {"name": "max_adv_participation_pct", "value": str(h.max_adv_participation_pct), "kind": a,
             "source": "risk HardLimits"},
            {"name": "commission", "kind": a, "source": "risk Preferences",
             "value": f"{p.commission_per_unit}/share, minimum {p.min_commission}"},
            {"name": "other fees", "value": f"{p.fee_bps} bps of notional", "kind": a,
             "source": "risk Preferences.fee_bps"},
            {"name": "regulatory (SEC section 31) fee",
             "kind": "CONFIGURED_SCHEDULE" if self.regulatory_fee_schedule else "NOT_MODELLED",
             "value": [r.model_dump(mode="json") for r in self.regulatory_fee_schedule]},
            {"name": "dividend withholding", "kind": a if self.withholding_rates else "NOT_MODELLED",
             "value": {k: str(v) for k, v in sorted(self.withholding_rates.items())}},
            {"name": "taxes", "kind": "NOT_MODELLED"},
            {"name": "interest on cash", "kind": a if self.cash_interest_rate_annual else "NOT_MODELLED",
             "value": str(self.cash_interest_rate_annual)},
            {"name": "short borrow availability and rate",
             "kind": "CONFIGURED_ASSUMPTION" if self.borrow_book else "NO_EVIDENCE_ALL_SHORTS_REJECTED",
             "value": [b.model_dump(mode="json") for b in self.borrow_book]},
            {"name": "short margin", "value": str(h.min_short_margin_rate), "kind": "REGULATORY_RULE",
             "source": "Regulation T 150% initial requirement (risk HardLimits.min_short_margin_rate)"},
            {"name": "Rule 201", "value": self.rule_201, "kind": "REGULATORY_RULE",
             "source": "SEC Rule 201 (alternative uptick rule): 10% decline from the prior close"},
            {"name": "dividend and exit-cash pay dates", "kind": "NOT_IN_EVIDENCE",
             "source": "X5: receivables, never spendable"},
            {"name": "settlement", "kind": a,
             "value": "sale proceeds usable after the fill's own time (margin-account convention); no T+n lag"},
            {"name": "reserve_buffer_bps", "value": str(self.reserve_buffer_bps), "kind": a},
        ]  # fmt: skip
        return rows


DEFAULT_CONFIG = X6Config()


# ---------------------------------------------------------------------- calendar
@lru_cache(maxsize=4096)
def next_session(session: str) -> str:
    return next_trading_day(date.fromisoformat(session)).day.isoformat()


def execution_times(decided: str, executed: str, timing: Timing) -> dict[str, str]:
    """UTC times: the decision at the close of its session, the execution in the next one."""
    d, e = trading_day(date.fromisoformat(decided)), trading_day(date.fromisoformat(executed))
    if d is None or e is None:
        raise ValueError(f"not a trading session: {decided} / {executed}")
    start = e.regular_open
    end = e.regular_close if timing != Timing.NEXT_OPEN else e.regular_open
    if timing == Timing.NEXT_CLOSE:
        start = e.regular_close
    if not start > d.regular_close:
        raise ValueError("an execution must follow its decision")
    return {
        "decided_at": d.regular_close.isoformat(),
        "execution_start": start.isoformat(),
        "execution_end": end.isoformat(),
    }


# ---------------------------------------------------------------------- liquidity
@dataclass(frozen=True)
class Liquidity:
    asof: str
    basis: str  # HISTORY, FORMATION_RECORD or NONE
    sessions: int
    adv_shares: D | None
    spread_bps: D
    spread_basis: str  # ABDI_RANALDO or ASSUMED_FALLBACK
    daily_volatility: D
    volatility_basis: str  # HISTORY or ASSUMED

    def as_json(self) -> dict[str, Any]:
        return {
            "asof": self.asof,
            "basis": self.basis,
            "sessions": self.sessions,
            "adv_shares": None if self.adv_shares is None else str(self.adv_shares),
            "spread_bps": str(self.spread_bps),
            "spread_basis": self.spread_basis,
            "daily_volatility": str(self.daily_volatility),
            "volatility_basis": self.volatility_basis,
        }


def _q(value: float, places: str) -> D:
    return D(repr(value)).quantize(D(places))


def estimate_liquidity(
    history: list[Bar],
    asof: str,
    config: X6Config,
    share_ratios: dict[str, D] | None = None,
    formation: tuple[D | None, D | None] = (None, None),
) -> Liquidity:
    """Point-in-time liquidity from bars up to ``asof`` (later bars are refused).

    ``share_ratios``: session -> share ratio applied on that ex-date (splits; distributions'
    share factors). Volumes before an ex-date are restated in post-ex shares, and no return or
    spread pair ends on an ex-date. ``formation``: the X4.4 record's (close, median dollar
    volume), used for the volume when history is short.
    """
    if any(b.session > asof for b in history):
        raise ValueError("liquidity estimate given bars after its as-of session (look-ahead)")
    ratios = share_ratios or {}
    window = config.liquidity_window_sessions
    bars = history[-window:]
    n = len(bars)
    p = config.preferences
    adv: D | None = None
    basis = "NONE"
    if n >= config.min_history_sessions:
        total = ZERO
        for b in bars:
            factor = ONE
            for d, r in ratios.items():
                if b.session < d <= asof:
                    factor *= r
            total += b.volume * factor
        adv = (total / n).quantize(D("1"), rounding=ROUND_FLOOR)
        basis = "HISTORY"
    elif formation[0] and formation[1]:
        adv = (formation[1] / formation[0]).quantize(D("1"), rounding=ROUND_FLOOR)
        basis = "FORMATION_RECORD"
    # Adjacent pairs (consecutive XNYS sessions), none ending on an ex-date.
    tail = history[-(window + 1) :]
    pairs = [
        (a, b)
        for a, b in itertools.pairwise(tail)
        if next_session(a.session) == b.session and b.session not in ratios
    ]
    if len(pairs) >= config.min_history_sessions:
        s2 = []
        rets = []
        for a, b in pairs:
            c = math.log(float(a.close))
            eta_a = (math.log(float(a.high)) + math.log(float(a.low))) / 2
            eta_b = (math.log(float(b.high)) + math.log(float(b.low))) / 2
            s2.append(max(4 * (c - eta_a) * (c - eta_b), 0.0))
            rets.append(math.log(float(b.close) / float(a.close)))
        spread = max(_q(math.sqrt(sum(s2) / len(s2)) * 10000, "0.01"), config.min_spread_bps)
        spread_basis = "ABDI_RANALDO"
        mean = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        volatility, vol_basis = _q(vol, "0.000001"), "HISTORY"
    else:
        spread, spread_basis = p.fallback_spread_bps, "ASSUMED_FALLBACK"
        volatility, vol_basis = config.assumed_daily_volatility, "ASSUMED"
    return Liquidity(asof, basis, n, adv, spread, spread_basis, volatility, vol_basis)


# ---------------------------------------------------------------------- prices and costs
def tick_for(price: D) -> D:
    return CENT if price >= ONE else D("0.0001")


def impact_bps(quantity: D, liquidity: Liquidity, config: X6Config) -> D:
    if quantity <= 0 or not liquidity.adv_shares:
        return ZERO
    share = float(quantity / liquidity.adv_shares)
    value = float(config.impact_coefficient) * float(liquidity.daily_volatility) * math.sqrt(share)
    return _q(value * 10000, "0.01")


def cost_bps(quantity: D, liquidity: Liquidity, config: X6Config) -> dict[str, D]:
    if not config.costs_enabled:
        return {"half_spread": ZERO, "slippage": ZERO, "impact": ZERO, "total": ZERO}
    half = (liquidity.spread_bps / 2).quantize(D("0.01"))
    slip = config.preferences.slippage_bps
    imp = impact_bps(quantity, liquidity, config)
    return {"half_spread": half, "slippage": slip, "impact": imp, "total": half + slip + imp}


def reference_price(bar: Bar, timing: Timing) -> D:
    if timing == Timing.NEXT_OPEN:
        return bar.open
    if timing == Timing.NEXT_CLOSE:
        return bar.close
    if bar.vwap is None:
        raise ValueError(f"{bar.session}: no VWAP recorded")
    return bar.vwap


def adverse_price(reference: D, total_bps: D, *, buys: bool, low: D, high: D) -> tuple[D, bool]:
    """Reference moved adversely, rounded adversely to the tick, held inside [low, high]."""
    raw = reference * (ONE + total_bps / BPS) if buys else reference * (ONE - total_bps / BPS)
    tick = tick_for(raw)
    price = (raw / tick).to_integral_value(ROUND_CEILING if buys else ROUND_FLOOR) * tick
    if price > high:
        return high, True
    if price < low:
        return low, True
    return price, False


def ceil_cents(value: D) -> D:
    return value.quantize(CENT, rounding=ROUND_CEILING)


def commission(quantity: D, config: X6Config) -> D:
    if quantity <= 0 or not config.costs_enabled:
        return ZERO
    p = config.preferences
    return ceil_cents(max(quantity * p.commission_per_unit, p.min_commission))


def other_fees(notional: D, config: X6Config) -> D:
    if notional <= 0 or not config.costs_enabled:
        return ZERO
    return ceil_cents(notional * config.preferences.fee_bps / BPS)


def regulatory_fee(notional: D, side: Side, session: str, config: X6Config) -> D:
    """Sell-side only, and only from a configured, sourced schedule (else not modelled)."""
    if side.buys or notional <= 0 or not config.costs_enabled:
        return ZERO
    rate = None
    for r in sorted(config.regulatory_fee_schedule, key=lambda x: x.effective):
        if r.effective <= session:
            rate = r.rate
    return ZERO if rate is None else ceil_cents(notional * rate)


@dataclass(frozen=True)
class Fill:
    side: Side
    session: str
    timing: Timing
    requested: D
    quantity: D
    reference: D
    price: D
    clamped: bool
    bps: dict[str, D]
    commission: D
    fees: D
    regulatory_fee: D
    reasons: tuple[str, ...]

    @property
    def notional(self) -> D:
        return self.quantity * self.price

    @property
    def charges(self) -> D:
        return self.commission + self.fees + self.regulatory_fee

    def as_json(self) -> dict[str, Any]:
        return {
            "side": self.side.value,
            "session": self.session,
            "timing": self.timing.value,
            "requested": str(self.requested),
            "quantity": str(self.quantity),
            "reference": str(self.reference),
            "price": str(self.price),
            "clamped_to_range": self.clamped,
            "bps": {k: str(v) for k, v in self.bps.items()},
            "commission": str(self.commission),
            "fees": str(self.fees),
            "regulatory_fee": str(self.regulatory_fee),
            "reasons": list(self.reasons),
        }


def session_cap(bar: Bar, config: X6Config) -> D:
    return (bar.volume * config.session_volume_participation).to_integral_value(ROUND_FLOOR)


def quote_fill(
    side: Side,
    requested: D,
    bar: Bar,
    liquidity: Liquidity,
    config: X6Config,
    limit: D | None = None,
    reference: D | None = None,
) -> Fill:
    """The fill of a whole-share order in ``bar``'s session, capped by the session's volume.
    ``reference`` overrides the timing's reference (a triggered stop: its stop price, or the
    open when the session gapped through it)."""
    reasons: list[str] = []
    qty = requested.to_integral_value(ROUND_FLOOR)
    cap = session_cap(bar, config)
    if qty > cap:
        qty = cap
        reasons.append("CAPPED_BY_SESSION_VOLUME")
    ref = reference_price(bar, config.timing) if reference is None else reference
    bps = cost_bps(qty, liquidity, config)
    price, clamped = adverse_price(ref, bps["total"], buys=side.buys, low=bar.low, high=bar.high)
    if clamped:
        reasons.append("PRICE_HELD_INSIDE_SESSION_RANGE")
    if limit is not None and (price > limit if side.buys else price < limit):
        reasons.append("LIMIT_NOT_REACHED")
        qty = ZERO
    if qty <= 0 and "LIMIT_NOT_REACHED" not in reasons:
        reasons.append("NO_LIQUIDITY")
    notional = qty * price
    return Fill(
        side,
        bar.session,
        config.timing,
        requested,
        qty,
        ref,
        price,
        clamped,
        bps,
        commission(qty, config),
        other_fees(notional, config),
        regulatory_fee(notional, side, bar.session, config),
        tuple(reasons),
    )


# ---------------------------------------------------------------------- shorts
def borrow_for(ticker: str, session: str, config: X6Config) -> BorrowAssumption | None:
    for b in config.borrow_book:
        if b.covers(ticker, session):
            return b
    return None


def rule_201_restricted(
    prior_close: D | None, previous: Bar | None, bar: Bar, timing: Timing
) -> str | None:
    """Whether SEC Rule 201 restricts a short sale executing in ``bar``'s session.

    The restriction applies for the rest of the day a security falls 10% below its prior
    close and for the whole next day. From daily bars: the previous session triggered it (its
    low <= 90% of its prior close), or this session triggered it before the execution (at the
    open for an opening execution; any time in the session for VWAP and close executions,
    since the order of trades inside the session is unknown). Restricted shorts would need to
    be priced above the national best bid, which daily bars cannot show: they are rejected.
    """
    if previous is not None and prior_close is not None and previous.low <= prior_close * D("0.9"):
        return "RULE_201_TRIGGERED_PREVIOUS_SESSION"
    if previous is None:
        return None
    limit = previous.close * D("0.9")
    if timing == Timing.NEXT_OPEN and bar.open <= limit:
        return "RULE_201_TRIGGERED_AT_OPEN"
    if timing != Timing.NEXT_OPEN and bar.low <= limit:
        return "RULE_201_TRIGGERED_IN_SESSION"
    return None


def borrow_fee(short_value: D, annual_rate: D, days: int, config: X6Config) -> D:
    if short_value <= 0 or days <= 0 or not config.costs_enabled:
        return ZERO
    return ceil_cents(short_value * annual_rate * days / config.borrow_day_count)
