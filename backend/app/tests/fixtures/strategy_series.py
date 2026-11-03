"""DETERMINISTIC TEST FIXTURES for the Phase 4B strategies.

Every series here is SYNTHETIC: prices built from a formula, not market data and not a
provider response. They exist to drive each strategy's conditions deterministically.

Features are computed by the real feature pipeline (``compute_bar_feature`` and
``compute_benchmark_feature``) from these completed bars, so a test exercises the
indicator calculations and the strategy logic together. Stored features (Phase 3B
regimes, Phase 3C news) need the database; here they are supplied as FIXTURE values.

``mirror`` reflects a series around a price level (high <-> low), turning every LONG
setup into the equivalent SHORT one, so both directions are tested with the same data.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.database.models import AssetClass
from app.feature_engine.bar_features import BarHistory, compute_bar_feature
from app.feature_engine.benchmark import Benchmark, compute_benchmark_feature
from app.feature_engine.features import Bar
from app.feature_engine.registry import (
    REGISTRY,
    FeatureSource,
    FeatureStatus,
    FeatureValue,
    ResolvedFeature,
)
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.base import (
    AssetView,
    Eligibility,
    EvaluationContext,
    Strategy,
    StrategyOutcome,
    StrategyParameters,
)
from app.strategies.enums import EvaluationMode, TriggerKind
from app.strategies.proposals import ProposalSpec, validate_drafts

FIRST_DAY = date(2025, 1, 1)
CUTOFF = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)
ASSET_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
BENCHMARK_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
ELIGIBLE = Eligibility(
    known=True,
    provider="alpaca",
    provider_status="ACTIVE",
    tradable=True,
    shortable=True,
    easy_to_borrow=True,
    reference_version=1,
    observed_at=datetime(2020, 1, 2, tzinfo=UTC),
)


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float = 1_000_000.0


def from_closes(
    closes: Sequence[float],
    *,
    spread: float = 0.004,
    volumes: Sequence[float] | None = None,
) -> list[Candle]:
    """Candles opening at the previous close, with a high/low ``spread`` (fraction)
    beyond the body."""
    out: list[Candle] = []
    previous = closes[0]
    for i, close in enumerate(closes):
        top, bottom = max(previous, close), min(previous, close)
        volume = 1_000_000.0 if volumes is None else volumes[i]
        out.append(Candle(previous, top * (1 + spread), bottom * (1 - spread), close, volume))
        previous = close
    return out


def oscillating(count: int, start: float, drift: float, swing: float) -> list[float]:
    """Closes moving by ``drift`` per period plus an alternating ``swing`` (fractions)."""
    closes = [start]
    for i in range(1, count):
        step = drift + (swing if i % 2 else -swing)
        closes.append(closes[-1] * (1 + step))
    return closes


def noisy(count: int, start: float, drift: float, vol: float, seed: int) -> list[float]:
    """Closes with ``drift`` per period plus seeded Gaussian noise of ``vol`` (fractions):
    deterministic for a seed, irregular enough for realistic ADX and RSI values."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture data, not security
    closes = [start]
    for _ in range(1, count):
        closes.append(closes[-1] * (1 + drift + rng.gauss(0.0, vol)))
    return closes


def then(closes: Sequence[float], *steps: float) -> list[float]:
    """``closes`` followed by closes moving by each of ``steps`` (fractions)."""
    out = list(closes)
    for step in steps:
        out.append(out[-1] * (1 + step))
    return out


def mirror(candles: Sequence[Candle], level: float | None = None) -> list[Candle]:
    """Reflect around ``level`` (default: the middle of the series' range): rises become
    falls, highs become lows."""
    if level is None:
        level = (max(c.high for c in candles) + min(c.low for c in candles)) / 2
    return [
        Candle(
            2 * level - c.open, 2 * level - c.low, 2 * level - c.high, 2 * level - c.close, c.volume
        )
        for c in candles
    ]


