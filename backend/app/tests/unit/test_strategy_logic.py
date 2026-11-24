"""The six Phase 4B strategies, each tested independently on DETERMINISTIC FIXTURES.

Prices are synthetic series (``app.tests.fixtures.strategy_series``), not market data;
features come from the real feature pipeline; stored regimes and news counts are FIXTURE
values. Every LONG scenario is mirrored into the equivalent SHORT one. Nothing here claims
a strategy is profitable: these tests check that each documented condition, threshold,
level and data rule behaves as specified.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from app.feature_engine.registry import FeatureSource
from app.strategies.base import Strategy, typed
from app.strategies.enums import Direction
from app.strategies.families.momentum import MomentumParameters
from app.strategies.registry import default_registry
from app.tests.fixtures.strategy_series import (
    ELIGIBLE,
    Candle,
    Run,
    benchmark_flat,
    compression_expansion,
    dip_in_uptrend,
    event_reaction,
    from_closes,
    mirror,
    momentum_up,
    oscillating,
    override,
    range_breakout,
    ranging,
    run,
    trend_up,
    with_last,
)

REGISTRY = default_registry()
NEWS = {"period_news_count": 1.0}


def momentum_kwargs(candles: list[Candle]) -> dict[str, Any]:
    return {"benchmark": benchmark_flat(len(candles))}


# strategy id -> (LONG scenario, extra run() arguments)
SCENARIOS: dict[
    str, tuple[Callable[[], list[Candle]], Callable[[list[Candle]], dict[str, Any]]]
] = {
    "momentum": (momentum_up, momentum_kwargs),
    "trend_following": (trend_up, lambda c: {}),
    "mean_reversion": (dip_in_uptrend, lambda c: {}),
    "breakout": (range_breakout, lambda c: {}),
    "volatility": (compression_expansion, lambda c: {}),
    "event_driven": (event_reaction, lambda c: {"stored": NEWS}),
}
IDS = list(SCENARIOS)

# A threshold that, tightened, defeats the default scenario, and the check that then fails.
TIGHTER = {
    "momentum": ({"min_volume_ratio": "3"}, "relative_volume"),
    "trend_following": ({"min_adx": "99"}, "adx_dmi"),
    "mean_reversion": ({"min_atr_distance": "15"}, "distance_from_mean"),
    "breakout": ({"min_volume_ratio": "5"}, "relative_volume"),
    "volatility": ({"expansion_ratio": "9"}, "range_expansion"),
    "event_driven": ({"min_reaction_atr": "10"}, "price_reaction"),
}


def strategy(sid: str) -> Strategy:
    return REGISTRY.get(sid)


def scenario(sid: str, short: bool = False, **extra: Any) -> tuple[list[Candle], Run]:
    build, kwargs = SCENARIOS[sid]
    candles = build()
    if short:
        candles = mirror(candles)
    return candles, run(strategy(sid), candles, **(kwargs(candles) | extra))


# --- Shared behaviour, every strategy ------------------------------------------------------
@pytest.mark.parametrize("sid", IDS)
@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_scenario_proposes_with_valid_levels_and_traceable_evidence(sid, direction):
    _, result = scenario(sid, short=direction is Direction.SHORT)
    assert result.blocked == {}
    assert [s.direction for s in result.specs] == [direction]
    spec = result.specs[0]
    assert spec.invalidation_level is not None
    assert spec.target_reference is not None
    if direction is Direction.LONG:
        assert spec.invalidation_level < spec.reference_entry < spec.target_reference
    else:
        assert spec.invalidation_level > spec.reference_entry > spec.target_reference
    assert Decimal(0) <= spec.signal_strength <= Decimal(1)
    assert spec.strength_kind.value == "UNCALIBRATED_SCORE"
    assert spec.reasoning.startswith(f"{strategy(sid).name} {direction.value}:")
    assert spec.evidence
    for item in spec.evidence:
        feature = result.ctx.features[item["feature_key"]]
        assert feature.valid
        assert feature.available_at is not None
        assert feature.available_at <= result.ctx.data_cutoff
    levels = spec.levels
    assert levels["entry_method"]
    assert levels["invalidation_method"]
    assert levels["target_method"]
    assert Decimal(levels["risk_per_unit"]) > 0
    assert (
        levels["holding_horizon_periods"]
        == strategy(sid).default_parameters().holding_horizon_periods
    )
    assert spec.holding_horizon_periods == levels["holding_horizon_periods"]
    # Every condition was recorded, passed or not, for both directions.
    assert result.outcome is not None
    assert {c.direction for c in result.outcome.checks} == {Direction.LONG, Direction.SHORT}


@pytest.mark.parametrize("sid", IDS)
def test_evaluation_is_deterministic(sid):
    _, first = scenario(sid)
    _, second = scenario(sid)
    a, b = first.specs[0], second.specs[0]
    assert (a.reference_entry, a.invalidation_level, a.target_reference, a.signal_strength) == (
        b.reference_entry,
        b.invalidation_level,
        b.target_reference,
        b.signal_strength,
    )
    assert a.evidence == b.evidence


@pytest.mark.parametrize("sid", IDS)
def test_thresholds_are_configurable(sid):
    change, check = TIGHTER[sid]
    _, result = scenario(sid, params=change)
    assert result.specs == []
    assert check in result.failed("LONG")
    assert result.outcome is not None
    assert check in (result.outcome.reason or "")


@pytest.mark.parametrize("sid", IDS)
def test_disabled_direction_is_not_proposed(sid):
    _, result = scenario(sid, params={"directions": ["SHORT"]})
    assert result.specs == []
    assert result.check("direction_enabled", "LONG").passed is None


@pytest.mark.parametrize("sid", IDS)
def test_short_needs_a_shortable_easy_to_borrow_asset(sid):
    for flags in ({"shortable": False}, {"easy_to_borrow": None}):
        _, result = scenario(sid, short=True, eligibility=replace(ELIGIBLE, **flags))
        assert result.specs == []
        eligibility = result.check("eligibility", "SHORT")
        assert eligibility.passed is False
        assert "SHORT needs" in eligibility.detail
    # LONG stays eligible on the same asset.
    _, long = scenario(sid, eligibility=replace(ELIGIBLE, shortable=False))
    assert [s.direction for s in long.specs] == [Direction.LONG]


@pytest.mark.parametrize("sid", IDS)
def test_insufficient_history_blocks_the_strategy(sid):
    build, kwargs = SCENARIOS[sid]
    candles = build()[-12:]
    result = run(strategy(sid), candles, **kwargs(candles))
    assert result.outcome is None  # the strategy is never called
    assert "INSUFFICIENT_DATA" in result.blocked.values()


@pytest.mark.parametrize("sid", IDS)
def test_missing_bar_inside_the_window_blocks_the_strategy(sid):
    build, kwargs = SCENARIOS[sid]
    candles = build()
    result = run(strategy(sid), candles, drop=[len(candles) - 3], **kwargs(candles))
    assert result.outcome is None
    assert "MISSING_INPUTS" in result.blocked.values()


@pytest.mark.parametrize("sid", IDS)
def test_inconsistent_prices_are_invalid_data_not_no_signal(sid):
    _, base = scenario(sid)
    atr_name = "atr"
    result = override(strategy(sid), base, {atr_name: 0.0})
    assert result.specs == []
    assert result.outcome is not None
    assert result.outcome.invalid_data is not None
    assert "ATR" in result.outcome.invalid_data
    negative = override(strategy(sid), base, {"close": -1.0})
    assert negative.outcome is not None
    assert "close" in (negative.outcome.invalid_data or "")


@pytest.mark.parametrize("sid", IDS)
def test_unchanged_flat_market_gives_no_signal(sid):
    candles = from_closes(ranging(320, 60.0, 0.004, 0.004, 99))
    extra = SCENARIOS[sid][1](candles)
    result = run(strategy(sid), candles, **extra)
    assert result.blocked == {}
    assert result.specs == []
    assert result.outcome is not None
    assert result.outcome.reason


# --- Momentum ------------------------------------------------------------------------------
def test_momentum_relative_strength_against_a_stronger_benchmark_fails():
    candles = momentum_up()
    strong = from_closes(oscillating(len(candles), 400.0, 0.006, 0.004))
    result = run(strategy("momentum"), candles, benchmark=strong)
    assert result.specs == []
    assert result.failed("LONG") == ["relative_strength"]


def test_momentum_relative_strength_is_not_applicable_to_the_benchmark_itself():
    candles = momentum_up()
    result = run(strategy("momentum"), candles, benchmark_is_asset=True)
    assert [s.direction for s in result.specs] == [Direction.LONG]
    rs = result.check("relative_strength", "LONG")
    assert rs.passed is None
    assert "not applicable" in rs.detail


def test_momentum_without_benchmark_data_blocks_unless_relative_strength_is_optional():
    candles = momentum_up()
    blocked = run(strategy("momentum"), candles)
    assert blocked.outcome is None
    assert list(blocked.blocked.values()) == ["UNAVAILABLE"]
    optional = run(strategy("momentum"), candles, params={"require_relative_strength": False})
    assert [s.direction for s in optional.specs] == [Direction.LONG]
    assert optional.check("relative_strength", "LONG").passed is None


def test_momentum_rejects_exhausted_and_decelerating_moves():
    _, base = scenario("momentum")
    exhausted = override(strategy("momentum"), base, {"rsi": 86.0})
    assert exhausted.failed("LONG") == ["not_exhausted"]
    p = typed(strategy("momentum").default_parameters(), MomentumParameters)
    short_key = next(
        f.key for f in base.resolved if f.param_map.get("periods") == p.acceleration_periods
    )
    slowing = override(strategy("momentum"), base, {short_key: 0.001})
    assert "acceleration" in slowing.failed("LONG")


# --- Trend following -----------------------------------------------------------------------
def test_trend_following_ignores_sideways_markets():
    # A range (not a random walk, which can genuinely trend over 300 periods).
    candles = from_closes(ranging(320, 60.0, 0.02, 0.02, 21))
    result = run(strategy("trend_following"), candles)
    assert result.specs == []
    assert {"trend_slope", "ma_alignment"} & set(result.failed("LONG"))
    assert {"trend_slope", "ma_alignment"} & set(result.failed("SHORT"))


def test_trend_following_respects_an_opposite_or_sideways_regime():
    for label in ("SIDEWAYS", "BEARISH"):
        _, blocked = scenario("trend_following", stored={"regime_label": label})
        assert blocked.failed("LONG") == ["trend_regime"]
    _, allowed = scenario("trend_following", stored={"regime_label": "BULLISH"})
    assert [s.direction for s in allowed.specs] == [Direction.LONG]
    _, ignored = scenario(
        "trend_following",
        stored={"regime_label": "SIDEWAYS"},
        params={"respect_trend_regime": False},
    )
    assert [s.direction for s in ignored.specs] == [Direction.LONG]


def test_trend_following_does_not_chase_overextended_prices():
    _, result = scenario("trend_following", params={"max_extension_atr": "0.1"})
    assert result.failed("LONG") == ["not_overextended"]


def test_trend_following_pullback_setup_and_trailing_invalidation():
    _, result = scenario("trend_following")
    spec = result.specs[0]
    assert spec.levels["setup"] == "PULLBACK"
    assert spec.invalidation_level is not None
    atr = result.ctx.number("atr", length=14)
    assert float(spec.reference_entry - spec.invalidation_level) == pytest.approx(3 * atr, abs=1e-3)


# --- Mean reversion ------------------------------------------------------------------------
def test_mean_reversion_never_trades_on_an_extreme_rsi_alone():
    _, base = scenario("mean_reversion")
    rsi_only = override(
        strategy("mean_reversion"),
        base,
        {"rsi": 12.0, "bollinger_percent_b": 0.4, "zscore": -0.5},
    )
    assert rsi_only.specs == []
    assert "statistical_stretch" in rsi_only.failed("LONG")
    # Even accepting a single extreme measure, RSI alone is not enough.
    lenient = override(
        strategy("mean_reversion"),
        base,
        {"rsi": 12.0, "bollinger_percent_b": 0.4, "zscore": -0.5},
        params={"min_extreme_signals": 1},
    )
    assert lenient.specs == []
    # A price-deviation measure with RSI is.
    with_band = override(
        strategy("mean_reversion"),
        base,
        {"rsi": 12.0, "zscore": -0.5},
        params={"min_extreme_signals": 2},
    )
    assert [s.direction for s in with_band.specs] == [Direction.LONG]


def test_mean_reversion_needs_reversal_confirmation_unless_disabled():
    _, base = scenario("mean_reversion")
    falling = override(strategy("mean_reversion"), base, {"return": -0.01})
    assert falling.failed("LONG") == ["reversal_confirmation"]
    unconfirmed = override(
        strategy("mean_reversion"),
        base,
        {"return": -0.01},
        params={"require_reversal_confirmation": False},
    )
    assert [s.direction for s in unconfirmed.specs] == [Direction.LONG]


def test_mean_reversion_uses_regimes_to_avoid_strong_opposing_trends():
    _, bearish = scenario("mean_reversion", stored={"regime_label": "BEARISH"})
    assert "market_trend" in bearish.failed("LONG")
    _, extreme = scenario("mean_reversion", stored={"regime_label": "EXTREME"})
    assert "volatility_regime" in extreme.failed("LONG")
    _, base = scenario("mean_reversion")
    downtrend = override(strategy("mean_reversion"), base, {"log_slope_tstat": -6.0})
    assert downtrend.failed("LONG") == ["opposing_trend"]
    below = override(strategy("mean_reversion"), base, {"sma": 1000.0})
    assert "long_term_trend" in below.failed("LONG")


def test_mean_reversion_targets_the_mean():
    _, result = scenario("mean_reversion")
    middle = result.ctx.number("bollinger_middle", length=20, k=2.0)
    target = result.specs[0].target_reference
    assert target is not None
    assert float(target) == pytest.approx(middle, abs=1e-4)


# --- Breakout ------------------------------------------------------------------------------
def test_breakout_level_excludes_the_newest_period():
    candles = range_breakout()
    base = run(strategy("breakout"), candles)
    spiked = run(strategy("breakout"), with_last(candles, high=candles[-1].high * 1.5))
    level = base.ctx.number("prior_high", length=20)
    assert spiked.ctx.number("prior_high", length=20) == level
    assert level == max(c.high for c in candles[-21:-1])
    assert level < candles[-1].close


def test_breakout_rejects_weak_closes_failed_breakouts_and_loose_ranges():
    _, base = scenario("breakout")
    weak = override(strategy("breakout"), base, {"close_location": 0.3})
    assert weak.failed("LONG") == ["strong_close"]
    failing = override(strategy("breakout"), base, {"failed_breakout_count": 3.0})
    assert failing.failed("LONG") == ["failed_breakouts"]
    loose = override(strategy("breakout"), base, {"prior_bollinger_width_percentile": 0.9})
    assert loose.failed("LONG") == ["volatility_compression"]
    quiet = override(strategy("breakout"), base, {"volume_ratio": 1.0})
    assert quiet.failed("LONG") == ["relative_volume"]


def test_breakout_levels_follow_the_range():
    _, result = scenario("breakout")
    spec = result.specs[0]
    high = result.ctx.number("prior_high", length=20)
    low = result.ctx.number("prior_low", length=20)
    atr = result.ctx.number("atr", length=14)
    assert spec.invalidation_level is not None
    assert spec.target_reference is not None
    assert float(spec.invalidation_level) == pytest.approx(high - atr, abs=1e-3)
    assert float(spec.target_reference) == pytest.approx(high + (high - low), abs=1e-3)


# --- Volatility ----------------------------------------------------------------------------
def test_volatility_alone_is_never_a_direction():
    _, base = scenario("volatility")
    undirected = override(strategy("volatility"), base, {"close_location": 0.5, "return": 0.001})
    assert undirected.specs == []
    assert "directional_confirmation" in undirected.failed("LONG")
    assert "directional_confirmation" in undirected.failed("SHORT")
    # The expansion itself still passed: high volatility was seen, not traded.
    assert undirected.check("range_expansion", "LONG").passed is True


def test_volatility_needs_prior_compression_and_avoids_extremes():
    _, base = scenario("volatility")
    loose = override(strategy("volatility"), base, {"prior_bollinger_width_percentile": 0.8})
    assert loose.failed("LONG") == ["prior_compression"]
    extreme = override(strategy("volatility"), base, {"realized_volatility_percentile": 0.99})
    assert extreme.failed("LONG") == ["not_extreme"]
    _, regime = scenario("volatility", stored={"regime_label": "EXTREME"})
    assert regime.failed("LONG") == ["volatility_regime"]


# --- Event driven --------------------------------------------------------------------------
def test_event_driven_needs_a_validated_event():
    candles = event_reaction()
    none = run(strategy("event_driven"), candles, stored={"period_news_count": 0.0})
    assert none.specs == []
    assert none.failed("LONG") == ["validated_event"]


def test_event_driven_missing_news_coverage_blocks_rather_than_counting_as_no_news():
    candles = event_reaction()
    result = run(strategy("event_driven"), candles)  # no stored news: UNAVAILABLE
    assert result.outcome is None
    assert set(result.blocked.values()) == {"UNAVAILABLE"}


def test_event_driven_needs_price_and_volume_confirmation():
    _, base = scenario("event_driven")
    flat = override(strategy("event_driven"), base, {"return": 0.002})
    assert flat.specs == []
    assert "price_reaction" in flat.failed("LONG")
    quiet = override(strategy("event_driven"), base, {"volume_ratio": 1.0})
    assert quiet.failed("LONG") == ["volume_reaction"]


def test_event_driven_takes_direction_from_price_not_from_article_text():
    params = strategy("event_driven").default_parameters()
    news = [
        f
        for f in REGISTRY.features.resolve_all(
            strategy("event_driven").required_features(params, "1Day")
        )
        if f.definition.source is FeatureSource.NEWS
    ]
    # Only counts and timing: no text, sentiment or headline input exists.
    assert {f.definition.name for f in news} == {"period_news_count", "news_hours_since_latest"}
    # The same event with a falling reaction is a SHORT, with a rising one a LONG.
    up = run(strategy("event_driven"), event_reaction(), stored=NEWS)
    down = run(strategy("event_driven"), mirror(event_reaction()), stored=NEWS)
    assert [s.direction for s in up.specs] == [Direction.LONG]
    assert [s.direction for s in down.specs] == [Direction.SHORT]


def test_event_driven_reports_the_missing_economic_calendar():
    _, result = scenario("event_driven")
    assert result.outcome is not None
    assert any("NOT_CONFIGURED" in n for n in result.outcome.notes)
    macro = result.check("macro_events", "LONG")
    assert macro.passed is None
    assert "NOT_CONFIGURED" in macro.detail


def test_event_driven_counts_only_configured_categories():
    candles = event_reaction()
    base = run(strategy("event_driven"), candles, stored=NEWS)
    any_key = next(f.key for f in base.resolved if f.param_map.get("category") == "ANY")
    only_other = override(strategy("event_driven"), base, {"period_news_count": 0.0, any_key: 3.0})
    assert only_other.specs == []
    assert only_other.failed("LONG") == ["validated_event"]
