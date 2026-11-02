"""Point-in-time feature sets (Phase 3B, spec section 10).

A feature set describes one instrument on one timeframe **at one decision time**, computed
from the completed bars Atlas had then (the caller selects them, see
``app.market_intelligence.data``). It is a pure function of those bars, so recomputing it
from the same inputs always gives the same values.

Design rules:

* **Fixed windows.** Values use exactly the newest ``window`` bars, never the whole stored
  history: adding older history later cannot change a result.
* **Scale-free values.** Moving averages, ATR and drawdowns are stored relative to price
  (ratios, percentages). Split-adjusted history is re-scaled by the provider after each
  split; ratios are unaffected, so a value stays reproducible after re-adjustment.
* **Missing data stays missing.** Bars are the observed ones; absent sessions are counted
  (``missing``) and lower ``coverage``. Below ``min_coverage`` the set is
  ``INSUFFICIENT_DATA`` and carries no values. Nothing is interpolated or zero-filled.

``FEATURE_SET_VERSION`` changes whenever a definition below changes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.feature_engine import indicators as ind

FEATURE_SET_VERSION = "features-v1"


class FeatureQuality(StrEnum):
    OK = "OK"  # every expected bar of the window is present
    PARTIAL = "PARTIAL"  # some bars missing, coverage still >= min_coverage
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # too few bars: no values


class TimeframeSpec(BaseModel):
    """Windows (in bars of this timeframe) for one timeframe's feature set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeframe: str = Field(pattern="^(1Day|1Week)$")
    periods_per_year: int = Field(ge=1)
    fast_ma: int = Field(ge=2)
    slow_ma: int = Field(ge=3)
    adx: int = Field(ge=2)
    roc: int = Field(ge=1)
    relative_strength: int = Field(ge=1)
    atr: int = Field(ge=2)
    atr_short: int = Field(ge=1)
    volatility: int = Field(ge=2)
    # Rolling-volatility observations the current volatility is ranked against.
    volatility_history: int = Field(ge=10)
    bollinger: int = Field(ge=2)
    structure_bars: int = Field(ge=4)
    structure_segments: int = Field(ge=2)
    # Bars between the current set and the earlier one used for change detection.
    change_lookback: int = Field(ge=1)
    min_coverage: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> TimeframeSpec:
        if self.fast_ma >= self.slow_ma:
            raise ValueError("fast_ma must be shorter than slow_ma")
        if self.atr_short >= self.atr:
            raise ValueError("atr_short must be shorter than atr")
        return self

    @property
    def window(self) -> int:
        """Bars the newest value needs (the longest look-back of any feature)."""
        return max(
            self.slow_ma,
            2 * self.adx,
            self.roc + 1,
            self.relative_strength + 1,
            self.atr + 1,
            self.volatility_history + self.volatility,
            self.bollinger,
            self.structure_bars,
        )


@dataclass(frozen=True)
class Bar:
    """One completed bar (floats for analytics; the stored bar is Decimal)."""

    event_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    available_at: datetime


@dataclass(frozen=True)
class FeatureSet:
    timeframe: str
    as_of: datetime
    quality: FeatureQuality
    values: dict[str, float | None]
    bars_used: int
    expected_bars: int
    missing_bars: int
    first_event_at: datetime | None
    last_event_at: datetime | None
    # Newest ``available_at`` among the bars used: the earliest time the set was knowable.
    inputs_available_at: datetime | None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def coverage(self) -> float:
        return 0.0 if self.expected_bars == 0 else self.bars_used / self.expected_bars

    def get(self, name: str) -> float | None:
        return self.values.get(name)