def ranging(count: int, level: float, amp0: float, amp1: float, seed: int) -> list[float]:
    """Closes scattered around ``level`` (a range, not a walk) with an amplitude moving
    from ``amp0`` to ``amp1``: a narrowing consolidation when amp1 < amp0."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture data, not security
    out = []
    for i in range(count):
        amp = amp0 + (amp1 - amp0) * i / (count - 1)
        out.append(level * (1 + amp * max(-2.0, min(2.0, rng.gauss(0.0, 1.0)))))
    return out


def contracting(count: int, start: float, vol0: float, vol1: float, seed: int) -> list[float]:
    """A seeded walk whose noise shrinks from ``vol0`` to ``vol1``: volatility compression."""
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture data, not security
    closes = [start]
    for i in range(1, count):
        vol = vol0 + (vol1 - vol0) * i / (count - 1)
        closes.append(closes[-1] * (1 + rng.gauss(0.0, vol)))
    return closes


def shape_last(candles: list[Candle], open_: float, high: float, low: float) -> list[Candle]:
    """Set the newest candle's open/high/low as multiples of its close."""
    last = candles[-1]
    candles[-1] = Candle(
        last.close * open_, last.close * high, last.close * low, last.close, last.volume
    )
    return candles


# --- Named scenarios (each tuned so the default parameters produce a LONG proposal; ------
# --- ``mirror`` gives the SHORT counterpart) -------------------------------------------


def momentum_up() -> list[Candle]:
    """A steady advance that accelerates over the last five periods on rising volume."""
    closes = then(oscillating(260, 50.0, 0.0012, 0.008), 0.012, 0.004, 0.016, -0.003, 0.014)
    volumes = [1_000_000.0] * (len(closes) - 5) + [1_600_000.0] * 5
    return from_closes(closes, volumes=volumes)


def benchmark_flat(count: int) -> list[Candle]:
    """A FIXTURE benchmark drifting slightly upwards."""
    return from_closes(oscillating(count, 400.0, 0.0002, 0.006))


def trend_up() -> list[Candle]:
    """A noisy established uptrend ending in a shallow pullback towards its fast EMA."""
    return from_closes(then(noisy(300, 50.0, 0.0015, 0.008, 1), -0.006, -0.004, 0.003))


def dip_in_uptrend() -> list[Candle]:
    """An uptrend, a four-period selloff, then a period that closes up near its high."""
    closes = then(noisy(300, 50.0, 0.0015, 0.008, 2), -0.025, -0.03, -0.025, -0.03, 0.006)
    return shape_last(from_closes(closes), 0.985, 1.002, 0.982)


def range_breakout() -> list[Candle]:
    """A narrowing range, then a strong close above it on 2.5x volume."""
    closes = then(ranging(200, 50.0, 0.02, 0.006, 7), 0.03)
    volumes = [1_000_000.0] * (len(closes) - 1) + [2_500_000.0]
    return shape_last(from_closes(closes, spread=0.002, volumes=volumes), 0.975, 1.002, 0.973)


def compression_expansion() -> list[Candle]:
    """Shrinking volatility, then one wide period closing near its high."""
    closes = then(contracting(220, 50.0, 0.018, 0.003, 4), 0.035)
    return shape_last(from_closes(closes, spread=0.002), 0.968, 1.002, 0.966)


def event_reaction() -> list[Candle]:
    """Ordinary trading, then a +4.5% period on 3x volume closing near its high."""
    closes = then(noisy(80, 40.0, 0.0003, 0.01, 5), 0.045)
    volumes = [1_000_000.0] * (len(closes) - 1) + [3_000_000.0]
    return shape_last(from_closes(closes, volumes=volumes), 0.965, 1.002, 0.962)


