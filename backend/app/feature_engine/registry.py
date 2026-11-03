"""Central feature registry (Phase 4A, spec section 10).

Every feature a strategy may use is described here once: name, version, source,
calculation method, parameters, timeframes, required data, availability rule and value
kind. Strategies ask for features with a :class:`FeatureRequest`; the registry validates
the parameters and gives each resolved feature a canonical key such as
``ema.v1(length=20)@1Day``, the identity under which its value is stored in a feature
snapshot.

Calculations are not reimplemented here: bar features call ``app.feature_engine.indicators``
(the Phase 3B functions plus the Phase 4A additions), market-intelligence features read the
stored Phase 3B classifications and news features read the stored Phase 3C events
(``app.feature_engine.stored``). Benchmark features (Phase 4B) combine the asset's
completed bars with a benchmark's bars on the same periods (``app.feature_engine.benchmark``).
Phase 4B added definitions under new names only; no earlier definition changed.

Rules every bar feature follows (``app.feature_engine.bar_features``):

* **Completed bars only.** The caller passes the bars of completed, settled periods known
  at the decision time; the newest period still in progress is never an input.
* **Fixed windows.** A value uses exactly the newest ``window(params)`` periods, never the
  whole stored history, so more history stored later cannot change it. EMA and Wilder
  smoothing are seeded inside that window (window = a multiple of the period, so the seed's
  remaining weight is small and, above all, always the same).
* **Missing stays missing.** A missing period inside the window makes the value
  ``MISSING_INPUTS``; too little history makes it ``INSUFFICIENT_DATA``; nothing is
  interpolated or zero-filled.

A definition's behaviour must not change under the same version: change the version (and
the method text) instead. ``definition_sha256`` covers everything stored about a
definition, and syncing a changed definition under an existing version is refused.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.feature_engine import indicators as ind
from app.market_intelligence.enums import Dimension, Scope
from app.news.enums import EventCategory

FEATURE_REGISTRY_VERSION = "feature-registry-v1"

DAILY, WEEKLY = "1Day", "1Week"
BAR_TIMEFRAMES = (DAILY, WEEKLY)
PERIODS_PER_YEAR = {DAILY: 252, WEEKLY: 52}

ParamValue = int | float | str
Params = Mapping[str, ParamValue]
FeatureScalar = float | str | None


class FeatureSource(StrEnum):
    BARS = "BARS"  # completed price bars (app.feature_engine.bar_features)
    # completed bars of the asset and of a benchmark on the same periods
    # (app.feature_engine.benchmark; Phase 4B)
    BENCHMARK = "BENCHMARK"
    MARKET_INTELLIGENCE = "MARKET_INTELLIGENCE"  # stored Phase 3B classifications
    NEWS = "NEWS"  # stored Phase 3C news events


class FeatureStatus(StrEnum):
    VALID = "VALID"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # not enough history yet
    MISSING_INPUTS = "MISSING_INPUTS"  # a period inside the window has no usable bar
    UNDEFINED = "UNDEFINED"  # mathematically undefined (e.g. zero denominator)
    UNAVAILABLE = "UNAVAILABLE"  # the source does not cover the window or is stale


class ValueKind(StrEnum):
    NUMBER = "NUMBER"
    LABEL = "LABEL"


class DefinitionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"


class FeatureRegistryError(ValueError):
    """An unknown feature, an invalid parameter or a conflicting definition."""


@dataclass(frozen=True)
class Parameter:
    name: str
    kind: type[int] | type[float] | type[str]
    default: ParamValue
    description: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None

    def coerce(self, value: Any) -> ParamValue:
        if self.kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise FeatureRegistryError(f"{self.name} must be an integer")
            result: ParamValue = value
        elif self.kind is float:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise FeatureRegistryError(f"{self.name} must be a number")
            if not math.isfinite(value):
                raise FeatureRegistryError(f"{self.name} must be finite")
            result = float(value)
        else:
            if not isinstance(value, str):
                raise FeatureRegistryError(f"{self.name} must be a string")
            result = value
        if isinstance(result, int | float):
            if self.minimum is not None and result < self.minimum:
                raise FeatureRegistryError(f"{self.name} must be at least {self.minimum:g}")
            if self.maximum is not None and result > self.maximum:
                raise FeatureRegistryError(f"{self.name} must be at most {self.maximum:g}")
        if self.choices is not None and result not in self.choices:
            raise FeatureRegistryError(f"{self.name} must be one of {', '.join(self.choices)}")
        return result

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.kind.__name__,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices) if self.choices else None,
            "description": self.description,
        }


@dataclass(frozen=True)
class BarWindow:
    """The completed bars of one feature's window, oldest first (floats for analytics)."""

    timeframe: str
    open: Sequence[float]
    high: Sequence[float]
    low: Sequence[float]
    close: Sequence[float]
    volume: Sequence[float | None]

    @property
    def periods_per_year(self) -> int:
        return PERIODS_PER_YEAR[self.timeframe]