def compute_features(
    bars: Sequence[Bar],
    spec: TimeframeSpec,
    *,
    as_of: datetime,
    expected_bars: int,
    benchmark: Mapping[datetime, float] | None = None,
) -> FeatureSet:
    """Features at ``as_of`` from ``bars`` (oldest first, completed, all known at ``as_of``).

    ``expected_bars`` is how many bars the calendar says the window should have (the
    window length, unless the instrument is newer). ``benchmark`` maps event times to
    benchmark closes of the same timeframe for relative strength.
    """
    window = list(bars[-spec.window :])
    expected = max(expected_bars, len(window))
    missing = expected - len(window)

    def result(
        quality: FeatureQuality, values: dict[str, float | None], notes: tuple[str, ...]
    ) -> FeatureSet:
        return FeatureSet(
            timeframe=spec.timeframe,
            as_of=as_of,
            quality=quality,
            values=values,
            bars_used=len(window),
            expected_bars=expected,
            missing_bars=missing,
            first_event_at=window[0].event_time if window else None,
            last_event_at=window[-1].event_time if window else None,
            inputs_available_at=max((b.available_at for b in window), default=None),
            notes=notes,
        )

    coverage = 0.0 if expected == 0 else len(window) / expected
    if len(window) < spec.window * spec.min_coverage or coverage < spec.min_coverage:
        needed = math.ceil(spec.window * spec.min_coverage)
        return result(
            FeatureQuality.INSUFFICIENT_DATA,
            {},
            (f"{len(window)} of {expected} expected bars; at least {needed} needed",),
        )
    if missing == 0:
        return result(FeatureQuality.OK, _values(window, spec, benchmark), ())
    return result(
        FeatureQuality.PARTIAL,
        _values(window, spec, benchmark),
        (f"{missing} expected bars missing from the window",),
    )


def _last(values: Sequence[float | None]) -> float | None:
    return values[-1] if values else None


def _ratio(value: float | None, reference: float | None) -> float | None:
    if value is None or reference is None or reference == 0:
        return None
    return value / reference - 1.0