def bars(candles: Sequence[Candle], *, cutoff: datetime = CUTOFF) -> list[Bar]:
    """Completed daily bars, each available 30 minutes after its (synthetic) close; the
    newest one ends before ``cutoff``."""
    last = cutoff - timedelta(hours=2)
    out = []
    count = len(candles)
    for i, c in enumerate(candles):
        event = last - timedelta(days=count - 1 - i)
        out.append(
            Bar(
                event_time=event,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                available_at=event + timedelta(minutes=30),
            )
        )
    return out


def history(
    candles: Sequence[Candle],
    timeframe: str = "1Day",
    *,
    drop: Sequence[int] = (),
    **kwargs: Any,
) -> BarHistory:
    """Every candle is an expected completed period; ``drop`` lists periods (indexes)
    whose bar is missing."""
    built = bars(candles, **kwargs)
    keys: list[Hashable] = [FIRST_DAY + timedelta(days=i) for i in range(len(built))]
    missing = {keys[i] for i in drop}
    return BarHistory(
        timeframe, keys, {k: b for k, b in zip(keys, built, strict=True) if k not in missing}
    )


def stored_value(
    feature: ResolvedFeature,
    value: float | str | None,
    inputs: Mapping[str, Any] | None = None,
) -> FeatureValue:
    """A FIXTURE stored feature: VALID with ``value``, or UNAVAILABLE when None."""
    d = feature.definition
    return FeatureValue(
        key=feature.key,
        name=d.name,
        version=d.version,
        source=d.source,
        timeframe=feature.timeframe,
        params=feature.param_map,
        value=value,
        status=FeatureStatus.VALID if value is not None else FeatureStatus.UNAVAILABLE,
        reason=None if value is not None else "FIXTURE: not supplied",
        available_at=CUTOFF - timedelta(hours=1),
        inputs=dict(inputs or {}),
    )


@dataclass(frozen=True)
class Run:
    ctx: EvaluationContext
    resolved: list[ResolvedFeature]
    outcome: StrategyOutcome | None
    specs: list[ProposalSpec]
    notes: list[str]
    blocked: dict[str, str]  # required features that were not VALID: key -> status

    @property
    def directions(self) -> list[str]:
        return [s.direction.value for s in self.specs]

    def failed(self, direction: str) -> list[str]:
        assert self.outcome is not None
        return [
            c.name
            for c in self.outcome.checks
            if c.passed is False and c.direction is not None and c.direction.value == direction
        ]

    def check(self, name: str, direction: str) -> Any:
        assert self.outcome is not None
        return next(
            c
            for c in self.outcome.checks
            if c.name == name and c.direction is not None and c.direction.value == direction
        )


