"""DETERMINISTIC TEST FIXTURES for the Phase 5A machine-learning engine.

Every series here is SYNTHETIC: a seeded random walk, not market data and not a provider
response. ``planted_signal`` series contain a deliberately planted, learnable relation
(the sign of the last 3-period return persists slightly into the next periods) so that
tests can check that the pipeline *can* find structure; ``noise`` series contain none, so
tests can check that the pipeline does not *claim* structure that is not there. Neither
says anything about real markets.
"""

from __future__ import annotations

import numpy as np

from app.tests.fixtures.strategy_series import Candle


def random_walk(
    count: int,
    *,
    seed: int,
    start: float = 100.0,
    volatility: float = 0.012,
    persistence: float = 0.0,
) -> list[Candle]:
    """Daily candles. ``persistence`` > 0 adds a planted momentum effect: each period's
    drift is ``persistence`` times the sign of the previous 3-period return."""
    rng = np.random.default_rng(seed)
    closes = [start]
    for i in range(1, count):
        drift = 0.0
        if persistence and i > 3:
            drift = persistence * volatility * float(np.sign(closes[-1] - closes[-4]))
        closes.append(closes[-1] * float(np.exp(drift + volatility * rng.standard_normal())))
    candles = []
    previous = start
    for close in closes:
        gap = float(rng.normal(0, volatility / 4))
        open_ = previous * float(np.exp(gap))
        span = abs(float(rng.normal(0, volatility))) * close
        high = max(open_, close) + span / 2
        low = max(0.01, min(open_, close) - span / 2)
        candles.append(
            Candle(
                open=round(open_, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(close, 4),
                volume=float(1_000_000 + int(rng.integers(0, 200_000))),
            )
        )
        previous = close
    return candles


def planted_signal(count: int, *, seed: int) -> list[Candle]:
    return random_walk(count, seed=seed, persistence=0.6)


def noise(count: int, *, seed: int) -> list[Candle]:
    return random_walk(count, seed=seed, persistence=0.0)


__all__ = ["noise", "planted_signal", "random_walk"]