def _values(
    window: Sequence[Bar], spec: TimeframeSpec, benchmark: Mapping[datetime, float] | None
) -> dict[str, float | None]:
    close = [b.close for b in window]
    high = [b.high for b in window]
    low = [b.low for b in window]
    last = close[-1]
    sma_fast, sma_slow = _last(ind.sma(close, spec.fast_ma)), _last(ind.sma(close, spec.slow_ma))
    ema_fast, ema_slow = _last(ind.ema(close, spec.fast_ma)), _last(ind.ema(close, spec.slow_ma))
    plus_di, minus_di, adx = ind.directional_movement(high, low, close, spec.adx)
    atr = _last(ind.atr(high, low, close, spec.atr))
    atr_short = _last(ind.atr(high, low, close, spec.atr_short))
    rolling = ind.rolling_volatility(close, spec.volatility, spec.periods_per_year)
    vol_now = rolling[-1]
    vol_history = [v for v in rolling[-spec.volatility_history :] if v is not None]
    widths = ind.bollinger_width(close, spec.bollinger)
    width_history = [w for w in widths[-spec.volatility_history :] if w is not None]
    slope = ind.log_slope(close[-spec.slow_ma :])
    structure_high, structure_low = high[-spec.structure_bars :], low[-spec.structure_bars :]
    prior_high = max(high[-spec.slow_ma - 1 : -1]) if len(high) > spec.slow_ma else None
    prior_low = min(low[-spec.slow_ma - 1 : -1]) if len(low) > spec.slow_ma else None
    window_high = max(high[-spec.slow_ma :])
    volumes = [b.volume for b in window[-spec.fast_ma :]]
    recent_volumes = volumes[-max(1, spec.fast_ma // 4) :]

    values: dict[str, float | None] = {
        # Trend: price relative to its averages, average alignment, slope, DMI, structure.
        "close_vs_sma_fast": _ratio(last, sma_fast),
        "close_vs_sma_slow": _ratio(last, sma_slow),
        "sma_fast_vs_slow": _ratio(sma_fast, sma_slow),
        "ema_fast_vs_slow": _ratio(ema_fast, ema_slow),
        "slow_slope_per_bar": None if slope is None else slope[0],
        "slow_slope_t": None if slope is None else _finite(slope[1]),
        "adx": _last(adx),
        "plus_di": _last(plus_di),
        "minus_di": _last(minus_di),
        "structure_score": ind.swing_structure(
            structure_high, structure_low, spec.structure_segments
        ),
        # Momentum.
        "roc": _last(ind.roc(close, spec.roc)),
        "return_1": _last(ind.roc(close, 1)),
        "relative_strength": _relative_strength(window, spec.relative_strength, benchmark),
        "drawdown_from_high": _ratio(last, window_high),
        "new_high": None if prior_high is None else float(last > prior_high),
        "new_low": None if prior_low is None else float(last < prior_low),
        # Volatility: all relative to price or to the instrument's own history.
        "atr_pct": None if atr is None or last <= 0 else atr / last,
        "range_ratio": None if atr is None or atr_short is None or atr == 0 else atr_short / atr,
        "realized_volatility": vol_now,
        "volatility_percentile": None
        if vol_now is None
        else ind.percentile_rank(vol_history, vol_now),
        "bollinger_width": widths[-1],
        "bollinger_width_percentile": None
        if widths[-1] is None
        else ind.percentile_rank(width_history, widths[-1]),
        # Participation (single-venue volume on IEX: relative values only).
        "volume_ratio": _volume_ratio(recent_volumes, volumes),
    }
    return values


def _finite(value: float) -> float:
    # A perfect fit has an infinite t-statistic; cap it so it can be stored and compared.
    return max(-1e6, min(1e6, value))


def _relative_strength(
    window: Sequence[Bar], n: int, benchmark: Mapping[datetime, float] | None
) -> float | None:
    """``(1 + r_asset) / (1 + r_benchmark) - 1`` over the last ``n`` bars, on the same
    event times; ``None`` if the benchmark lacks either bar."""
    if benchmark is None or len(window) <= n:
        return None
    start, end = window[-n - 1], window[-1]
    bench_start, bench_end = benchmark.get(start.event_time), benchmark.get(end.event_time)
    if not bench_start or bench_end is None or start.close <= 0:
        return None
    return (end.close / start.close) / (bench_end / bench_start) - 1.0


def _volume_ratio(recent: Sequence[float | None], window: Sequence[float | None]) -> float | None:
    if any(v is None for v in window) or not recent:
        return None  # missing volume is never treated as zero
    total = [v for v in window if v is not None]
    average = sum(total) / len(total) if total else 0.0
    if average <= 0:
        return None
    return sum(v for v in recent if v is not None) / len(recent) / average


def weekly_bars(
    daily: Sequence[Bar], session_days: Sequence[date], complete_weeks: set[tuple[int, int]]
) -> list[Bar]:
    """Aggregate daily bars into ISO-week bars, keeping only ``complete_weeks``.

    ``session_days`` gives each daily bar's session date (same order). A week's bar opens
    with its first session's open, closes with its last close, spans the extreme high/low,
    and is available when its last daily bar was. Volume is summed only if every bar has
    it. A week missing some daily bars is still built from those present (the missing
    days lower the window's coverage upstream).
    """
    if len(daily) != len(session_days):
        raise ValueError("one session date per daily bar")
    grouped: dict[tuple[int, int], list[Bar]] = {}
    for bar, day in zip(daily, session_days, strict=True):
        iso = day.isocalendar()
        grouped.setdefault((iso.year, iso.week), []).append(bar)
    weeks: list[Bar] = []
    for key in sorted(grouped):
        if key not in complete_weeks:
            continue
        group = grouped[key]
        volumes = [b.volume for b in group if b.volume is not None]
        weeks.append(
            Bar(
                event_time=group[0].event_time,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum(volumes) if len(volumes) == len(group) else None,
                available_at=max(b.available_at for b in group),
            )
        )
    return weeks


__all__ = [
    "FEATURE_SET_VERSION",
    "Bar",
    "FeatureQuality",
    "FeatureSet",
    "TimeframeSpec",
    "compute_features",
    "weekly_bars",
]
