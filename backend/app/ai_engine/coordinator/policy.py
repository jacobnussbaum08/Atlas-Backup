"""The decision policy: every weight, threshold and rule the coordinator uses (Phase 5B).

A policy is data. It is validated here, stored content-addressed in
``decision_policy_versions`` and named by every run that used it, so a decision can always
be traced to the exact parameters behind it. Changing anything is a new version.

**The defaults are documented starting points, not optimized values.** No weight below was
fitted: Atlas has no traded or backtested outcomes to fit them on (Phases 7-8), and the
Phase 5A models showed no out-of-sample value. Each value is chosen to be interpretable and
conservative; the reasoning is in ``docs/architecture/decisions.md`` (D-113 onwards).

The **heuristic score** is a weighted mean of [0, 1] component scores over the components
whose inputs existed at the decision time. It orders opportunities. It is **not a
probability** of anything, and nothing derived from it is presented as one.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.feature_engine.registry import sha256_json
from app.strategies.enums import StrategyFamily

LOGIC_VERSION = "coordinator-v1"
DEFAULT_CODE = "atlas-decision-policy"

COMPONENTS = (
    "signal_quality",
    "strategy_agreement",
    "track_record",
    "ml_support",
    "regime_suitability",
    "trend_alignment",
    "volatility_conditions",
    "liquidity",
    "cost_efficiency",
    "reward_risk",
    "news",
    "data_reliability",
)

TREND_LABELS = ("BULLISH", "BEARISH", "SIDEWAYS", "TRANSITIONING", "UNCERTAIN")
VOLATILITY_LABELS = ("LOW", "NORMAL", "ELEVATED", "EXTREME", "UNCERTAIN")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Weights(_Strict):
    """Relative importance of each component. Renormalized over available components."""

    signal_quality: float = Field(0.15, ge=0, le=1)
    strategy_agreement: float = Field(0.10, ge=0, le=1)
    track_record: float = Field(0.10, ge=0, le=1)
    ml_support: float = Field(0.15, ge=0, le=1)
    regime_suitability: float = Field(0.10, ge=0, le=1)
    trend_alignment: float = Field(0.08, ge=0, le=1)
    volatility_conditions: float = Field(0.05, ge=0, le=1)
    liquidity: float = Field(0.05, ge=0, le=1)
    cost_efficiency: float = Field(0.05, ge=0, le=1)
    reward_risk: float = Field(0.10, ge=0, le=1)
    news: float = Field(0.02, ge=0, le=1)
    data_reliability: float = Field(0.05, ge=0, le=1)

    def as_dict(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in COMPONENTS}

    @model_validator(mode="after")
    def _positive_total(self) -> Self:
        if sum(self.as_dict().values()) <= 0:
            raise ValueError("at least one weight must be positive")
        return self


class RegimeSuitability(_Strict):
    """Score of a strategy family under the market trend regime, relative to the proposal's
    direction: ALIGNED (BULLISH for LONG, BEARISH for SHORT), OPPOSED, SIDEWAYS or
    TRANSITIONING. UNCERTAIN or missing regimes make the component UNAVAILABLE."""

    aligned: float = Field(ge=0, le=1)
    opposed: float = Field(ge=0, le=1)
    sideways: float = Field(ge=0, le=1)
    transitioning: float = Field(ge=0, le=1)


def _default_regime_table() -> dict[str, RegimeSuitability]:
    trend = RegimeSuitability(aligned=1.0, opposed=0.0, sideways=0.4, transitioning=0.5)
    return {
        StrategyFamily.MOMENTUM.value: trend,
        StrategyFamily.TREND_FOLLOWING.value: trend,
        StrategyFamily.BREAKOUT.value: RegimeSuitability(
            aligned=1.0, opposed=0.1, sideways=0.5, transitioning=0.6
        ),
        StrategyFamily.MEAN_REVERSION.value: RegimeSuitability(
            aligned=0.6, opposed=0.3, sideways=1.0, transitioning=0.6
        ),
        StrategyFamily.VOLATILITY.value: RegimeSuitability(
            aligned=0.7, opposed=0.3, sideways=0.6, transitioning=0.6
        ),
        StrategyFamily.EVENT_DRIVEN.value: RegimeSuitability(
            aligned=0.6, opposed=0.4, sideways=0.5, transitioning=0.5
        ),
    }


class DecisionPolicy(_Strict):
    code: str = Field(DEFAULT_CODE, pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    description: str = Field(
        "Default Atlas decision policy: documented, conservative starting values; not fitted.",
        max_length=2000,
    )
    weights: Weights = Weights()

    # --- Evidence floors ---------------------------------------------------------------
    # Share of the total weight whose components must be available; below it the
    # opportunity is INSUFFICIENT_DATA rather than scored on a sliver of evidence.
    min_component_coverage: float = Field(0.5, gt=0, le=1)
    min_data_reliability: float = Field(0.5, ge=0, le=1)
    # Proposals whose data cutoff is older than this at the decision time are DEFERRED.
    max_data_age_hours: dict[str, float] = Field(
        default_factory=lambda: {"1Day": 96.0, "1Week": 240.0}
    )

    # --- Ranking ------------------------------------------------------------------------
    prioritize_threshold: float = Field(0.55, ge=0, le=1)
    # How many opportunities a run may put forward for (future) risk review at most.
    max_prioritized: int = Field(5, ge=0, le=50)
    uncertainty_penalty: float = Field(0.3, ge=0, le=1)
    max_uncertainty: float = Field(0.6, ge=0, le=1)
    conflict_penalty: float = Field(0.10, ge=0, le=1)

    # --- Rules --------------------------------------------------------------------------
    # Net reward over net adverse move (both after the cost assumption) below this: REJECT.
    min_reward_risk: float = Field(1.0, ge=0, le=20)
    reward_risk_saturation: float = Field(3.0, gt=0, le=50)
    reject_negative_expected_value: bool = True
    reject_on_opposed_regime: bool = True
    # Market volatility regimes in which every opportunity is DEFERRED.
    defer_market_volatility: tuple[str, ...] = ("EXTREME",)
    defer_on_ml_disagreement: bool = True
    # Opposite-direction opportunities on one asset whose ranking scores differ by less
    # than this are both DEFERRED; otherwise the weaker is DEPRIORITIZED (never merged).
    opposing_margin: float = Field(0.15, ge=0, le=1)
    # Proposals of different strategies whose entries differ by at most this fraction are
    # "nearly identical": one opportunity, counted once for capacity.
    duplicate_entry_tolerance: float = Field(0.01, ge=0, le=0.5)
    # |correlation| at or above this makes two same-exposure opportunities compete; the
    # lower-ranked one is DEPRIORITIZED.
    correlation_threshold: float = Field(0.8, ge=0, le=1)
    decision_ttl_hours: float = Field(24.0, gt=0, le=24 * 30)
    # Context older than this at the decision time is not used: regimes, asset features,
    # correlations, liquidity and ML predictions.
    max_context_age_hours: float = Field(120.0, gt=0, le=24 * 60)

    # --- Component parameters -----------------------------------------------------------
    # Distinct agreeing strategies at which agreement saturates.
    agreement_saturation: int = Field(2, ge=1, le=6)
    regime_suitability: dict[str, RegimeSuitability] = Field(default_factory=_default_regime_table)
    # Component score multiplier when the market volatility regime is EXTREME.
    extreme_volatility_factor: float = Field(0.5, ge=0, le=1)
    # Track record: matured observations needed, and the mean net return mapped to 1.0.
    min_track_record: int = Field(30, ge=5, le=100_000)
    track_record_scale: float = Field(0.02, gt=0, le=1)
    # Liquidity: median daily dollar volume mapped to 0 and to 1 (log scale).
    liquidity_floor_usd: float = Field(1_000_000, gt=0)
    liquidity_full_usd: float = Field(100_000_000, gt=0)
    # Volatility: the asset's volatility percentile above which the score declines.
    high_volatility_percentile: float = Field(0.8, ge=0, le=1)
    # Costs: assumed round trip (commission + spread + slippage) when nothing better is
    # known. An ASSUMPTION: Atlas has no execution cost model before Phase 7.
    assumed_round_trip_cost_bps: float = Field(10.0, ge=0, le=500)
    # ML: authority of an approved model's uncalibrated score or regression estimate
    # relative to a calibrated probability; scale mapping an estimate to the score.
    uncalibrated_authority: float = Field(0.5, ge=0, le=1)
    regression_scale: float = Field(0.02, gt=0, le=1)
    ml_disagreement_margin: float = Field(0.1, ge=0, le=0.5)
    # News: hours of LLM analyses considered, and the |mean sentiment| counted as a
    # contradiction of the proposal's direction.
    news_lookback_hours: float = Field(72.0, gt=0, le=24 * 30)
    news_conflict_threshold: float = Field(0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.liquidity_full_usd <= self.liquidity_floor_usd:
            raise ValueError("liquidity_full_usd must exceed liquidity_floor_usd")
        unknown = [r for r in self.defer_market_volatility if r not in VOLATILITY_LABELS]
        if unknown:
            raise ValueError(f"unknown volatility regime(s): {', '.join(unknown)}")
        missing = [f.value for f in StrategyFamily if f.value not in self.regime_suitability]
        if missing:
            raise ValueError(f"regime_suitability lacks {', '.join(missing)}")
        return self

    def parameters(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def sha256(self) -> str:
        return sha256_json({"logic_version": LOGIC_VERSION, "parameters": self.parameters()})


__all__ = [
    "COMPONENTS",
    "DEFAULT_CODE",
    "LOGIC_VERSION",
    "TREND_LABELS",
    "VOLATILITY_LABELS",
    "DecisionPolicy",
    "RegimeSuitability",
    "Weights",
]
