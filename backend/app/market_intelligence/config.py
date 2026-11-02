"""Market intelligence configuration (Phase 3B).

Two layers, as in Phase 3A:

* ``IntelligenceConfig``: every window, weight and threshold the algorithms use. It is
  versioned database configuration (``intelligence_configs``): a change is a new version,
  and every stored result names the version it used.
* ``MarketIntelligenceSettings`` (``MARKET_INTELLIGENCE_*``): operational knobs only.

**The defaults are starting points, not optimized or validated values.** Each one states
its rationale (decision D-067); none has been fitted to returns, and none implies that a
classification predicts anything.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)
from app.data_engine.enums import PriceAdjustment
from app.feature_engine.features import TimeframeSpec

ENV_PREFIX = "MARKET_INTELLIGENCE_"
DEFAULT_CONFIG_CODE = "market-intelligence-default"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TrendConfig(_Frozen):
    # Evidence weights (normalized over the components that are available).
    weights: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "ma_alignment": Decimal("0.20"),
            "price_vs_slow": Decimal("0.20"),
            "slope": Decimal("0.20"),
            "structure": Decimal("0.15"),
            "momentum": Decimal("0.25"),
        }
    )
    # Distances from averages are measured in ATR-percent units: 1 ATR = tanh(1) = 0.76.
    ma_scale_atr: float = Field(default=1.0, gt=0)
    # Slope t-statistic giving tanh(1) = 0.76 of full evidence.
    slope_t_scale: float = Field(default=4.0, gt=0)
    # Momentum as a z-score of the n-bar return against the realized volatility.
    momentum_z_scale: float = Field(default=1.5, gt=0)
    # ADX logistic midpoint and width: Wilder's commonly cited 20-25 trend threshold.
    adx_mid: float = Field(default=20.0, gt=0)
    adx_width: float = Field(default=4.0, gt=0)
    # Steepness turning the direction score (-1..1) into an up/down split.
    direction_gain: float = Field(default=3.0, gt=0)
    disagreement_weight: float = Field(default=0.6, ge=0, le=1)
    flip_min: float = Field(default=0.2, ge=0, le=1)
    flip_weight: float = Field(default=0.5, ge=0, le=1)
    max_transition: float = Field(default=0.6, ge=0, le=1)
    missing_uncertainty: float = Field(default=0.5, ge=0, le=1)
    confidence_floor: float = Field(default=0.40, ge=0, le=1)
    min_components: int = Field(default=3, ge=1, le=5)

    @field_validator("weights")
    @classmethod
    def _known(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        unknown = set(value) - {"ma_alignment", "price_vs_slow", "slope", "structure", "momentum"}
        if unknown:
            raise ValueError(f"unknown trend components: {sorted(unknown)}")
        if sum(value.values(), Decimal(0)) <= 0:
            raise ValueError("trend weights must sum to more than zero")
        return value


class VolatilityConfig(_Frozen):
    # Cut points on the volatility percentile against the instrument's own history:
    # quartiles for low/normal, the 95th percentile for extreme (a 1-in-20 condition).
    low_below: float = Field(default=0.25, gt=0, lt=1)
    elevated_above: float = Field(default=0.75, gt=0, lt=1)
    extreme_above: float = Field(default=0.95, gt=0, lt=1)
    softness: float = Field(default=0.03, gt=0, le=0.2)
    # ATR(short) / ATR(long): +25 % range expansion, -20 % contraction.
    expansion_ratio: float = Field(default=1.25, gt=1)
    contraction_ratio: float = Field(default=0.80, gt=0, lt=1)
    missing_uncertainty: float = Field(default=0.5, ge=0, le=1)
    confidence_floor: float = Field(default=0.40, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> VolatilityConfig:
        if not self.low_below < self.elevated_above < self.extreme_above:
            raise ValueError("volatility cut points must increase")
        return self


class StabilityConfig(_Frozen):
    weights: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "volatility": Decimal("0.30"),
            "expansion": Decimal("0.15"),
            "drawdown": Decimal("0.20"),
            "correlation": Decimal("0.15"),
            "participation": Decimal("0.20"),
        }
    )
    # A 10 % drawdown from the window high counts as full drawdown stress.
    drawdown_full: float = Field(default=0.10, gt=0)
    # Average pairwise correlation: 0.3 ordinary, 0.7 everything moving together.
    correlation_low: float = Field(default=0.3, ge=-1, le=1)
    correlation_high: float = Field(default=0.7, ge=-1, le=1)
    # Share of assets above their slow average: below 50 % weakens, 20 % is full stress.
    participation_mid: float = Field(default=0.5, gt=0, le=1)
    participation_span: float = Field(default=0.3, gt=0, le=1)
    stable_below: float = Field(default=0.35, gt=0, lt=1)
    stressed_above: float = Field(default=0.60, gt=0, lt=1)
    softness: float = Field(default=0.07, gt=0, le=0.3)
    min_evidence: int = Field(default=2, ge=1, le=5)
    confidence_floor: float = Field(default=0.40, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> StabilityConfig:
        if self.stable_below >= self.stressed_above:
            raise ValueError("stable_below must be below stressed_above")
        if self.correlation_low >= self.correlation_high:
            raise ValueError("correlation_low must be below correlation_high")
        return self


class ParticipationConfig(_Frozen):
    # Fewer evaluated members than this: no participation figures (too few to mean much).
    min_members: int = Field(default=20, ge=2)
    # Share of members (by count) whose moves count as "the top" for concentration.
    concentration_top_share: float = Field(default=0.1, gt=0, lt=1)


class CorrelationConfig(_Frozen):
    # Daily log returns: ~3 months, compared with the ~3 months before.
    window: int = Field(default=60, ge=10, le=500)
    prior_window: int = Field(default=60, ge=10, le=500)
    # Pairs with fewer overlapping returns get no coefficient at all.
    min_overlap: int = Field(default=45, ge=10)
    # Matrix size bound (n * (n - 1) / 2 pairs per run).
    max_assets: int = Field(default=40, ge=2, le=150)
    # Two-sided 95 % interval (Fisher z).
    confidence_z: float = Field(default=1.96, gt=0)

    @model_validator(mode="after")
    def _overlap(self) -> CorrelationConfig:
        if self.min_overlap > self.window:
            raise ValueError("min_overlap cannot exceed window")
        return self


class IntelligenceConfig(_Frozen):
    timeframes: list[TimeframeSpec]
    trend: TrendConfig = Field(default_factory=TrendConfig)
    volatility: VolatilityConfig = Field(default_factory=VolatilityConfig)
    stability: StabilityConfig = Field(default_factory=StabilityConfig)
    participation: ParticipationConfig = Field(default_factory=ParticipationConfig)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)

    @model_validator(mode="after")
    def _daily_present(self) -> IntelligenceConfig:
        codes = [t.timeframe for t in self.timeframes]
        if "1Day" not in codes:
            raise ValueError("the 1Day timeframe is required (participation and correlation)")
        if len(set(codes)) != len(codes):
            raise ValueError("each timeframe at most once")
        return self

    def spec(self, timeframe: str) -> TimeframeSpec:
        return next(t for t in self.timeframes if t.timeframe == timeframe)


DAILY = TimeframeSpec(
    timeframe="1Day",
    periods_per_year=252,
    fast_ma=20,  # ~1 month
    slow_ma=50,  # ~1 quarter
    adx=14,  # Wilder's default
    roc=20,
    relative_strength=20,
    atr=14,  # Wilder's default
    atr_short=5,  # ~1 week
    volatility=20,
    volatility_history=120,  # ~6 months of rolling 20-session volatility
    bollinger=20,  # Bollinger's default
    structure_bars=40,
    structure_segments=4,
    change_lookback=5,  # one week earlier
    min_coverage=0.9,  # as the Phase 3A history rule
)
WEEKLY = TimeframeSpec(
    timeframe="1Week",
    periods_per_year=52,
    fast_ma=10,  # ~1 quarter
    slow_ma=30,  # ~7 months
    adx=14,
    roc=13,
    relative_strength=13,
    atr=14,
    atr_short=4,
    volatility=13,
    volatility_history=26,
    bollinger=20,
    structure_bars=24,
    structure_segments=4,
    change_lookback=4,
    min_coverage=0.9,
)

DEFAULT_CONFIGS: dict[str, tuple[str, IntelligenceConfig]] = {
    DEFAULT_CONFIG_CODE: (
        "Initial market intelligence configuration: conventional indicator windows (Wilder, "
        "Bollinger), evidence weights and cut points chosen for interpretability. Not "
        "optimized or validated against outcomes.",
        IntelligenceConfig(timeframes=[DAILY, WEEKLY]),
    )
}


class MarketIntelligenceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # Versioned configuration code to use.
    config: str = DEFAULT_CONFIG_CODE
    # Broad US equity-market reference (an Atlas symbol with daily history).
    benchmark_symbol: str = "SPY"
    # Daily-bar series the engine reads (D-063: split-adjusted, as qualification).
    price_adjustment: PriceAdjustment = PriceAdjustment.SPLIT
    # Before a live run, re-retrieve the whole history window of every in-scope instrument
    # in one request each, so an adjusted series is consistent across the window (D-063).
    prepare_history: bool = True
    history_lookback_days: int = Field(default=420, ge=120, le=3650)
    history_prep_max_assets: int = Field(default=150, ge=0, le=1000)
    # Reconstruction: past sessions recomputed per run, and the delay after the close at
    # which a session's daily bar is treated as available (settled).
    reconstruction_sessions: int = Field(default=120, ge=1, le=750)
    settle_seconds: int = Field(default=3600, ge=0, le=86_400)

    @field_validator("price_adjustment", mode="before")
    @classmethod
    def _normalise_adjustment(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper().replace("-", "_")
        return value


@lru_cache(maxsize=1)
def get_market_intelligence_settings() -> MarketIntelligenceSettings:
    """Load once; unknown ``MARKET_INTELLIGENCE_*`` keys are rejected as typos."""
    known = {f"{ENV_PREFIX}{name}" for name in MarketIntelligenceSettings.model_fields}
    reject_unknown_keys((ENV_PREFIX,), known)
    try:
        return MarketIntelligenceSettings()
    except ValidationError as exc:
        raise InvalidSettingsError(
            describe_validation_error(exc, "Market intelligence settings")
        ) from None


__all__ = [
    "DAILY",
    "DEFAULT_CONFIGS",
    "DEFAULT_CONFIG_CODE",
    "WEEKLY",
    "CorrelationConfig",
    "IntelligenceConfig",
    "MarketIntelligenceSettings",
    "ParticipationConfig",
    "StabilityConfig",
    "TrendConfig",
    "VolatilityConfig",
    "get_market_intelligence_settings",
]
