"""Protection decisions for simulated positions (Phase 7B): pure, deterministic functions.

A position's protection is a set of levels, not resting broker orders: the approved stop
(only ever tightened), an optional trailing stop, the strategy's invalidation level, an
optional expiry, and the target. The position monitor evaluates the *adverse* levels against
the observations since its last check (:func:`decide`), and exits with one market order when
one is reached. The target is worked by a resting TAKE_PROFIT limit order at the paper
broker, so a take-profit fills only by the limit rules (strictly through, never assumed).

One-cancels-other: the stop side and the target can never both close the same quantity. Exit
quantities are bounded by what the entry filled minus what other exits filled or may still
fill (``closable_quantity``); when an adverse level is reached the resting take-profit is
cancelled first and the protective exit is placed only once that cancellation is confirmed
(or the take-profit filled), never alongside it.

Simultaneous triggers: within one evaluation the adverse levels are checked first. When a
bar (or the set of trades since the last check) reached both the stop side and the target
and nothing finer establishes which came first, the stop is assumed first (conservative):
the resting take-profit is cancelled and the position is exited as stopped out. The bar's
path between high and low is never invented.

Priority when several adverse levels are reached: the binding level (the highest for a long,
the lowest for a short) decides; on a tie STOP_LOSS, then TRAILING_STOP, then INVALIDATION.
Then EXPIRATION (time), then an operator CLOSE_REQUEST.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.execution.enums import ExitReason
from app.paper_broker.fill_model import adverse_price, find_trigger
from app.paper_broker.market import MarketView, Quote
from app.risk.enums import TradeDirection

_ORDER = (ExitReason.STOP_LOSS, ExitReason.TRAILING_STOP, ExitReason.INVALIDATION)


@dataclass(frozen=True)
class Levels:
    direction: TradeDirection
    stop: Decimal
    tick: Decimal | None
    trailing_stop: Decimal | None = None
    invalidation: Decimal | None = None
    trail_amount: Decimal | None = None
    trail_percent: Decimal | None = None
    trail_reference: Decimal | None = None


@dataclass(frozen=True)
class Decision:
    reason: ExitReason | None
    level: Decimal | None = None
    trigger: dict[str, Any] | None = None
    trail_reference: Decimal | None = None
    trailing_stop: Decimal | None = None
    details: dict[str, Any] = field(default_factory=dict)


def binding(levels: Levels) -> tuple[Decimal, ExitReason]:
    """The adverse level that is reached first and why it exists."""
    long = levels.direction is TradeDirection.LONG
    candidates = [
        (value, reason)
        for value, reason in (
            (levels.stop, ExitReason.STOP_LOSS),
            (levels.trailing_stop, ExitReason.TRAILING_STOP),
            (levels.invalidation, ExitReason.INVALIDATION),
        )
        if value is not None
    ]
    # Highest for a long (it is reached first as the price falls), lowest for a short;
    # ties resolved in _ORDER order.
    return sorted(candidates, key=lambda c: ((-c[0]) if long else c[0], _ORDER.index(c[1])))[0]


def trailing_level(levels: Levels, reference: Decimal) -> Decimal:
    if levels.trail_amount is not None:
        offset = levels.trail_amount
    else:
        assert levels.trail_percent is not None  # noqa: S101  # caller checks
        offset = reference * levels.trail_percent / 100
    if levels.direction is TradeDirection.LONG:
        return adverse_price(reference - offset, levels.tick, buys=False)
    return adverse_price(reference + offset, levels.tick, buys=True)


def decide(
    levels: Levels,
    view: MarketView,
    fresh: Quote | None,
    *,
    now: datetime,
    expires_at: datetime | None,
    close_requested: bool,
) -> Decision:
    """Which exit, if any, the observations since the last check call for."""
    long = levels.direction is TradeDirection.LONG
    level, why = binding(levels)
    # A long is protected by a sell stop (reached by bids/prices at or below the level); a
    # short by a buy stop (asks/prices at or above it).
    hit = find_trigger(level, buys=not long, trades=view.trades, bars=view.bars, quote=fresh)
    reference, trailing = levels.trail_reference, levels.trailing_stop
    trails = levels.trail_amount is not None or levels.trail_percent is not None
    if hit is None and trails:
        points = [t.price for t in view.trades] + [b.high if long else b.low for b in view.bars]
        if fresh is not None:
            points.append(fresh.bid if long else fresh.ask)
        if points:
            best = max(points) if long else min(points)
            if reference is None or (best > reference if long else best < reference):
                reference = best
                candidate = trailing_level(levels, best)
                if trailing is None:
                    trailing = candidate
                else:
                    trailing = max(trailing, candidate) if long else min(trailing, candidate)
    if hit is not None:
        return Decision(
            why, level, hit, reference, trailing, {"binding": why.value, "level": str(level)}
        )
    if expires_at is not None and now >= expires_at:
        return Decision(
            ExitReason.EXPIRATION,
            None,
            None,
            reference,
            trailing,
            {"expires_at": expires_at.isoformat()},
        )
    if close_requested:
        return Decision(ExitReason.CLOSE_REQUEST, None, None, reference, trailing, {})
    return Decision(None, None, None, reference, trailing, {})


__all__ = ["Decision", "Levels", "binding", "decide", "trailing_level"]