BarCompute = Callable[[BarWindow, Params], float | None]
# (asset window, benchmark window on the same periods, params) -> value
PairCompute = Callable[[BarWindow, BarWindow, Params], float | None]


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    version: str
    source: FeatureSource
    description: str
    method: str  # the calculation, precise enough to re-implement and check
    parameters: tuple[Parameter, ...]
    timeframes: tuple[str, ...]
    required_data: tuple[str, ...]
    availability: str  # when a value becomes usable
    value_kind: ValueKind = ValueKind.NUMBER
    status: DefinitionStatus = DefinitionStatus.ACTIVE
    # Bar features: periods a value needs (with the rule as text) and the calculation.
    window_rule: str | None = None
    window: Callable[[Params], int] | None = None
    compute: BarCompute | None = None
    check: Callable[[Params], str | None] | None = None  # cross-parameter validation
    # Benchmark features: the calculation over the asset's and the benchmark's windows.
    pair_compute: PairCompute | None = None

    def __post_init__(self) -> None:
        bars = self.source is FeatureSource.BARS
        pair = self.source is FeatureSource.BENCHMARK
        if bars != (self.compute is not None) or pair != (self.pair_compute is not None):
            raise FeatureRegistryError(f"{self.name}: bar features (only) need window/compute")
        if (bars or pair) != (self.window is not None):
            raise FeatureRegistryError(f"{self.name}: bar features (only) need window/compute")

    def resolve_params(self, params: Mapping[str, Any]) -> dict[str, ParamValue]:
        known = {p.name: p for p in self.parameters}
        unknown = sorted(set(params) - set(known))
        if unknown:
            raise FeatureRegistryError(f"{self.name}: unknown parameter(s) {', '.join(unknown)}")
        resolved = {
            name: spec.coerce(params[name]) if name in params else spec.default
            for name, spec in known.items()
        }
        if self.check is not None and (problem := self.check(resolved)) is not None:
            raise FeatureRegistryError(f"{self.name}: {problem}")
        return resolved

    def describe(self) -> dict[str, Any]:
        """Everything stored about the definition (the input of ``definition_sha256``)."""
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source.value,
            "description": self.description,
            "method": self.method,
            "parameters": [p.describe() for p in self.parameters],
            "timeframes": list(self.timeframes),
            "required_data": list(self.required_data),
            "availability": self.availability,
            "value_kind": self.value_kind.value,
            "status": self.status.value,
            "window_rule": self.window_rule,
        }

    @property
    def definition_sha256(self) -> str:
        return sha256_json(self.describe())


@dataclass(frozen=True)
class FeatureRequest:
    """What a strategy asks for. ``optional`` features may be invalid without blocking an
    evaluation; required ones must all be ``VALID``."""

    name: str
    timeframe: str
    params: Mapping[str, Any] = field(default_factory=dict)
    optional: bool = False


@dataclass(frozen=True)
class ResolvedFeature:
    definition: FeatureDefinition
    timeframe: str
    params: tuple[tuple[str, ParamValue], ...]
    optional: bool = False

    @property
    def key(self) -> str:
        args = ",".join(f"{k}={_format(v)}" for k, v in self.params)
        return f"{self.definition.name}.{self.definition.version}({args})@{self.timeframe}"

    @property
    def param_map(self) -> dict[str, ParamValue]:
        return dict(self.params)

    @property
    def window(self) -> int:
        if self.definition.window is None:
            return 0
        return self.definition.window(self.param_map)


@dataclass(frozen=True)
class FeatureValue:
    key: str
    name: str
    version: str
    source: FeatureSource
    timeframe: str
    params: Mapping[str, ParamValue]
    value: FeatureScalar
    status: FeatureStatus
    reason: str | None
    # When the value became knowable: the newest availability among its inputs.
    available_at: datetime | None
    inputs: Mapping[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.status is FeatureStatus.VALID

    def to_json(self) -> dict[str, Any]:
        value: Any = self.value
        if isinstance(value, float):
            value = repr(value)  # exact round trip; floats are never stored as JSON numbers
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source.value,
            "timeframe": self.timeframe,
            "params": dict(self.params),
            "value": value,
            "status": self.status.value,
            "reason": self.reason,
            "available_at": None if self.available_at is None else self.available_at.isoformat(),
            "inputs": dict(self.inputs),
        }

    @classmethod
    def from_json(cls, key: str, data: Mapping[str, Any], kind: ValueKind) -> FeatureValue:
        raw = data.get("value")
        value: FeatureScalar = raw
        if raw is not None and kind is ValueKind.NUMBER:
            value = float(raw)
        available = data.get("available_at")
        return cls(
            key=key,
            name=data["name"],
            version=data["version"],
            source=FeatureSource(data["source"]),
            timeframe=data["timeframe"],
            params=data.get("params", {}),
            value=value,
            status=FeatureStatus(data["status"]),
            reason=data.get("reason"),
            available_at=None if available is None else datetime.fromisoformat(available),
            inputs=data.get("inputs", {}),
        )


