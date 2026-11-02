"""Indicator test vectors (Phase 3B).

Expected values are derived analytically from each indicator's definition (not by running
the implementation), so a regression in either the formula or its seeding is caught.
"""

from __future__ import annotations

import math

import pytest

from app.feature_engine import indicators as ind


def approx_list(values, expected, tol=1e-9):
    assert len(values) == len(expected)
    for got, want in zip(values, expected, strict=True):
        if want is None:
            assert got is None
        else:
            assert got == pytest.approx(want, abs=tol)


def test_sma_is_the_trailing_mean():
    approx_list(ind.sma([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])


def test_ema_seeded_with_sma_tracks_a_linear_series_at_a_constant_lag():
    # For x_t = 100 + t, the SMA seed equals x - (n-1)/2, and with alpha = 2/(n+1) the EMA
    # keeps exactly that lag: x_t - (n-1)/2 for every t >= n-1.
    n = 5
    values = [100.0 + t for t in range(30)]
    expected = [None] * (n - 1) + [100.0 + t - (n - 1) / 2 for t in range(n - 1, 30)]
    approx_list(ind.ema(values, n), expected)


def test_wilder_of_a_constant_is_constant_and_needs_contiguous_values():
    approx_list(ind.wilder([None, 2.0, 2.0, 2.0, 2.0], 3), [None, None, None, 2.0, 2.0])
    with pytest.raises(ValueError, match="contiguous"):
        ind.wilder([1.0, None, 1.0, 1.0], 2)


def test_true_range_includes_gaps_from_the_previous_close():
    high, low, close = [10, 12, 9], [9, 11, 8], [9.5, 11.5, 8.5]
    # 1: max(1, |12-9.5|, |11-9.5|) = 2.5 ; 2: max(1, |9-11.5|, |8-11.5|) = 3.5
    assert ind.true_range(high, low, close) == [1, 2.5, 3.5]


def test_atr_of_a_constant_range_is_that_range():
    high = [10.0 + t for t in range(20)]
    low = [h - 2.0 for h in high]
    close = [h - 1.0 for h in high]
    # TR from t=1: max(2, |h - (h-2)| , |h-2 - (h-2)|) = 2 ... h_t - c_{t-1} = 2 -> TR = 2.
    values = ind.atr(high, low, close, 5)
    assert values[3] is None
    assert values[4] == pytest.approx(2.0)
    assert values[-1] == pytest.approx(2.0)


def test_adx_of_a_steady_uptrend_is_100():
    # High t+1, low t, close t+0.5: +DM = 1, -DM = 0, TR = 1.5 every bar, so +DI = 66.67,
    # -DI = 0, DX = 100 and ADX = 100.
    count, n = 40, 14
    high = [t + 1.0 for t in range(count)]
    low = [float(t) for t in range(count)]
    close = [t + 0.5 for t in range(count)]
    plus, minus, adx = ind.directional_movement(high, low, close, n)
    assert plus[n - 1] is None
    assert plus[n] == pytest.approx(100 / 1.5)
    assert minus[n] == pytest.approx(0.0)
    assert adx[2 * n - 1] is not None
    assert adx[2 * n - 2] is None
    assert adx[-1] == pytest.approx(100.0)


def test_adx_of_a_downtrend_is_100_with_minus_di_dominant():
    count, n = 40, 14
    high = [100.0 - t for t in range(count)]
    low = [h - 1.0 for h in high]
    close = [h - 0.5 for h in high]
    plus, minus, adx = ind.directional_movement(high, low, close, n)
    assert plus[-1] == pytest.approx(0.0)
    assert minus[-1] == pytest.approx(100 / 1.5)
    assert adx[-1] == pytest.approx(100.0)


def test_adx_is_undefined_for_flat_prices():
    flat = [10.0] * 40
    plus, _, adx = ind.directional_movement(flat, flat, flat, 14)
    assert all(v is None for v in plus)
    assert all(v is None for v in adx)


def test_rate_of_change():
    approx_list(ind.roc([100, 110, 121, 0, 5], 1), [None, 0.1, 0.1, -1.0, None])


def test_realized_volatility_of_alternating_returns():
    # Closes alternate 100, 101: log returns +r, -r with r = ln(1.01). Over 4 returns the
    # mean is 0 and the sample sd is r * 2 / sqrt(3); annualized with sqrt(252).
    closes = [100.0, 101.0] * 4
    r = math.log(1.01)
    values = ind.rolling_volatility(closes, 4, 252)
    assert values[3] is None
    assert values[4] == pytest.approx(r * 2 / math.sqrt(3) * math.sqrt(252))


def test_bollinger_width_uses_population_standard_deviation():
    # Window [1, 3]: mean 2, population sd 1, k = 2 -> (upper - lower) / middle = 4 / 2.
    approx_list(ind.bollinger_width([1.0, 3.0], 2), [None, 2.0])


def test_percentile_rank_counts_ties_half():
    assert ind.percentile_rank([1, 2, 3, 4], 3) == pytest.approx(0.625)
    assert ind.percentile_rank([1, 2, 3, 4], 0) == 0
    assert ind.percentile_rank([1, 2, 3, 4], 9) == 1
    assert ind.percentile_rank([], 1) is None


def test_log_slope_of_exponential_growth():
    slope, t_stat = ind.log_slope([100 * 1.01**t for t in range(20)])  # type: ignore[misc]
    assert slope == pytest.approx(math.log(1.01))
    assert t_stat == math.inf
    noisy = [100 * 1.01**t * (1.002 if t % 2 else 0.998) for t in range(20)]
    slope2, t2 = ind.log_slope(noisy)  # type: ignore[misc]
    assert slope2 == pytest.approx(math.log(1.01), rel=0.05)
    assert 10 < t2 < math.inf
    assert ind.log_slope([1.0, 2.0]) is None


def test_swing_structure_scores_higher_highs_and_lows():
    rising_high = [float(t) for t in range(12)]
    rising_low = [t - 1.0 for t in range(12)]
    assert ind.swing_structure(rising_high, rising_low, 3) == 1.0
    assert ind.swing_structure(rising_high[::-1], rising_low[::-1], 3) == -1.0
    # Expanding range: higher highs but lower lows -> neither.
    assert ind.swing_structure([1, 2, 3, 4, 5, 6], [6, 5, 4, 3, 2, 1], 3) == 0.0


def test_pearson():
    xs = [1.0, 2.0, 3.0, 4.0]
    assert ind.pearson(xs, [2 * x + 1 for x in xs]) == pytest.approx(1.0)
    assert ind.pearson(xs, [-x for x in xs]) == pytest.approx(-1.0)
    assert ind.pearson(xs, [5.0] * 4) is None  # constant: undefined, never 0
    # Hand-computed: x = 1,2,3 ; y = 1,3,2 -> sxy = 1, sxx = 2, syy = 2 -> 0.5
    assert ind.pearson([1, 2, 3], [1, 3, 2]) == pytest.approx(0.5)


def test_no_value_depends_on_later_inputs():
    """Causality: appending bars never changes earlier outputs."""
    closes = [100 + math.sin(t / 3) * 5 + t * 0.2 for t in range(80)]
    high = [c + 1 for c in closes]
    low = [c - 1 for c in closes]
    full = {
        "ema": ind.ema(closes, 10),
        "atr": ind.atr(high, low, closes, 14),
        "adx": ind.directional_movement(high, low, closes, 14)[2],
        "vol": ind.rolling_volatility(closes, 20, 252),
        "bbw": ind.bollinger_width(closes, 20),
    }
    cut = 50
    short_closes, short_high, short_low = closes[:cut], high[:cut], low[:cut]
    prefix = {
        "ema": ind.ema(short_closes, 10),
        "atr": ind.atr(short_high, short_low, short_closes, 14),
        "adx": ind.directional_movement(short_high, short_low, short_closes, 14)[2],
        "vol": ind.rolling_volatility(short_closes, 20, 252),
        "bbw": ind.bollinger_width(short_closes, 20),
    }
    for name, values in prefix.items():
        approx_list(values, full[name][:cut], tol=1e-12)
