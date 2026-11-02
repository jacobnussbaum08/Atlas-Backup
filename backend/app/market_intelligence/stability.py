"""Market stability (Phase 3B): STABLE, UNSETTLED, STRESSED, UNCERTAIN.

Algorithm ``stability-stress-v1``. A separate dimension from trend and volatility: a
market can trend up while stressed, or be calm while drifting down. Evidence, each in
[0, 1] where 1 is stress, from the benchmark and the qualified universe:

* ``volatility``: elevated + extreme share of the benchmark's volatility classification.
* ``expansion``: benchmark range expansion, ``(ATR short/long - 1) / (expansion - 1)``.
* ``drawdown``: benchmark drawdown from its window high, relative to ``drawdown_full``.
* ``correlation``: average pairwise correlation between ``correlation_low`` and ``_high``
  (assets moving together: diversification is low).
* ``participation``: share of members above their slow average, below ``participation_mid``.

The stress score ``S`` is their weighted mean over the available ones; then
``stable = logistic((stable_below - S) / softness)``,
``stressed = logistic((S - stressed_above) / softness)``, ``unsettled`` the rest. Missing
evidence moves mass to UNCERTAIN in proportion to its configured weight.
"""

from __future__ import annotations

from app.market_intelligence.classification import (
    Classification,
    ClassificationQuality,
    Dimension,
    clamp,
    finalize,
    insufficient,
    logistic,
)
from app.market_intelligence.config import StabilityConfig, VolatilityConfig
from app.market_intelligence.volatility import ELEVATED, EXTREME

ALGORITHM = "stability-stress-v1"
STABLE, UNSETTLED, STRESSED = "STABLE", "UNSETTLED", "STRESSED"
LABELS = (STABLE, UNSETTLED, STRESSED)


def stress_evidence(
    *,
    benchmark_volatility: Classification | None,
    range_ratio: float | None,
    drawdown: float | None,
    average_correlation: float | None,
    above_slow_ma: float | None,
    cfg: StabilityConfig,
    volatility_cfg: VolatilityConfig,
) -> dict[str, float]:
    out: dict[str, float] = {}
    if benchmark_volatility is not None and benchmark_volatility.label != "UNCERTAIN":
        p = benchmark_volatility.probabilities
        out["volatility"] = clamp(p.get(ELEVATED, 0.0) + p.get(EXTREME, 0.0))
    if range_ratio is not None:
        out["expansion"] = clamp((range_ratio - 1.0) / (volatility_cfg.expansion_ratio - 1.0))
    if drawdown is not None:
        out["drawdown"] = clamp(-drawdown / cfg.drawdown_full)
    if average_correlation is not None:
        span = cfg.correlation_high - cfg.correlation_low
        out["correlation"] = clamp((average_correlation - cfg.correlation_low) / span)
    if above_slow_ma is not None:
        out["participation"] = clamp(
            (cfg.participation_mid - above_slow_ma) / cfg.participation_span
        )
    return out


def classify_stability(evidence: dict[str, float], cfg: StabilityConfig) -> Classification:
    weights = {k: float(v) for k, v in cfg.weights.items()}
    available = {k: v for k, v in evidence.items() if weights.get(k, 0) > 0}
    if len(available) < cfg.min_evidence:
        return insufficient(
            Dimension.STABILITY,
            ALGORITHM,
            LABELS,
            f"{len(available)} stress inputs available; at least {cfg.min_evidence} needed",
        )
    used = sum(weights[k] for k in available)
    total = sum(weights.values())
    score = sum(weights[k] * v for k, v in available.items()) / used
    stable = logistic((cfg.stable_below - score) / cfg.softness)
    stressed = logistic((score - cfg.stressed_above) / cfg.softness)
    raw = {STABLE: stable, STRESSED: stressed, UNSETTLED: max(0.0, 1.0 - stable - stressed)}
    missing = [k for k in weights if k not in available and weights[k] > 0]
    return finalize(
        Dimension.STABILITY,
        ALGORITHM,
        raw,
        uncertainty=1.0 - used / total,
        confidence_floor=cfg.confidence_floor,
        quality=ClassificationQuality.OK if not missing else ClassificationQuality.PARTIAL,
        evidence={"stress_score": score, "inputs": available, "missing_inputs": missing},
        reasons=tuple(f"stress input '{k}' unavailable" for k in missing),
    )


__all__ = ["ALGORITHM", "LABELS", "classify_stability", "stress_evidence"]