class FeatureRegistry:
    def __init__(self, definitions: Iterable[FeatureDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, str], FeatureDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: FeatureDefinition) -> None:
        identity = (definition.name, definition.version)
        if identity in self._definitions:
            raise FeatureRegistryError(f"{definition.name} {definition.version} is registered")
        self._definitions[identity] = definition

    def definitions(self) -> list[FeatureDefinition]:
        return sorted(self._definitions.values(), key=lambda d: (d.source, d.name, d.version))

    def get(self, name: str, version: str | None = None) -> FeatureDefinition:
        if version is not None:
            found = self._definitions.get((name, version))
            if found is None:
                raise FeatureRegistryError(f"unknown feature {name} {version}")
            return found
        active = [
            d
            for (n, _), d in self._definitions.items()
            if n == name and d.status is DefinitionStatus.ACTIVE
        ]
        if not active:
            raise FeatureRegistryError(f"unknown feature {name}")
        return max(active, key=lambda d: _version_order(d.version))

    def resolve(self, request: FeatureRequest, version: str | None = None) -> ResolvedFeature:
        definition = self.get(request.name, version)
        if request.timeframe not in definition.timeframes:
            raise FeatureRegistryError(
                f"{definition.name} does not support timeframe {request.timeframe}"
            )
        params = definition.resolve_params(request.params)
        return ResolvedFeature(
            definition, request.timeframe, tuple(sorted(params.items())), request.optional
        )

    def resolve_all(self, requests: Iterable[FeatureRequest]) -> list[ResolvedFeature]:
        """Resolve and de-duplicate (a feature is required if any request requires it)."""
        by_key: dict[str, ResolvedFeature] = {}
        for request in requests:
            resolved = self.resolve(request)
            existing = by_key.get(resolved.key)
            if existing is None or (existing.optional and not resolved.optional):
                by_key[resolved.key] = resolved
        return [by_key[k] for k in sorted(by_key)]


