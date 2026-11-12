"""The fill model of the simulated paper broker (Phase 7B): pure, deterministic functions.

**Simulated fills, not real executions**, and not guaranteed to match what a real venue
would have done. Given one working order, the market data observable at the evaluation time
(:mod:`app.paper_broker.market`) and the execution model's assumptions
(:mod:`app.paper_broker.execution_model`), :func:`evaluate` decides one of:

* FILLED / PARTIALLY_FILLED: a fill (quantity, price, costs, and the data it came from);
* TRIGGERED: a stop triggered and could not execute in the same evaluation;
* NO_FILL: the data was sufficient and the order did not execute (limit not reached, stop
  not triggered, liquidity already used, price beyond the reserved worst entry price);
* DEFERRED: the data was insufficient (closed market, no/stale quote and no usable bar,
  unknown liquidity): nothing is decided and nothing is invented.

Order types
-----------
MARKET: executes against the newest *fresh, unused* quote: buy at the ask, sell at the bid,
plus slippage and impact, rounded adversely to the tick; at most ``touch_participation`` of
the displayed size. With no fresh quote and ``allow_bar_fallback``: the newest completed bar
(fresh, after the order arrived) at its close widened by the assumed half spread, at most
``bar_participation`` of its volume. Otherwise DEFERRED.

LIMIT / TAKE_PROFIT: marketable at a fresh unused quote (ask <= buy limit, bid >= sell limit)
-> executes like a market order but never beyond the limit. Otherwise passive: only trades
printed *strictly through* the limit after the order arrived (or, with no trades, completed
bars whose range went strictly through it) fill it, at the limit, for at most
``trade_participation`` / ``bar_participation`` of that volume. A touch is not a fill.

STOP / TRAILING_STOP: triggers when the stop is reached: a sell stop by a trade, bar low or
fresh bid at or below it; a buy stop by a trade, bar high or fresh ask at or above it. A
triggered stop executes as a market order at the newest fresh quote and **never better than
its stop price** (a sell stop fills at most at the stop, a buy stop at least at it); a gap
through the stop fills at the worse market price. No execution at the stop is guaranteed.
Under the gap rule (model version 2, Phase 10B) a stop whose trigger observation was already
beyond the stop also fills no better than that observation adjusted by the slippage (a bar:
its open), in that evaluation and every later one (:func:`gap_fill_cap`).

STOP_LIMIT: the stop triggers as above; execution then follows the limit rules using only
data after the trigger. A triggered stop-limit may stay unfilled indefinitely.

Trailing stops: the reference starts at the first observed executable price after arrival
(bid for a sell, ask for a buy) and ratchets with the favourable extreme of each evaluation's
observations *after* those observations were checked against the previous stop: the model
never assumes that a bar's high came before its low (or the reverse). The newest quote is
checked against the ratcheted stop only if it is not older than the extreme that moved it.

Entries never fill beyond the reserved worst entry price (``protection_price``): the fill is
deferred instead. Exits never fill more than the position holds (``max_fill``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from app.accounting.money import PRICE_SCALE
from app.execution.enums import AttemptOutcome, DataBasis, OrderPurpose, OrderSide, OrderType
from app.paper_broker.execution_model import AssetClassModel
from app.paper_broker.market import Bar, MarketView, Quote, Trade

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)
_PRICE_QUANTUM = Decimal(1).scaleb(-PRICE_SCALE)
STOP_TYPES = frozenset({OrderType.STOP, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP})
LIMIT_TYPES = frozenset({OrderType.LIMIT, OrderType.TAKE_PROFIT})


# ---------------------------------------------------------------------- rounding
def floor_to(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def adverse_price(value: Decimal, tick: Decimal | None, *, buys: bool) -> Decimal:
    """Round a price against the order: buys up, sells down (to the tick, else to the
    ledger's price scale)."""
    step = tick if tick is not None and tick > 0 else _PRICE_QUANTUM
    return ceil_to(value, step) if buys else floor_to(value, step)


def _inside(
    price: Decimal, low: Decimal, high: Decimal, tick: Decimal | None, *, buys: bool
) -> Decimal:
    """Clamp to an observed range, staying on the tick grid (adverse rounding unless that
    would leave the range)."""
    clamped = min(max(price, low), high)
    rounded = adverse_price(clamped, tick, buys=buys)
    if low <= rounded <= high:
        return rounded
    return adverse_price(clamped, tick, buys=not buys)


def cost_round(value: Decimal, minor_units: int) -> Decimal:
    """Costs are rounded up to the currency's minor unit (never in Atlas's favour)."""
    return value.quantize(Decimal(1).scaleb(-minor_units), rounding=ROUND_CEILING)


# ---------------------------------------------------------------------- inputs / outputs
@dataclass(frozen=True)
class OrderState:
    """What the fill model needs to know about one working order (asset currency)."""

    side: OrderSide
    order_type: OrderType
    purpose: OrderPurpose
    quantity: Decimal
    remaining: Decimal
    # The most this evaluation may fill: ``remaining``, and for exits what the position holds.
    max_fill: Decimal
    quantity_increment: Decimal
    min_quantity: Decimal
    tick: Decimal | None
    minor_units: int
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_amount: Decimal | None = None
    trail_percent: Decimal | None = None
    protection_price: Decimal | None = None
    triggered: bool = False
    triggered_at: datetime | None = None
    trigger_level: Decimal | None = None
    trail_reference: Decimal | None = None
    current_stop: Decimal | None = None
    quote_used: int | None = None
    arrival_price: Decimal | None = None
    filled_quantity: Decimal = ZERO
    filled_value: Decimal = ZERO
    commission_charged: Decimal = ZERO
    # A take-profit exit: the position's binding protective level. Observations that
    # reached it end the passive window (stop first when the order cannot be established).
    adverse_level: Decimal | None = None
    # A triggered stop under the gap rule (model version 2): the price it may fill no
    # better than, recorded with its trigger (``fill_cap``).
    gap_cap: Decimal | None = None

    @property
    def buys(self) -> bool:
        return self.side.buys

    @property
    def sells_short_or_long(self) -> bool:
        return self.side in (OrderSide.SELL, OrderSide.SELL_SHORT)


@dataclass(frozen=True)
class Costs:
    commission: Decimal
    regulatory_fees: Decimal
    exchange_fees: Decimal

    @property
    def fees(self) -> Decimal:
        return self.regulatory_fees + self.exchange_fees


@dataclass(frozen=True)
class Fill:
    quantity: Decimal
    price: Decimal
    basis: DataBasis
    costs: Costs
    reference_price: Decimal | None
    touch_price: Decimal | None
    spread: Decimal | None
    slippage_bps: Decimal | None
    impact_bps: Decimal
    shortfall_bps: Decimal | None
    liquidity: Decimal | None
    participation: Decimal | None
    gap: bool
    quote_id: int | None
    observations: dict[str, Any]
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class Evaluation:
    outcome: AttemptOutcome
    code: str
    message: str
    fill: Fill | None = None
    triggered_now: bool = False
    trigger: dict[str, Any] | None = None
    trail_reference: Decimal | None = None
    current_stop: Decimal | None = None
    arrival_price: Decimal | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _plain(value: Decimal | None) -> str:
    return "-" if value is None else format(value.normalize(), "f")


# ---------------------------------------------------------------------- costs
def costs(
    model: AssetClassModel,
    order: OrderState,
    quantity: Decimal,
    price: Decimal,
) -> Costs:
    """Commission for the order's cumulative fills (its minimum and cap apply to the order,
    not to each partial fill) minus what was already charged; sell-side fees per fill."""
    cumulative_qty = order.filled_quantity + quantity
    cumulative_notional = order.filled_value + quantity * price
    total = model.commission_per_unit * cumulative_qty + model.commission_rate * cumulative_notional
    total = max(total, model.commission_minimum)
    if model.commission_maximum_rate is not None:
        total = min(total, model.commission_maximum_rate * cumulative_notional)
    commission = max(cost_round(total, order.minor_units) - order.commission_charged, ZERO)
    notional = quantity * price
    regulatory = ZERO
    if order.sells_short_or_long:
        per_unit = model.sell_per_unit_fee * quantity
        if model.sell_per_unit_fee_maximum is not None:
            per_unit = min(per_unit, model.sell_per_unit_fee_maximum)
        regulatory = cost_round(model.sell_regulatory_fee_rate * notional, order.minor_units)
        regulatory += cost_round(per_unit, order.minor_units)
    exchange = cost_round(model.exchange_fee_rate * notional, order.minor_units)
    return Costs(commission, regulatory, exchange)


# ---------------------------------------------------------------------- trailing stops
def trail_stop(order: OrderState, reference: Decimal) -> Decimal:
    """The stop a trailing order sits at for ``reference``, rounded away from the market."""
    if order.trail_amount is not None:
        offset = order.trail_amount
    else:
        assert order.trail_percent is not None  # noqa: S101  # one offset (database CHECK)
        offset = reference * order.trail_percent / 100
    if order.buys:
        return adverse_price(reference + offset, order.tick, buys=True)
    return adverse_price(max(reference - offset, _PRICE_QUANTUM), order.tick, buys=False)


def _favourable(
    order: OrderState, view: MarketView, fresh: Quote | None
) -> tuple[Decimal | None, datetime | None]:
    """The most favourable price observed for a trailing reference and when (sell trailing
    stops trail highs, buy trailing stops trail lows)."""
    points: list[tuple[Decimal, datetime]] = [(t.price, t.event_time) for t in view.trades]
    for b in view.bars:
        points.append((b.low if order.buys else b.high, b.end))
    if fresh is not None:
        points.append((fresh.ask if order.buys else fresh.bid, fresh.event_time))
    if not points:
        return None, None
    best = min(points, key=lambda p: p[0]) if order.buys else max(points, key=lambda p: p[0])
    return best


def _anchor(order: OrderState, view: MarketView, fresh: Quote | None) -> Decimal | None:
    if fresh is not None:
        return fresh.ask if order.buys else fresh.bid
    if view.trades:
        return view.trades[-1].price
    if view.bars:
        return view.bars[-1].close
    return None


# ---------------------------------------------------------------------- triggers
def find_trigger(
    level: Decimal,
    *,
    buys: bool,
    trades: tuple[Trade, ...],
    bars: tuple[Bar, ...],
    quote: Quote | None,
) -> dict[str, Any] | None:
    """The first observation that reaches a stop ``level`` (chronologically: trades and bars
    of the window, then the fresh quote if it is not older than them). A bar that *opens*
    beyond the level is a gap."""
    hits: list[tuple[datetime, int, dict[str, Any]]] = []
    for t in trades:
        if (t.price >= level) if buys else (t.price <= level):
            hits.append(
                (
                    t.event_time,
                    0,
                    {
                        "source": "TRADE",
                        "id": t.id,
                        "at": t.event_time.isoformat(),
                        "observed": str(t.price),
                        "gap": t.price != level,
                    },
                )
            )
            break
    for b in bars:
        reached = b.high >= level if buys else b.low <= level
        if reached:
            opened_beyond = b.open >= level if buys else b.open <= level
            hits.append(
                (
                    b.start,
                    1,
                    {
                        "source": "BAR",
                        "id": b.id,
                        "at": b.start.isoformat(),
                        "observed": str(b.open if opened_beyond else level),
                        "gap": opened_beyond and b.open != level,
                    },
                )
            )
            break
    latest = max([t.event_time for t in trades] + [b.end for b in bars], default=None)
    # The quote is the current market only if it is not older than the newest trade or bar
    # considered: an older quote is superseded by what traded after it.
    if not hits and quote is not None and (latest is None or quote.event_time >= latest):
        touch = quote.ask if buys else quote.bid
        if (touch >= level) if buys else (touch <= level):
            hits.append(
                (
                    quote.event_time,
                    2,
                    {
                        "source": "QUOTE",
                        "id": quote.id,
                        "at": quote.event_time.isoformat(),
                        "observed": str(touch),
                        "gap": touch != level,
                    },
                )
            )
    if not hits:
        return None
    _at, _rank, info = min(hits, key=lambda h: (h[0], h[1]))
    info["level"] = str(level)
    return info


def gap_fill_cap(
    order: OrderState, trigger: dict[str, Any], view: MarketView, model: AssetClassModel
) -> Decimal | None:
    """The gap rule (model version 2, D-217): the price a stop that gapped may fill no
    better than: the first observation beyond the stop adjusted adversely by the slippage;
    for a bar, its open, kept within the bar's range. None when the trigger is no gap."""
    if not trigger.get("gap"):
        return None
    observed = Decimal(str(trigger["observed"]))
    slip = model.slippage_bps / BPS
    raw = observed * (ONE + slip) if order.buys else observed * (ONE - slip)
    cap = adverse_price(raw, order.tick, buys=order.buys)
    if trigger.get("source") == "BAR":
        bar = next((b for b in view.bars if b.id == trigger.get("id")), None)
        if bar is not None:
            cap = min(cap, bar.high) if order.buys else max(cap, bar.low)
    return cap


# ---------------------------------------------------------------------- evaluation
def _fresh(view: MarketView, model: AssetClassModel) -> Quote | None:
    q = view.quote
    if q is None or q.event_time < view.floor:
        return None  # none, or displayed before the order could act on it (latency)
    if view.as_of - q.event_time > timedelta(seconds=model.quote_max_age_seconds):
        return None
    return q


def _bps(price: Decimal, reference: Decimal | None, *, buys: bool) -> Decimal | None:
    if reference is None or reference <= 0:
        return None
    raw = (price - reference) / reference * BPS
    return (raw if buys else -raw).quantize(Decimal("0.000001"))


def _deferred(code: str, message: str, **kw: Any) -> Evaluation:
    return Evaluation(AttemptOutcome.DEFERRED, code, message, **kw)


def _no_fill(code: str, message: str, **kw: Any) -> Evaluation:
    return Evaluation(AttemptOutcome.NO_FILL, code, message, **kw)


@dataclass
class _Ctx:
    order: OrderState
    view: MarketView
    model: AssetClassModel
    fresh: Quote | None
    keep: dict[str, Any]  # trail / trigger / arrival state to carry into the evaluation


def _size_fill(c: _Ctx, liquidity: Decimal, share: Decimal) -> tuple[Decimal, Decimal] | Evaluation:
    """(quantity, participation) for ``share`` of ``liquidity``, or a NO_FILL."""
    order = c.order
    take = min(order.max_fill, liquidity * share)
    quantity = floor_to(take, order.quantity_increment)
    if quantity <= 0 or (quantity < order.min_quantity and quantity < order.max_fill):
        return _no_fill(
            "INSUFFICIENT_LIQUIDITY",
            f"{_plain(liquidity)} available x {_plain(share)} participation is below one lot "
            f"({_plain(order.quantity_increment)}, minimum {_plain(order.min_quantity)})",
            **c.keep,
        )
    return quantity, min(quantity / liquidity, ONE)


def _finish(
    c: _Ctx,
    *,
    quantity: Decimal,
    price: Decimal,
    basis: DataBasis,
    reference: Decimal | None,
    touch: Decimal | None,
    spread: Decimal | None,
    impact_bps: Decimal,
    liquidity: Decimal | None,
    participation: Decimal | None,
    gap: bool,
    quote_id: int | None,
    observations: dict[str, Any],
    assumptions: tuple[str, ...],
) -> Evaluation:
    order = c.order
    if order.protection_price is not None and order.purpose is OrderPurpose.ENTRY:
        beyond = price > order.protection_price if order.buys else price < order.protection_price
        if beyond:
            return _no_fill(
                "PROTECTION_EXCEEDED",
                f"the simulated price {_plain(price)} is beyond the reserved worst entry price "
                f"{_plain(order.protection_price)}: the entry waits",
                **c.keep,
            )
    fill = Fill(
        quantity=quantity,
        price=price,
        basis=basis,
        costs=costs(c.model, order, quantity, price),
        reference_price=reference,
        touch_price=touch,
        spread=spread,
        slippage_bps=_bps(price, reference, buys=order.buys),
        impact_bps=impact_bps.quantize(Decimal("0.000001")),
        shortfall_bps=_bps(price, c.keep.get("arrival_price"), buys=order.buys),
        liquidity=liquidity,
        participation=None if participation is None else participation.quantize(Decimal("1e-10")),
        gap=gap,
        quote_id=quote_id,
        observations=observations,
        assumptions=assumptions,
    )
    done = quantity == order.remaining
    outcome = AttemptOutcome.FILLED if done else AttemptOutcome.PARTIALLY_FILLED
    return Evaluation(
        outcome,
        basis.value,
        f"{_plain(quantity)} filled at {_plain(price)} ({basis.value}"
        + (", gap through the stop" if gap else "")
        + ")",
        fill=fill,
        **c.keep,
    )


def _marketable(
    c: _Ctx, *, limit: Decimal | None, cap: Decimal | None, gap: bool
) -> Evaluation | None:
    """Execute at the fresh unused quote's touch (None: no such quote)."""
    order, q = c.order, c.fresh
    if q is None or q.id == order.quote_used:
        return None
    touch = q.ask if order.buys else q.bid
    if limit is not None and ((touch > limit) if order.buys else (touch < limit)):
        return None
    size = q.ask_size if order.buys else q.bid_size
    if size is None or size <= 0:
        return _deferred(
            "LIQUIDITY_UNKNOWN",
            "the quote has no displayed size: liquidity is not measurable, nothing is assumed",
            **c.keep,
        )
    sized = _size_fill(c, size, c.model.touch_participation)
    if isinstance(sized, Evaluation):
        return sized
    quantity, participation = sized
    impact = c.model.impact_bps_at_full_participation * participation
    adjust = (c.model.slippage_bps + impact) / BPS
    raw = touch * (ONE + adjust) if order.buys else touch * (ONE - adjust)
    price = adverse_price(raw, order.tick, buys=order.buys)
    notes = ["touch of a fresh quote", "slippage and linear impact applied"]
    if limit is not None:
        price = min(price, limit) if order.buys else max(price, limit)
    if cap is not None:
        price = max(price, cap) if order.buys else min(price, cap)
        notes.append("a triggered stop fills no better than its stop price")
    return _finish(
        c,
        quantity=quantity,
        price=price,
        basis=DataBasis.QUOTE,
        reference=q.mid,
        touch=touch,
        spread=q.spread,
        impact_bps=impact,
        liquidity=size,
        participation=participation,
        gap=gap,
        quote_id=q.id,
        observations={"quote": q.ref()},
        assumptions=tuple(notes),
    )


def _bar_fallback(c: _Ctx, *, cap: Decimal | None, gap: bool) -> Evaluation | None:
    """No fresh quote: the newest completed fresh bar's close widened by the assumed spread."""
    order, model, view = c.order, c.model, c.view
    if not model.allow_bar_fallback or not view.bars:
        return None
    bar = view.bars[-1]
    if view.as_of - bar.end > timedelta(seconds=model.bar_max_age_seconds):
        return None
    if bar.volume is None or bar.volume <= 0:
        return _deferred(
            "VOLUME_UNKNOWN",
            f"bar {bar.id} has no volume: liquidity is not measurable, nothing is assumed",
            **c.keep,
        )
    sized = _size_fill(c, bar.volume, model.bar_participation)
    if isinstance(sized, Evaluation):
        return sized
    quantity, participation = sized
    half = model.assumed_half_spread_bps / BPS
    touch = bar.close * (ONE + half) if order.buys else bar.close * (ONE - half)
    impact = model.impact_bps_at_full_participation * participation
    adjust = (model.slippage_bps + impact) / BPS
    raw = touch * (ONE + adjust) if order.buys else touch * (ONE - adjust)
    price = adverse_price(raw, order.tick, buys=order.buys)
    # Never outside the range the bar actually traded.
    price = _inside(price, bar.low, bar.high, order.tick, buys=order.buys)
    if cap is not None:
        price = max(price, cap) if order.buys else min(price, cap)
    return _finish(
        c,
        quantity=quantity,
        price=price,
        basis=DataBasis.BAR_FALLBACK,
        reference=bar.close,
        touch=None,
        spread=None,
        impact_bps=impact,
        liquidity=bar.volume,
        participation=participation,
        gap=gap,
        quote_id=None,
        observations={
            "bar": {
                "table": "market_bars",
                "id": bar.id,
                "start": bar.start.isoformat(),
                "close": str(bar.close),
                "volume": str(bar.volume),
            }
        },
        assumptions=(
            "no fresh quote: bar close plus an ASSUMED half spread (not an observed quote)",
            "slippage and linear impact applied; clamped to the bar's range",
        ),
    )


def _passive(c: _Ctx, limit: Decimal, after: datetime | None) -> Evaluation | None:
    """A resting limit: only trades (else bars) strictly through the limit, at the limit."""
    order, model, view = c.order, c.model, c.view
    trades = [t for t in view.trades if after is None or t.event_time > after]
    bars = [b for b in view.bars if after is None or b.start >= after]
    if order.adverse_level is not None:
        # Stop first: a take-profit is not filled by activity at or after the first
        # observation that reached the position's stop (a bar that reached both counts as
        # the stop: its intrabar order is unknown).
        level = order.adverse_level
        # The exit sells a long (stop below: reached by lows) or buys a short (stop above).
        hit_trade = next(
            (
                i
                for i, t in enumerate(trades)
                if (t.price >= level if order.buys else t.price <= level)
            ),
            None,
        )
        if hit_trade is not None:
            trades = trades[:hit_trade]
        hit_bar = next(
            (i for i, b in enumerate(bars) if (b.high >= level if order.buys else b.low <= level)),
            None,
        )
        if hit_bar is not None:
            bars = bars[:hit_bar]
        if (hit_trade is not None or hit_bar is not None) and not trades and not bars:
            return _no_fill(
                "STOP_REACHED_FIRST",
                f"the position's stop {_plain(level)} was reached first (or in the same bar): "
                "the take-profit is not filled from that activity",
                **c.keep,
            )
    if trades:
        through = [t for t in trades if (t.price < limit if order.buys else t.price > limit)]
        volume = sum((t.size for t in through if t.size), ZERO)
        if volume <= 0:
            return _no_fill(
                "LIMIT_NOT_THROUGH",
                f"no trade strictly through the limit {_plain(limit)} "
                f"({len(trades)} trades observed)",
                **c.keep,
            )
        sized = _size_fill(c, volume, model.trade_participation)
        if isinstance(sized, Evaluation):
            return sized
        quantity, participation = sized
        return _finish(
            c,
            quantity=quantity,
            price=limit,
            basis=DataBasis.TRADE_THROUGH,
            reference=None if c.fresh is None else c.fresh.mid,
            touch=None,
            spread=None,
            impact_bps=ZERO,
            liquidity=volume,
            participation=participation,
            gap=False,
            quote_id=None,
            observations={
                "trades": {
                    "table": "market_trades",
                    "ids": [through[0].id, through[-1].id],
                    "count": len(through),
                    "volume": str(volume),
                }
            },
            assumptions=(
                "resting limit: filled only by volume printed strictly through the limit",
                "queue position unknown: a participation share of that volume, at the limit",
            ),
        )
    if bars:
        through_bars = [b for b in bars if (b.low < limit if order.buys else b.high > limit)]
        if not through_bars:
            return _no_fill(
                "LIMIT_NOT_THROUGH",
                f"no completed bar traded strictly through the limit {_plain(limit)}",
                **c.keep,
            )
        if any(b.volume is None for b in through_bars):
            return _deferred(
                "VOLUME_UNKNOWN",
                "a bar through the limit has no volume: nothing is assumed",
                **c.keep,
            )
        volume = sum((b.volume or ZERO for b in through_bars), ZERO)
        if volume <= 0:
            return _no_fill(
                "INSUFFICIENT_LIQUIDITY", "no volume traded through the limit", **c.keep
            )
        sized = _size_fill(c, volume, model.bar_participation)
        if isinstance(sized, Evaluation):
            return sized
        quantity, participation = sized
        return _finish(
            c,
            quantity=quantity,
            price=limit,
            basis=DataBasis.BAR_THROUGH,
            reference=None,
            touch=None,
            spread=None,
            impact_bps=ZERO,
            liquidity=volume,
            participation=participation,
            gap=False,
            quote_id=None,
            observations={
                "bars": {
                    "table": "market_bars",
                    "ids": [b.id for b in through_bars][:20],
                    "volume": str(volume),
                }
            },
            assumptions=(
                "resting limit, no trade data: bars whose range went strictly through the "
                "limit; intrabar order unknown, a participation share of the bar volume",
            ),
        )
    return None


def evaluate(
    order: OrderState, view: MarketView, model: AssetClassModel, *, market_open: bool
) -> Evaluation:
    """Decide what one working order does with the data observable at ``view.as_of``."""
    if not model.supported:
        return _deferred("UNSUPPORTED_ASSET_CLASS", "the execution model does not cover this asset")
    fresh = _fresh(view, model)
    arrival = order.arrival_price
    if arrival is None and fresh is not None:
        arrival = fresh.mid
    keep: dict[str, Any] = {
        "trail_reference": order.trail_reference,
        "current_stop": order.current_stop,
        "arrival_price": arrival,
    }
    if not market_open:
        return _deferred("MARKET_CLOSED", "the venue's session is closed", **keep)

    triggered, level = order.triggered, order.trigger_level
    trigger: dict[str, Any] | None = None
    triggered_now = False
    reference: Decimal | None = order.trail_reference
    stop: Decimal | None = order.current_stop
    if order.order_type in STOP_TYPES and not triggered:
        if order.order_type is OrderType.TRAILING_STOP:
            if reference is None:
                anchor = _anchor(order, view, fresh)
                if anchor is None:
                    return _deferred(
                        "NO_DATA",
                        "no observation after arrival to anchor the trailing stop",
                        **keep,
                    )
                reference, stop = anchor, trail_stop(order, anchor)
            assert stop is not None  # noqa: S101  # set with the reference
            level = stop
        else:
            level = order.stop_price
        assert level is not None  # noqa: S101  # database CHECK: stops carry a stop price
        trigger = find_trigger(
            level, buys=order.buys, trades=view.trades, bars=view.bars, quote=fresh
        )
        if trigger is None and order.order_type is OrderType.TRAILING_STOP:
            best, best_at = _favourable(order, view, fresh)
            if best is not None and (
                reference is None or (best < reference if order.buys else best > reference)
            ):
                reference = best
                new_stop = trail_stop(order, best)
                assert stop is not None  # noqa: S101  # anchored above
                stop = min(stop, new_stop) if order.buys else max(stop, new_stop)
                level = stop
                # The newest quote may reach the ratcheted stop if it is not older than
                # the extreme that moved it.
                if fresh is not None and best_at is not None and fresh.event_time >= best_at:
                    trigger = find_trigger(level, buys=order.buys, trades=(), bars=(), quote=fresh)
            keep["trail_reference"], keep["current_stop"] = reference, stop
        elif order.order_type is OrderType.TRAILING_STOP:
            keep["trail_reference"], keep["current_stop"] = reference, stop
        if trigger is None:
            return _no_fill(
                "STOP_NOT_TRIGGERED", f"the stop {_plain(level)} was not reached", **keep
            )
        triggered, triggered_now = True, True
    keep["triggered_now"] = triggered_now
    keep["trigger"] = trigger
    gap = bool(trigger and trigger.get("gap"))

    result: Evaluation | None
    if order.order_type is OrderType.MARKET or (
        order.order_type in (OrderType.STOP, OrderType.TRAILING_STOP) and triggered
    ):
        cap = level if order.order_type is not OrderType.MARKET else None
        gap_cap: Decimal | None = None
        if model.gap_fill_at_observed:
            if cap is not None:  # a broker-resident stop
                if triggered_now and gap and trigger is not None:
                    gap_cap = gap_fill_cap(order, trigger, view, model)
                    if gap_cap is not None:
                        trigger["fill_cap"] = str(gap_cap)  # recorded with the trigger event
                else:
                    gap_cap = order.gap_cap
                if gap_cap is not None:
                    cap = max(cap, gap_cap) if order.buys else min(cap, gap_cap)
            elif order.purpose is OrderPurpose.EXIT and order.gap_cap is not None:
                # A protective MARKET exit the position monitor submitted after a stop it
                # watches was crossed by a gap: no better than the gap observation either.
                gap_cap = cap = order.gap_cap
        c = _Ctx(order, view, model, fresh, keep)
        result = _marketable(c, limit=None, cap=cap, gap=gap) or _bar_fallback(c, cap=cap, gap=gap)
        if gap_cap is not None and result is not None and result.fill is not None:
            result = replace(
                result,
                fill=replace(
                    result.fill,
                    gap=True,
                    assumptions=(
                        *result.fill.assumptions,
                        f"gap rule: no better than {_plain(gap_cap)}, the first observed price "
                        "beyond the stop adjusted by the slippage",
                    ),
                ),
            )
        if result is None:
            if fresh is not None and fresh.id == order.quote_used:
                result = _no_fill(
                    "WAITING_FOR_QUOTE",
                    f"quote {fresh.id}'s displayed size was already used: waiting for a new one",
                    **keep,
                )
            elif view.quote is not None and view.quote.event_time < view.floor:
                result = _no_fill(
                    "WAITING_FOR_QUOTE",
                    "no quote observed since the order reached the simulator (execution "
                    "delay): the quote displayed at arrival is not executable",
                    **keep,
                )
            elif view.quote is not None:
                result = _deferred(
                    "STALE_QUOTE",
                    f"the newest quote is {view.quote_age()} old (limit "
                    f"{model.quote_max_age_seconds}s) and no usable bar: no execution at a "
                    "stale price",
                    **keep,
                )
            else:
                result = _deferred(
                    "NO_QUOTE", "no quote and no usable bar: nothing to execute against", **keep
                )
    else:
        limit = order.limit_price
        assert limit is not None  # noqa: S101  # database CHECK: limit types carry a limit
        c = _Ctx(order, view, model, fresh, keep)
        result = _marketable(c, limit=limit, cap=None, gap=False)
        if result is None:
            after = order.triggered_at if order.order_type is OrderType.STOP_LIMIT else None
            if triggered_now and trigger is not None:
                after = datetime.fromisoformat(trigger["at"])
            result = _passive(c, limit, after)
        if result is None:
            if fresh is not None:
                result = _no_fill(
                    "LIMIT_NOT_MARKETABLE",
                    f"the touch is beyond the limit {_plain(limit)} and nothing traded through it",
                    **keep,
                )
            else:
                result = _deferred(
                    "NO_FRESH_DATA",
                    "no fresh quote and no trade or bar after arrival: nothing is decided",
                    **keep,
                )
    if triggered_now and result.outcome in (AttemptOutcome.NO_FILL, AttemptOutcome.DEFERRED):
        return Evaluation(
            AttemptOutcome.TRIGGERED,
            result.code,
            f"stop {_plain(level)} triggered ({trigger['source'] if trigger else '?'}); "
            f"not executed yet: {result.message}",
            triggered_now=True,
            trigger=trigger,
            trail_reference=result.trail_reference,
            current_stop=result.current_stop,
            arrival_price=result.arrival_price,
            details={"execution": result.code},
        )
    return result


__all__ = [
    "Costs",
    "Evaluation",
    "Fill",
    "OrderState",
    "adverse_price",
    "ceil_to",
    "cost_round",
    "costs",
    "evaluate",
    "find_trigger",
    "floor_to",
    "trail_stop",
]