def run(
    strategy: Strategy,
    candles: Sequence[Candle],
    *,
    params: StrategyParameters | Mapping[str, Any] | None = None,
    timeframe: str = "1Day",
    benchmark: Sequence[Candle] | None = None,
    benchmark_is_asset: bool = False,
    stored: Mapping[str, float | str | None] | None = None,
    eligibility: Eligibility | None = ELIGIBLE,
    asset_class: AssetClass = AssetClass.EQUITY,
    drop: Sequence[int] = (),
) -> Run:
    """Evaluate ``strategy`` on synthetic ``candles`` the way the engine does: compute the
    required features, call the strategy only if every required one is VALID, then
    validate the drafts. ``stored`` maps a stored feature's name (or full key) to its
    FIXTURE value; unlisted stored features are UNAVAILABLE."""
    if params is None:
        p = strategy.default_parameters()
    elif isinstance(params, StrategyParameters):
        p = params
    else:
        p = strategy.parse_parameters(params)
    resolved = REGISTRY.resolve_all(strategy.required_features(p, timeframe))
    hist = history(candles, timeframe, drop=drop)
    bench_hist = None if benchmark is None else history(benchmark, timeframe)
    stored = dict(stored or {})
    values: dict[str, FeatureValue] = {}
    for f in resolved:
        source = f.definition.source
        if source is FeatureSource.BARS:
            values[f.key] = compute_bar_feature(f, hist)
        elif source is FeatureSource.BENCHMARK:
            symbol = str(f.param_map["benchmark"])
            if benchmark_is_asset:
                bm = Benchmark(symbol, ASSET_ID, hist)
            elif bench_hist is None:
                bm = Benchmark(symbol, None, None, "FIXTURE: no benchmark")
            else:
                bm = Benchmark(symbol, BENCHMARK_ID, bench_hist)
            values[f.key] = compute_benchmark_feature(f, hist, bm, ASSET_ID)
        else:
            value = stored.get(f.key, stored.get(f.definition.name))
            values[f.key] = stored_value(f, value)
    ctx = EvaluationContext(
        asset=AssetView(ASSET_ID, "SYN", asset_class, "XNAS", eligibility),
        timeframe=timeframe,
        mode=EvaluationMode.LIVE,
        basis=KnowledgeBasis.RECORDED,
        data_cutoff=CUTOFF,
        features=values,
        trigger=TriggerKind.BAR_CLOSE,
    )
    blocked = {
        f.key: values[f.key].status.value
        for f in resolved
        if not f.optional and not values[f.key].valid
    }
    if blocked:
        return Run(ctx, resolved, None, [], [], blocked)
    outcome = strategy.evaluate(ctx, p)
    specs, notes = validate_drafts(
        outcome.proposals,
        ctx,
        p,
        expires_at=CUTOFF + timedelta(days=1),
        snapshot_ids=[uuid.uuid4()],
        evaluation_event="BAR_CLOSE:FIXTURE",
    )
    return Run(ctx, resolved, outcome, specs, notes, {})


def override(
    strategy: Strategy,
    base: Run,
    changes: Mapping[str, float | str | None],
    *,
    params: StrategyParameters | Mapping[str, Any] | None = None,
    eligibility: Eligibility | None = ELIGIBLE,
) -> Run:
    """Re-evaluate ``base`` with some feature values replaced (keyed by full feature key
    or by name, which replaces every feature of that name). Used to isolate one condition
    precisely; the replaced values are FIXTURE values."""
    if params is None:
        p = strategy.default_parameters()
    elif isinstance(params, StrategyParameters):
        p = params
    else:
        p = strategy.parse_parameters(params)
    values = dict(base.ctx.features)
    by_key = {f.key: f for f in base.resolved}
    for f in base.resolved:
        for target in (f.key, f.definition.name):
            if target in changes:
                old = values[f.key]
                values[f.key] = replace(
                    stored_value(by_key[f.key], changes[target]),
                    source=old.source,
                    inputs=old.inputs,
                )
    ctx = replace(
        base.ctx,
        features=values,
        asset=replace(base.ctx.asset, eligibility=eligibility),
    )
    outcome = strategy.evaluate(ctx, p)
    specs, notes = validate_drafts(
        outcome.proposals,
        ctx,
        p,
        expires_at=CUTOFF + timedelta(days=1),
        snapshot_ids=[uuid.uuid4()],
        evaluation_event="BAR_CLOSE:FIXTURE",
    )
    return Run(ctx, base.resolved, outcome, specs, notes, {})


def with_last(candles: Sequence[Candle], **changes: float) -> list[Candle]:
    out = list(candles)
    out[-1] = replace(out[-1], **changes)
    return out


__all__ = [
    "ASSET_ID",
    "BENCHMARK_ID",
    "CUTOFF",
    "ELIGIBLE",
    "Candle",
    "Run",
    "bars",
    "benchmark_flat",
    "compression_expansion",
    "contracting",
    "dip_in_uptrend",
    "event_reaction",
    "from_closes",
    "history",
    "mirror",
    "momentum_up",
    "noisy",
    "oscillating",
    "override",
    "range_breakout",
    "ranging",
    "run",
    "shape_last",
    "stored_value",
    "then",
    "trend_up",
    "with_last",
]