def sha256_json(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _format(value: ParamValue) -> str:
    return format(value, "g") if isinstance(value, float) else str(value)


def _version_order(version: str) -> tuple[int, str]:
    digits = version.lstrip("v")
    return (int(digits), version) if digits.isdigit() else (0, version)


# --- Bar feature calculations (all delegate to app.feature_engine.indicators) -----------


def _int(params: Params, name: str) -> int:
    value = params[name]
    if not isinstance(value, int):
        raise FeatureRegistryError(f"{name} must be an integer")
    return value


def _float(params: Params, name: str) -> float:
    value = params[name]
    if not isinstance(value, int | float):
        raise FeatureRegistryError(f"{name} must be a number")
    return float(value)


def _last(values: Sequence[float | None]) -> float | None:
    return values[-1] if values else None


def _close(w: BarWindow, p: Params) -> float | None:
    return w.close[-1]


def _return(w: BarWindow, p: Params) -> float | None:
    series = ind.roc(w.close, _int(p, "periods"))
    offset = _int(p, "offset")
    return series[-1 - offset] if len(series) > offset else None


def _log_return(w: BarWindow, p: Params) -> float | None:
    n = _int(p, "periods")
    start, end = w.close[-n - 1], w.close[-1]
    return math.log(end / start) if start > 0 and end > 0 else None


def _sma(w: BarWindow, p: Params) -> float | None:
    return _last(ind.sma(w.close, _int(p, "length")))


def _ema(w: BarWindow, p: Params) -> float | None:
    return _last(ind.ema(w.close, _int(p, "length")))


def _rsi(w: BarWindow, p: Params) -> float | None:
    return _last(ind.rsi(w.close, _int(p, "length")))


def _atr(w: BarWindow, p: Params) -> float | None:
    return _last(ind.atr(w.high, w.low, w.close, _int(p, "length")))


def _atr_pct(w: BarWindow, p: Params) -> float | None:
    atr, last = _atr(w, p), w.close[-1]
    return None if atr is None or last <= 0 else atr / last


def _atr_ratio(w: BarWindow, p: Params) -> float | None:
    short = _last(ind.atr(w.high, w.low, w.close, _int(p, "short")))
    long = _last(ind.atr(w.high, w.low, w.close, _int(p, "long")))
    return None if short is None or long is None or long == 0 else short / long


def _dmi(index: int) -> BarCompute:
    def compute(w: BarWindow, p: Params) -> float | None:
        return _last(ind.directional_movement(w.high, w.low, w.close, _int(p, "length"))[index])

    return compute


def _macd(index: int) -> BarCompute:
    def compute(w: BarWindow, p: Params) -> float | None:
        series = ind.macd(w.close, _int(p, "fast"), _int(p, "slow"), _int(p, "signal"))
        return _last(series[index])

    return compute


def _bollinger(part: str) -> BarCompute:
    def compute(w: BarWindow, p: Params) -> float | None:
        n, k = _int(p, "length"), _float(p, "k")
        if part == "width":
            return _last(ind.bollinger_width(w.close, n, k))
        middle, upper, lower = (_last(s) for s in ind.bollinger_bands(w.close, n, k))
        if middle is None or upper is None or lower is None:
            return None
        if part == "percent_b":
            return None if upper == lower else (w.close[-1] - lower) / (upper - lower)
        return {"middle": middle, "upper": upper, "lower": lower}[part]

    return compute


def _bollinger_width_percentile(w: BarWindow, p: Params) -> float | None:
    widths = ind.bollinger_width(w.close, _int(p, "length"), _float(p, "k"))
    history = [x for x in widths[-_int(p, "history") :] if x is not None]
    current = widths[-1]
    return None if current is None else ind.percentile_rank(history, current)


def _zscore(w: BarWindow, p: Params) -> float | None:
    n = _int(p, "length")
    window = w.close[-n:]
    mean, sd = _last(ind.sma(window, n)), ind.stdev(window, ddof=0)
    return None if mean is None or sd is None or sd == 0 else (w.close[-1] - mean) / sd


def _volume_ratio(w: BarWindow, p: Params) -> float | None:
    return ind.volume_ratio(w.volume, _int(p, "short"), _int(p, "long"))


def _realized_volatility(w: BarWindow, p: Params) -> float | None:
    return _last(ind.rolling_volatility(w.close, _int(p, "length"), w.periods_per_year))


def _prior(highest: bool) -> BarCompute:
    def compute(w: BarWindow, p: Params) -> float | None:
        series = w.high if highest else w.low
        return ind.prior_extreme(series, _int(p, "length"), highest=highest)

    return compute


def _close_location(w: BarWindow, p: Params) -> float | None:
    return ind.close_location(w.high[-1], w.low[-1], w.close[-1])


def _true_range_ratio(w: BarWindow, p: Params) -> float | None:
    newest = ind.true_range(w.high[-2:], w.low[-2:], w.close[-2:])[-1]
    before = _last(ind.atr(w.high[:-1], w.low[:-1], w.close[:-1], _int(p, "length")))
    return None if before is None or before <= 0 else newest / before


def _log_slope(index: int) -> BarCompute:
    def compute(w: BarWindow, p: Params) -> float | None:
        fitted = ind.log_slope(w.close[-_int(p, "length") :])
        if fitted is None:
            return None
        value = fitted[index]
        # A perfect fit has an infinite t-statistic; report the cap instead.
        return max(-TSTAT_CAP, min(TSTAT_CAP, value)) if index == 1 else value

    return compute


def _realized_volatility_percentile(w: BarWindow, p: Params) -> float | None:
    series = ind.rolling_volatility(w.close, _int(p, "length"), w.periods_per_year)
    history = [x for x in series[-_int(p, "history") :] if x is not None]
    current = series[-1]
    return None if current is None else ind.percentile_rank(history, current)


def _prior_bollinger_width_percentile(w: BarWindow, p: Params) -> float | None:
    widths = ind.bollinger_width(w.close, _int(p, "length"), _float(p, "k"))
    ending = widths[: len(widths) - _int(p, "offset")]
    history = [x for x in ending[-_int(p, "history") :] if x is not None]
    current = ending[-1] if ending else None
    return None if current is None else ind.percentile_rank(history, current)


def _failed_breakout_count(w: BarWindow, p: Params) -> float | None:
    count = ind.failed_breakouts(
        w.high,
        w.low,
        w.close,
        _int(p, "length"),
        _int(p, "lookback"),
        upside=p["side"] == "HIGH",
    )
    return None if count is None else float(count)


def _relative_return(asset: BarWindow, benchmark: BarWindow, p: Params) -> float | None:
    n = _int(p, "periods")
    mine, theirs = ind.roc(asset.close, n)[-1], ind.roc(benchmark.close, n)[-1]
    return None if mine is None or theirs is None else mine - theirs


def _benchmark_return(asset: BarWindow, benchmark: BarWindow, p: Params) -> float | None:
    return ind.roc(benchmark.close, _int(p, "periods"))[-1]


def _benchmark_volatility(asset: BarWindow, benchmark: BarWindow, p: Params) -> float | None:
    return _last(
        ind.rolling_volatility(benchmark.close, _int(p, "length"), benchmark.periods_per_year)
    )


def _symbol(p: Params) -> str | None:
    value = str(p["benchmark"])
    return None if SYMBOL_PATTERN.match(value) else "benchmark must be an upper-case ticker"


def _shorter(short: str, long: str) -> Callable[[Params], str | None]:
    def check(p: Params) -> str | None:
        return None if _int(p, short) < _int(p, long) else f"{short} must be shorter than {long}"

    return check


def _not_longer(short: str, long: str) -> Callable[[Params], str | None]:
    def check(p: Params) -> str | None:
        return None if _int(p, short) <= _int(p, long) else f"{short} must not exceed {long}"

    return check


def _length(default: int, maximum: int = 400) -> Parameter:
    return Parameter("length", int, default, "periods in the calculation", 1, maximum)


_K = Parameter("k", float, 2.0, "standard deviations from the middle band", 0.5, 4.0)
_BARS = ("bars.high", "bars.low", "bars.close")
_CLOSE = ("bars.close",)
_BAR_AVAILABILITY = "the newest availability (available_at) among the completed bars of its window"

V1 = "v1"
TSTAT_CAP = 1000.0
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def _bar(
    name: str,
    description: str,
    method: str,
    parameters: tuple[Parameter, ...],
    window_rule: str,
    window: Callable[[Params], int],
    compute: BarCompute,
    *,
    required: tuple[str, ...] = _CLOSE,
    check: Callable[[Params], str | None] | None = None,
) -> FeatureDefinition:
    return FeatureDefinition(
        name=name,
        version=V1,
        source=FeatureSource.BARS,
        description=description,
        method=method,
        parameters=parameters,
        timeframes=BAR_TIMEFRAMES,
        required_data=required,
        availability=_BAR_AVAILABILITY,
        window_rule=window_rule,
        window=window,
        compute=compute,
        check=check,
    )


def _bar_definitions() -> list[FeatureDefinition]:
    length = lambda p: _int(p, "length")  # noqa: E731 - window rules read best inline
    macd_params = (
        Parameter("fast", int, 12, "fast EMA periods", 2, 100),
        Parameter("slow", int, 26, "slow EMA periods", 3, 200),
        Parameter("signal", int, 9, "signal EMA periods", 2, 50),
    )
    macd_window = lambda p: 3 * _int(p, "slow") + _int(p, "signal")  # noqa: E731
    bands = (_length(20), _K)
    definitions = [
        _bar(
            "close",
            "Close of the newest completed period (price units of the stored series).",
            "close[t]",
            (),
            "1",
            lambda p: 1,
            _close,
        ),
        _bar(
            "return",
            "Simple return over `periods` completed periods, ending `offset` periods before "
            "the newest one (offset 0: ending at the newest).",
            "close[t - offset] / close[t - offset - periods] - 1 (indicators.roc)",
            (
                Parameter("periods", int, 1, "periods in the return", 1, 260),
                Parameter("offset", int, 0, "periods between its end and the newest", 0, 60),
            ),
            "periods + offset + 1",
            lambda p: _int(p, "periods") + _int(p, "offset") + 1,
            _return,
        ),
        _bar(
            "log_return",
            "Logarithmic return over the last `periods` completed periods.",
            "ln(close[t] / close[t - periods])",
            (Parameter("periods", int, 1, "periods back", 1, 260),),
            "periods + 1",
            lambda p: _int(p, "periods") + 1,
            _log_return,
        ),
        _bar(
            "sma",
            "Simple moving average of closes.",
            "arithmetic mean of the last `length` closes (indicators.sma)",
            (_length(20),),
            "length",
            length,
            _sma,
        ),
        _bar(
            "ema",
            "Exponential moving average of closes.",
            "alpha = 2 / (length + 1), seeded with the SMA of the window's first `length` "
            "closes (indicators.ema)",
            (_length(20, 200),),
            "3 * length",
            lambda p: 3 * _int(p, "length"),
            _ema,
        ),
        _bar(
            "rsi",
            "Relative strength index (0-100).",
            "Wilder (1978): 100 - 100 / (1 + avg_gain / avg_loss), Wilder smoothing "
            "(indicators.rsi)",
            (_length(14, 100),),
            "5 * length + 1",
            lambda p: 5 * _int(p, "length") + 1,
            _rsi,
        ),
        _bar(
            "atr",
            "Average true range in price units of the stored series.",
            "Wilder (1978) true range smoothed with Wilder's average (indicators.atr)",
            (_length(14, 100),),
            "5 * length + 1",
            lambda p: 5 * _int(p, "length") + 1,
            _atr,
            required=_BARS,
        ),
        _bar(
            "atr_pct",
            "Average true range relative to the newest close.",
            "atr(length) / close[t]",
            (_length(14, 100),),
            "5 * length + 1",
            lambda p: 5 * _int(p, "length") + 1,
            _atr_pct,
            required=_BARS,
        ),
        _bar(
            "atr_ratio",
            "Short ATR over long ATR (range expansion above 1, contraction below).",
            "atr(short) / atr(long)",
            (
                Parameter("short", int, 5, "short ATR periods", 1, 100),
                Parameter("long", int, 20, "long ATR periods", 2, 100),
            ),
            "5 * long + 1",
            lambda p: 5 * _int(p, "long") + 1,
            _atr_ratio,
            required=_BARS,
            check=_shorter("short", "long"),
        ),
        _bar(
            "adx",
            "Average directional index (trend strength, 0-100).",
            "Wilder (1978) DMI/ADX (indicators.directional_movement)",
            (_length(14, 100),),
            "6 * length",
            lambda p: 6 * _int(p, "length"),
            _dmi(2),
            required=_BARS,
        ),
        _bar(
            "plus_di",
            "Positive directional indicator (0-100).",
            "Wilder (1978) +DI (indicators.directional_movement)",
            (_length(14, 100),),
            "6 * length",
            lambda p: 6 * _int(p, "length"),
            _dmi(0),
            required=_BARS,
        ),
        _bar(
            "minus_di",
            "Negative directional indicator (0-100).",
            "Wilder (1978) -DI (indicators.directional_movement)",
            (_length(14, 100),),
            "6 * length",
            lambda p: 6 * _int(p, "length"),
            _dmi(1),
            required=_BARS,
        ),
        _bar(
            "macd_line",
            "MACD line in price units.",
            "Appel: ema(fast) - ema(slow) (indicators.macd)",
            macd_params,
            "3 * slow + signal",
            macd_window,
            _macd(0),
            check=_shorter("fast", "slow"),
        ),
        _bar(
            "macd_signal",
            "MACD signal line in price units.",
            "EMA(signal) of the MACD line (indicators.macd)",
            macd_params,
            "3 * slow + signal",
            macd_window,
            _macd(1),
            check=_shorter("fast", "slow"),
        ),
        _bar(
            "macd_histogram",
            "MACD histogram in price units.",
            "MACD line - signal line (indicators.macd)",
            macd_params,
            "3 * slow + signal",
            macd_window,
            _macd(2),
            check=_shorter("fast", "slow"),
        ),
        _bar(
            "bollinger_middle",
            "Bollinger middle band (SMA) in price units.",
            "sma(length) (indicators.bollinger_bands)",
            bands,
            "length",
            length,
            _bollinger("middle"),
        ),
        _bar(
            "bollinger_upper",
            "Bollinger upper band in price units.",
            "sma(length) + k * population stdev (indicators.bollinger_bands)",
            bands,
            "length",
            length,
            _bollinger("upper"),
        ),
        _bar(
            "bollinger_lower",
            "Bollinger lower band in price units.",
            "sma(length) - k * population stdev (indicators.bollinger_bands)",
            bands,
            "length",
            length,
            _bollinger("lower"),
        ),
        _bar(
            "bollinger_percent_b",
            "Position of the close within the bands (0 = lower, 1 = upper).",
            "(close - lower) / (upper - lower)",
            bands,
            "length",
            length,
            _bollinger("percent_b"),
        ),
        _bar(
            "bollinger_width",
            "Relative Bollinger band width.",
            "(upper - lower) / middle = 2 k sigma / mu (indicators.bollinger_width)",
            bands,
            "length",
            length,
            _bollinger("width"),
        ),
        _bar(
            "bollinger_width_percentile",
            "Rank of the current band width among the last `history` widths (0-1).",
            "mid-rank percentile (indicators.percentile_rank) of bollinger_width",
            (*bands, Parameter("history", int, 100, "widths ranked against", 10, 500)),
            "length + history - 1",
            lambda p: _int(p, "length") + _int(p, "history") - 1,
            _bollinger_width_percentile,
        ),
        _bar(
            "zscore",
            "Distance of the close from its mean in standard deviations.",
            "(close - sma(length)) / population stdev of the last `length` closes",
            (_length(20),),
            "length",
            length,
            _zscore,
        ),
        _bar(
            "volume_ratio",
            "Recent volume relative to its average (single-venue IEX volume: relative "
            "values only).",
            "mean(volume, short) / mean(volume, long) (indicators.volume_ratio)",
            (
                Parameter("short", int, 5, "recent periods", 1, 100),
                Parameter("long", int, 20, "reference periods", 2, 260),
            ),
            "long",
            lambda p: _int(p, "long"),
            _volume_ratio,
            required=("bars.volume",),
            check=_not_longer("short", "long"),
        ),
        _bar(
            "realized_volatility",
            "Annualized realized volatility of log returns.",
            "sample stdev (ddof 1) of the last `length` log returns * sqrt(periods per year) "
            "(indicators.rolling_volatility)",
            (Parameter("length", int, 20, "returns in the calculation", 2, 260),),
            "length + 1",
            lambda p: _int(p, "length") + 1,
            _realized_volatility,
        ),
        _bar(
            "prior_high",
            "Highest high of the `length` periods before the newest one.",
            "max(high[t - length .. t - 1]) (indicators.prior_extreme)",
            (_length(20, 260),),
            "length + 1",
            lambda p: _int(p, "length") + 1,
            _prior(highest=True),
            required=("bars.high",),
        ),
        _bar(
            "prior_low",
            "Lowest low of the `length` periods before the newest one.",
            "min(low[t - length .. t - 1]) (indicators.prior_extreme)",
            (_length(20, 260),),
            "length + 1",
            lambda p: _int(p, "length") + 1,
            _prior(highest=False),
            required=("bars.low",),
        ),
        # --- Phase 4B additions (new names; earlier definitions are unchanged) ----------
        _bar(
            "close_location",
            "Where the newest period closed within its own high-low range (0 = at the low, "
            "1 = at the high).",
            "(close - low) / (high - low) of the newest period; UNDEFINED when high = low "
            "(indicators.close_location)",
            (),
            "1",
            lambda p: 1,
            _close_location,
            required=_BARS,
        ),
        _bar(
            "true_range_ratio",
            "True range of the newest period relative to the average true range of the "
            "periods before it (range expansion above 1).",
            "true_range[t] / atr(length) over the periods before t (indicators.true_range, "
            "indicators.atr)",
            (_length(14, 100),),
            "5 * length + 2",
            lambda p: 5 * _int(p, "length") + 2,
            _true_range_ratio,
            required=_BARS,
        ),
        _bar(
            "log_slope",
            "Trend slope: growth rate per period of the least-squares line through log "
            "closes (0.001 = +0.1 % per period).",
            "OLS slope of ln(close) over the last `length` closes (indicators.log_slope)",
            (Parameter("length", int, 20, "closes in the fit", 3, 260),),
            "length",
            length,
            _log_slope(0),
        ),
        _bar(
            "log_slope_tstat",
            "How clearly the trend slope differs from flat: slope over its standard error "
            "(relative evidence, not a significance test; residuals are autocorrelated).",
            "OLS t-statistic of the ln(close) slope over the last `length` closes "
            f"(indicators.log_slope), capped at +/-{TSTAT_CAP:g} for a perfect fit",
            (Parameter("length", int, 20, "closes in the fit", 3, 260),),
            "length",
            length,
            _log_slope(1),
        ),
        _bar(
            "realized_volatility_percentile",
            "Rank of the current realized volatility among the last `history` values (0-1).",
            "mid-rank percentile (indicators.percentile_rank) of realized_volatility(length)",
            (
                Parameter("length", int, 20, "returns per volatility value", 2, 260),
                Parameter("history", int, 100, "values ranked against", 10, 500),
            ),
            "length + history",
            lambda p: _int(p, "length") + _int(p, "history"),
            _realized_volatility_percentile,
        ),
        _bar(
            "prior_bollinger_width_percentile",
            "Bollinger band-width percentile as it stood `offset` periods before the newest "
            "one (compression measured before the newest period's move).",
            "mid-rank percentile of bollinger_width at t - offset among the `history` widths "
            "ending there (indicators.bollinger_width, indicators.percentile_rank)",
            (
                *bands,
                Parameter("history", int, 100, "widths ranked against", 10, 500),
                Parameter("offset", int, 1, "periods before the newest", 1, 20),
            ),
            "length + history - 1 + offset",
            lambda p: _int(p, "length") + _int(p, "history") - 1 + _int(p, "offset"),
            _prior_bollinger_width_percentile,
        ),
        _bar(
            "failed_breakout_count",
            "Breakouts beyond the prior `length`-period high (side HIGH) or low (LOW) among "
            "the `lookback` periods before the newest one that later closed back inside.",
            "count of i in [t - lookback, t - 1] with close[i] beyond the extreme of the "
            "`length` periods before i and a later close (up to t) back across that level "
            "(indicators.failed_breakouts)",
            (
                Parameter("length", int, 20, "periods defining the level", 2, 260),
                Parameter("lookback", int, 20, "breakout periods examined", 1, 120),
                Parameter("side", str, "HIGH", "breakout side", choices=("HIGH", "LOW")),
            ),
            "length + lookback + 1",
            lambda p: _int(p, "length") + _int(p, "lookback") + 1,
            _failed_breakout_count,
            required=_BARS,
        ),
    ]
    return definitions


def _benchmark_definitions() -> list[FeatureDefinition]:
    return [
        FeatureDefinition(
            name="relative_return",
            version=V1,
            source=FeatureSource.BENCHMARK,
            description="Relative strength: the asset's return over `periods` minus the "
            "benchmark's return over the same completed periods.",
            method="roc(asset close, periods) - roc(benchmark close, periods) "
            "(indicators.roc); both series from the same completed, settled periods and "
            "known by the same knowledge time; 0 by construction for the benchmark itself",
            parameters=(
                Parameter("periods", int, 20, "periods in the returns", 1, 260),
                Parameter("benchmark", str, "SPY", "benchmark symbol (an Atlas asset)"),
            ),
            timeframes=BAR_TIMEFRAMES,
            required_data=("bars.close", "benchmark bars.close"),
            availability="the newest availability among the completed bars of both windows",
            window_rule="periods + 1",
            window=lambda p: _int(p, "periods") + 1,
            pair_compute=_relative_return,
            check=_symbol,
        ),
        # --- Phase 5A additions (new names): broad-market context for ML datasets -------
        FeatureDefinition(
            name="benchmark_return",
            version=V1,
            source=FeatureSource.BENCHMARK,
            description="The benchmark's own simple return over `periods` completed periods "
            "(broad-market direction, measured on the asset's periods).",
            method="roc(benchmark close, periods) (indicators.roc); the benchmark's bars must "
            "cover exactly the asset's completed, settled periods",
            parameters=(
                Parameter("periods", int, 20, "periods in the return", 1, 260),
                Parameter("benchmark", str, "SPY", "benchmark symbol (an Atlas asset)"),
            ),
            timeframes=BAR_TIMEFRAMES,
            required_data=("bars.close", "benchmark bars.close"),
            availability="the newest availability among the completed bars of both windows",
            window_rule="periods + 1",
            window=lambda p: _int(p, "periods") + 1,
            pair_compute=_benchmark_return,
            check=_symbol,
        ),
        FeatureDefinition(
            name="benchmark_realized_volatility",
            version=V1,
            source=FeatureSource.BENCHMARK,
            description="Annualized realized volatility of the benchmark's log returns "
            "(broad-market volatility, measured on the asset's periods).",
            method="sample stdev (ddof 1) of the benchmark's last `length` log returns * "
            "sqrt(periods per year) (indicators.rolling_volatility)",
            parameters=(
                Parameter("length", int, 20, "returns in the calculation", 2, 260),
                Parameter("benchmark", str, "SPY", "benchmark symbol (an Atlas asset)"),
            ),
            timeframes=BAR_TIMEFRAMES,
            required_data=("bars.close", "benchmark bars.close"),
            availability="the newest availability among the completed bars of both windows",
            window_rule="length + 1",
            window=lambda p: _int(p, "length") + 1,
            pair_compute=_benchmark_volatility,
            check=_symbol,
        ),
    ]


def _stored_definitions() -> list[FeatureDefinition]:
    regime_params = (
        Parameter(
            "dimension",
            str,
            Dimension.TREND.value,
            "classified dimension",
            choices=tuple(d.value for d in Dimension),
        ),
        Parameter(
            "scope",
            str,
            Scope.ASSET.value,
            "ASSET: the instrument itself; MARKET: the broad market (benchmark)",
            choices=tuple(s.value for s in Scope),
        ),
        Parameter("max_age_hours", int, 96, "older classifications are UNAVAILABLE", 1, 720),
    )
    regime_method = (
        "newest stored Phase 3B regime classification (regime_classifications) with "
        "as_of <= the data cutoff and available_at <= the knowledge time; RECORDED-basis "
        "evaluations use only RECORDED classifications"
    )
    regime_availability = "the classification's available_at"
    categories = ("ANY", *(c.value for c in EventCategory))
    news_params = (
        Parameter("lookback_hours", int, 24, "hours before the cutoff", 1, 720),
        Parameter("category", str, "ANY", "event category filter", choices=categories),
        Parameter("min_relevance", float, 0.0, "minimum static relevance (relevance-v1)", 0, 1),
    )
    news_method = (
        "articles DIRECTLY related to the asset whose newest Phase 3C event known at the "
        "knowledge time has event_time in (cutoff - lookback, cutoff]; UNAVAILABLE unless "
        "completed news retrieval or a subscribed stream covered the whole window. Absent "
        "coverage is never read as 'no news'."
    )
    news_availability = "the newest available_at among the events used (or of the coverage)"
    return [
        FeatureDefinition(
            name="regime_label",
            version=V1,
            source=FeatureSource.MARKET_INTELLIGENCE,
            description="Descriptive Phase 3B regime label (not a forecast).",
            method=regime_method,
            parameters=regime_params,
            timeframes=BAR_TIMEFRAMES,
            required_data=("regime_classifications",),
            availability=regime_availability,
            value_kind=ValueKind.LABEL,
        ),
        FeatureDefinition(
            name="regime_confidence",
            version=V1,
            source=FeatureSource.MARKET_INTELLIGENCE,
            description="Confidence of the Phase 3B regime label (evidence weight, 0-1; not "
            "a calibrated probability).",
            method=regime_method,
            parameters=regime_params,
            timeframes=BAR_TIMEFRAMES,
            required_data=("regime_classifications",),
            availability=regime_availability,
        ),
        FeatureDefinition(
            name="news_event_count",
            version=V1,
            source=FeatureSource.NEWS,
            description="Number of news articles about the asset in the look-back window.",
            method=news_method,
            parameters=news_params,
            timeframes=BAR_TIMEFRAMES,
            required_data=("news_events", "event_asset_relationships", "news coverage"),
            availability=news_availability,
        ),
        FeatureDefinition(
            name="news_hours_since_latest",
            version=V1,
            source=FeatureSource.NEWS,
            description="Hours from the newest matching article to the cutoff; UNDEFINED "
            "when the covered window has none.",
            method=news_method,
            parameters=news_params,
            timeframes=BAR_TIMEFRAMES,
            required_data=("news_events", "event_asset_relationships", "news coverage"),
            availability=news_availability,
        ),
        FeatureDefinition(
            name="period_news_count",
            version=V1,
            source=FeatureSource.NEWS,
            description="Number of news articles about the asset published between the "
            "regular close of the session `sessions` before the newest completed one and the "
            "newest one's close: every article counted precedes the price reaction of the "
            "newest period(s), and weekends and holidays are covered.",
            method=(
                "articles DIRECTLY related to the asset whose newest Phase 3C event known at "
                "the knowledge time has event_time in (close[s - sessions], close[s]], where "
                "close[s] is the regular close of the newest session completed and settled by "
                "the cutoff (exchange calendar); UNAVAILABLE unless completed news retrieval "
                "or a subscribed stream covered the whole window. Absent coverage is never "
                "read as 'no news'."
            ),
            parameters=(
                Parameter("sessions", int, 1, "sessions covered, ending at the newest", 1, 5),
                *news_params[1:],
            ),
            timeframes=BAR_TIMEFRAMES,
            required_data=("news_events", "event_asset_relationships", "news coverage"),
            availability=news_availability,
        ),
    ]


REGISTRY = FeatureRegistry([*_bar_definitions(), *_benchmark_definitions(), *_stored_definitions()])


__all__ = [
    "BAR_TIMEFRAMES",
    "DAILY",
    "FEATURE_REGISTRY_VERSION",
    "PERIODS_PER_YEAR",
    "REGISTRY",
    "WEEKLY",
    "BarWindow",
    "DefinitionStatus",
    "FeatureDefinition",
    "FeatureRegistry",
    "FeatureRegistryError",
    "FeatureRequest",
    "FeatureSource",
    "FeatureStatus",
    "FeatureValue",
    "PairCompute",
    "Parameter",
    "ResolvedFeature",
    "ValueKind",
    "sha256_json",
]
