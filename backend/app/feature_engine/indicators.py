"""Technical indicators (Phase 3B): pure, causal functions over oldest-first series.

Every function returns a list aligned with its input. Position ``i`` uses inputs ``0..i``
only (nothing looks ahead), and is ``None`` where there is not enough history or the value
is undefined (e.g. a zero denominator). Missing data is never replaced by zero.

Definitions follow their original authors so values can be checked against references:

* SMA: arithmetic mean of the last ``n`` values.
* EMA: ``alpha = 2 / (n + 1)``, seeded with the SMA of the first ``n`` values.
* Wilder smoothing (RMA): seeded with the mean of the first ``n`` values, then
  ``prev + (x - prev) / n`` (Wilder, *New Concepts in Technical Trading Systems*, 1978).
* True range, ATR, +DI, -DI, DX, ADX: Wilder (1978).
* Bollinger Band width: ``(upper - lower) / middle`` with ``k`` population standard
  deviations (Bollinger), i.e. ``2 k sigma / mu``.
* Realized volatility: sample standard deviation (ddof 1) of log returns, annualized with
  the square root of the periods per year.

Floats are used here (analytics); persisted values are rounded ``Decimal`` (spec 3.5).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

Values = Sequence[float]
Optional = list[float | None]


def _check(n: int) -> None:
    if n < 1:
        raise ValueError("window length must be at least 1")


def sma(values: Values, n: int) -> Optional:
    _check(n)
    out: Optional = [None] * len(values)
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: Values, n: int) -> Optional:
    _check(n)
    out: Optional = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1)
    current = sum(values[:n]) / n
    out[n - 1] = current
    for i in range(n, len(values)):
        current += alpha * (values[i] - current)
        out[i] = current
    return out


def wilder(values: Sequence[float | None], n: int) -> Optional:
    """Wilder's moving average over the defined values, which must be contiguous (leading
    ``None`` values are skipped; a ``None`` after the first value is an error)."""
    _check(n)
    out: Optional = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) - start < n:
        return out
    seed = values[start : start + n]
    if any(v is None for v in seed):
        raise ValueError("wilder smoothing needs contiguous values")
    current = sum(v for v in seed if v is not None) / n
    out[start + n - 1] = current
    for i in range(start + n, len(values)):
        value = values[i]
        if value is None:
            raise ValueError("wilder smoothing needs contiguous values")
        current += (value - current) / n
        out[i] = current
    return out


def true_range(high: Values, low: Values, close: Values) -> list[float]:
    _same_length(high, low, close)
    out: list[float] = []
    for i in range(len(high)):
        span = high[i] - low[i]
        if i == 0:
            out.append(span)
        else:
            prev = close[i - 1]
            out.append(max(span, abs(high[i] - prev), abs(low[i] - prev)))
    return out


def atr(high: Values, low: Values, close: Values, n: int) -> Optional:
    return wilder(true_range(high, low, close), n)


def directional_movement(
    high: Values, low: Values, close: Values, n: int
) -> tuple[Optional, Optional, Optional]:
    """(+DI, -DI, ADX), Wilder. The first +DI/-DI is at index ``n``; the first ADX at
    ``2n - 1``. Undefined where the smoothed true range (or DI sum) is zero."""
    _check(n)
    _same_length(high, low, close)
    length = len(high)
    tr: list[float | None] = [None] * length
    plus_dm: list[float | None] = [None] * length
    minus_dm: list[float | None] = [None] * length
    for i in range(1, length):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    smooth_tr, smooth_plus, smooth_minus = wilder(tr, n), wilder(plus_dm, n), wilder(minus_dm, n)
    plus_di: Optional = [None] * length
    minus_di: Optional = [None] * length
    dx: list[float | None] = [None] * length
    for i in range(length):
        s_tr, s_plus, s_minus = smooth_tr[i], smooth_plus[i], smooth_minus[i]
        if s_tr is None or s_plus is None or s_minus is None or s_tr == 0:
            continue
        plus, minus = 100.0 * s_plus / s_tr, 100.0 * s_minus / s_tr
        plus_di[i], minus_di[i] = plus, minus
        if plus + minus > 0:
            dx[i] = 100.0 * abs(plus - minus) / (plus + minus)
    # ADX smooths DX from its first defined value; an undefined DX later (flat prices)
    # leaves the remaining ADX undefined rather than inventing a value.
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    adx: Optional = [None] * length
    if first is not None:
        run_end = first
        while run_end < length and dx[run_end] is not None:
            run_end += 1
        adx[first:run_end] = wilder(dx[first:run_end], n)
    return plus_di, minus_di, adx


def roc(values: Values, n: int) -> Optional:
    """Rate of change: ``values[i] / values[i - n] - 1``."""
    _check(n)
    out: Optional = [None] * len(values)
    for i in range(n, len(values)):
        if values[i - n] != 0:
            out[i] = values[i] / values[i - n] - 1.0
    return out


def log_returns(values: Values) -> Optional:
    out: Optional = [None] * len(values)
    for i in range(1, len(values)):
        if values[i] > 0 and values[i - 1] > 0:
            out[i] = math.log(values[i] / values[i - 1])
    return out


def stdev(values: Values, ddof: int = 1) -> float | None:
    count = len(values)
    if count - ddof <= 0:
        return None
    mean = sum(values) / count
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (count - ddof))


def rolling_volatility(values: Values, n: int, periods_per_year: int) -> Optional:
    """Annualized realized volatility of the last ``n`` log returns, at every position."""
    _check(n)
    returns = log_returns(values)
    out: Optional = [None] * len(values)
    for i in range(n, len(values)):
        window = returns[i - n + 1 : i + 1]
        if any(r is None for r in window):
            continue
        sd = stdev([r for r in window if r is not None])
        if sd is not None:
            out[i] = sd * math.sqrt(periods_per_year)
    return out


def bollinger_width(values: Values, n: int, k: float = 2.0) -> Optional:
    _check(n)
    out: Optional = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1 : i + 1]
        mean = sum(window) / n
        sd = stdev(window, ddof=0)
        if mean > 0 and sd is not None:
            out[i] = 2.0 * k * sd / mean
    return out


def percentile_rank(history: Values, value: float) -> float | None:
    """Mid-rank percentile of ``value`` within ``history`` (0..1); ties count half."""
    if not history:
        return None
    below = sum(1 for h in history if h < value)
    equal = sum(1 for h in history if h == value)
    return (below + 0.5 * equal) / len(history)


def log_slope(values: Values) -> tuple[float, float] | None:
    """OLS slope of ``ln(values)`` per step and its t-statistic.

    The slope is a growth rate per bar (0.001 = +0.1 % per bar). The t-statistic says how
    clearly the line differs from flat given the scatter around it; it assumes independent
    residuals, which price series violate, so it is a relative strength-of-evidence
    measure, not a significance test. ``None`` for fewer than 3 points or non-positive
    values; a perfect fit has an infinite t-statistic, reported as +/-inf.
    """
    count = len(values)
    if count < 3 or any(v <= 0 for v in values):
        return None
    ys = [math.log(v) for v in values]
    mean_x = (count - 1) / 2
    mean_y = sum(ys) / count
    sxx = sum((i - mean_x) ** 2 for i in range(count))
    sxy = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residual = sum((y - (intercept + slope * i)) ** 2 for i, y in enumerate(ys))
    if residual <= 1e-24:
        return slope, math.copysign(math.inf, slope) if slope else 0.0
    standard_error = math.sqrt(residual / (count - 2) / sxx)
    return slope, slope / standard_error


def swing_structure(high: Values, low: Values, segments: int) -> float | None:
    """Higher-highs/higher-lows score in [-1, 1] over ``segments`` equal consecutive blocks.

    For each pair of adjacent blocks: +1 if the later block has both a higher high and a
    higher low, -1 if both are lower, 0 otherwise; the score is the mean over pairs.
    """
    _same_length(high, low)
    if segments < 2:
        raise ValueError("at least two segments are needed")
    size = len(high) // segments
    if size < 1:
        return None
    start = len(high) - size * segments  # drop the oldest remainder, keep the newest bars
    blocks = [
        (
            max(high[start + b * size : start + (b + 1) * size]),
            min(low[start + b * size : start + (b + 1) * size]),
        )
        for b in range(segments)
    ]
    score = 0
    for (prev_high, prev_low), (next_high, next_low) in itertools.pairwise(blocks):
        if next_high > prev_high and next_low > prev_low:
            score += 1
        elif next_high < prev_high and next_low < prev_low:
            score -= 1
    return score / (segments - 1)


def pearson(xs: Values, ys: Values) -> float | None:
    """Pearson correlation of paired observations; ``None`` if either side is constant."""
    _same_length(xs, ys)
    count = len(xs)
    if count < 3:
        return None
    mean_x, mean_y = sum(xs) / count, sum(ys) / count
    sxx = sum((x - mean_x) ** 2 for x in xs)
    syy = sum((y - mean_y) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def _same_length(*series: Sequence[float]) -> None:
    if len({len(s) for s in series}) > 1:
        raise ValueError("series must have the same length")


__all__ = [
    "atr",
    "bollinger_width",
    "directional_movement",
    "ema",
    "log_returns",
    "log_slope",
    "pearson",
    "percentile_rank",
    "roc",
    "rolling_volatility",
    "sma",
    "stdev",
    "swing_structure",
    "true_range",
    "wilder",
]
