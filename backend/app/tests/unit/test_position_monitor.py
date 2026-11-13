"""Pure rules of the position monitor (no database)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.monitor import take_profit_limit
from app.risk.enums import TradeDirection

D = Decimal
LONG, SHORT = TradeDirection.LONG, TradeDirection.SHORT


@pytest.mark.parametrize(
    ("target", "tick", "direction", "expected"),
    [
        # A long sells at the first grid price at or above its target.
        ("105.9267", "0.01", LONG, "105.93"),
        ("105.9300", "0.01", LONG, "105.93"),
        ("105.9301", "0.01", LONG, "105.94"),
        ("0.52341", "0.0001", LONG, "0.5235"),
        # A short covers at the first grid price at or below its target.
        ("94.0733", "0.01", SHORT, "94.07"),
        ("94.0700", "0.01", SHORT, "94.07"),
        ("94.0799", "0.01", SHORT, "94.07"),
        # A coarser provider tick.
        ("101.03", "0.05", LONG, "101.05"),
        ("101.03", "0.05", SHORT, "101.00"),
    ],
)
def test_take_profit_limit_is_on_the_grid_at_or_beyond_the_target(
    target: str, tick: str, direction: TradeDirection, expected: str
) -> None:
    limit = take_profit_limit(D(target), D(tick), direction)
    assert limit == D(expected)
    assert limit % D(tick) == 0
    if direction is LONG:
        assert D(0) <= limit - D(target) < D(tick)
    else:
        assert D(0) <= D(target) - limit < D(tick)


def test_take_profit_limit_without_a_known_tick_is_the_target() -> None:
    assert take_profit_limit(D("105.9267"), None, LONG) == D("105.9267")
    assert take_profit_limit(D("105.9267"), D(0), SHORT) == D("105.9267")


def test_a_short_target_below_one_tick_never_becomes_zero() -> None:
    assert take_profit_limit(D("0.004"), D("0.01"), SHORT) == D("0.01")
