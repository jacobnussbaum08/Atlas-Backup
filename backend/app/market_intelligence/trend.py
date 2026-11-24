"""Trend classification (Phase 3B): BULLISH, BEARISH, SIDEWAYS, TRANSITIONING, UNCERTAIN.

Algorithm ``trend-evidence-v1``, from one feature set (and the set ``change_lookback``
bars earlier, for transitions):

1. **Direction evidence**, each component in [-1, 1]:
   ``ma_alignment`` (fast vs slow average, in ATR-percent units), ``price_vs_slow`` (close
   vs slow average, same units), ``slope`` (t-statistic of the slow-window log-price
   slope), ``structure`` (higher highs / higher lows), ``momentum`` (n-bar return as a
   z-score against realized volatility). The direction score ``D`` is their weighted mean.
2. **Trend strength** from ADX: ``s = logistic((ADX - mid) / width)``.
3. ``bullish = s * logistic(gain * D)``, ``bearish = s * (1 - logistic(gain * D))``,
   ``sideways = 1 - s``.
4. **Transitioning** takes a share of the mass when the components disagree (weighted
   spread around ``D``) or the direction flipped since the earlier set.
5. Missing bars move mass to ``UNCERTAIN``; see ``classification``.
"""

from __future__ import annotations

import math

from app.feature_engine.features import FeatureQuality, FeatureSet, TimeframeSpec
from app.market_intelligence.classification import (
    Classification,
    ClassificationQuality,
    Dimension,
    clamp,
    finalize,
    insufficient,
    logistic,
    missing_uncertainty,
)
from app.market_intelligence.config import TrendConfig

ALGORITHM = "trend-evidence-v1"
BULLISH, BEARISH, SIDEWAYS, TRANSITIONING = "BULLISH", "BEARISH", "SIDEWAYS", "TRANSITIONING"
LABELS = (BULLISH, BEARISH, SIDEWAYS, TRANSITIONING)


def components(features: FeatureSet, spec: TimeframeSpec, cfg: TrendConfig) -> dict[str, float]:
    """Direction evidence in [-1, 1]; components whose inputs are missing are left out."""
    get = features.get
    out: dict[str, float] = {}
    atr_pct = get("atr_pct")
    if atr_pct:
        scale = cfg.ma_scale_atr * atr_pct
        alignment, distance = get("sma_fast_vs_slow"), get("close_vs_sma_slow")
        if alignment is not None:
            out["ma_alignment"] = math.tanh(alignment / scale)
        if distance is not None:
            out["price_vs_slow"] = math.tanh(distance / scale)
    slope_t = get("slow_slope_t")
    if slope_t is not None:
        out["slope"] = math.tanh(slope_t / cfg.slope_t_scale)
    structure = get("structure_score")
    if structure is not None:
        out["structure"] = structure
    change, volatility = get("roc"), get("realized_volatility")
    if change is not None and volatility:
        per_bar = volatility / math.sqrt(spec.periods_per_year)
        z = math.log1p(change) / (per_bar * math.sqrt(spec.roc))
        out["momentum"] = math.tanh(z / cfg.momentum_z_scale)
    return out


def direction(parts: dict[str, float], cfg: TrendConfig) -> tuple[float, float]:
    """(weighted direction score D, weighted spread of the components around D)."""
    weights = {k: float(cfg.weights.get(k, 0)) for k in parts}
    total = sum(weights.values())
    if total <= 0:
        return 0.0, 0.0
    score = sum(weights[k] * v for k, v in parts.items()) / total
    spread = math.sqrt(sum(weights[k] * (v - score) ** 2 for k, v in parts.items()) / total)
    return score, spread


def classify_trend(
    current: FeatureSet,
    previous: FeatureSet | None,
    spec: TimeframeSpec,
    cfg: TrendConfig,
) -> Classification:
    if current.quality is FeatureQuality.INSUFFICIENT_DATA:
        return insufficient(Dimension.TREND, ALGORITHM, LABELS, _reason(current))
    parts = components(current, spec, cfg)
    adx = current.get("adx")
    if len(parts) < cfg.min_components or adx is None:
        return insufficient(
            Dimension.TREND,
            ALGORITHM,
            LABELS,
            f"{len(parts)} of 5 direction components and "
            f"{'no' if adx is None else 'an'} ADX available",
        )
    score, spread = direction(parts, cfg)
    strength = logistic((adx - cfg.adx_mid) / cfg.adx_width)
    up = logistic(cfg.direction_gain * score)

    previous_score: float | None = None
    flip = 0.0
    if previous is not None and previous.quality is not FeatureQuality.INSUFFICIENT_DATA:
        earlier = components(previous, spec, cfg)
        if len(earlier) >= cfg.min_components:
            previous_score = direction(earlier, cfg)[0]
            if previous_score * score < 0 and max(abs(previous_score), abs(score)) >= cfg.flip_min:
                flip = clamp(abs(score - previous_score) / 2.0)
    transition = min(cfg.max_transition, cfg.disagreement_weight * spread + cfg.flip_weight * flip)
    raw = {
        BULLISH: (1 - transition) * strength * up,
        BEARISH: (1 - transition) * strength * (1 - up),
        SIDEWAYS: (1 - transition) * (1 - strength),
        TRANSITIONING: transition,
    }
    uncertainty = missing_uncertainty(current.coverage, spec.min_coverage, cfg.missing_uncertainty)
    quality = (
        ClassificationQuality.OK
        if current.quality is FeatureQuality.OK
        else ClassificationQuality.PARTIAL
    )
    reasons = current.notes
    if len(parts) < 5:
        reasons = (*reasons, f"direction from {len(parts)} of 5 components")
    return finalize(
        Dimension.TREND,
        ALGORITHM,
        raw,
        uncertainty=uncertainty,
        confidence_floor=cfg.confidence_floor,
        quality=quality,
        evidence={
            "components": parts,
            "direction_score": score,
            "component_spread": spread,
            "previous_direction_score": previous_score,
            "flip": flip,
            "adx": adx,
            "trend_strength": strength,
            "plus_di": current.get("plus_di"),
            "minus_di": current.get("minus_di"),
            "coverage": current.coverage,
        },
        reasons=reasons,
    )


def _reason(features: FeatureSet) -> str:
    return features.notes[0] if features.notes else "insufficient data"


__all__ = ["ALGORITHM", "LABELS", "classify_trend", "components", "direction"]
