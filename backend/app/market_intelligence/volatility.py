"""Volatility classification (Phase 3B): LOW, NORMAL, ELEVATED, EXTREME, UNCERTAIN.

Algorithm ``volatility-percentile-v1``, independent of trend direction:

* The current realized volatility is ranked against the instrument's **own** recent
  rolling volatility (``volatility_percentile``). The classes are therefore relative: a
  LOW reading for a volatile stock can exceed a HIGH reading for an index fund. The
  absolute annualized figure is reported alongside.
* Soft class boundaries: with ``a_c = logistic((p - c) / softness)`` at each cut point,
  ``low = 1 - a_low``, ``normal = a_low - a_elevated``, ``elevated = a_elevated - a_extreme``,
  ``extreme = a_extreme``. Near a boundary both neighbours share the probability.
* The range state (ATR short / long) and a change of class since the earlier feature set
  are reported as evidence (``range_state``, ``regime_change``).

Large moves are measurements, not errors: data validation happened at ingestion (2B), and
nothing here rejects or clips an observation for being extreme.
"""

from __future__ import annotations

from app.feature_engine.features import FeatureQuality, FeatureSet, TimeframeSpec
from app.market_intelligence.classification import (
    Classification,
    ClassificationQuality,
    Dimension,
    finalize,
    insufficient,
    logistic,
    missing_uncertainty,
)
from app.market_intelligence.config import VolatilityConfig

ALGORITHM = "volatility-percentile-v1"
LOW, NORMAL, ELEVATED, EXTREME = "LOW", "NORMAL", "ELEVATED", "EXTREME"
LABELS = (LOW, NORMAL, ELEVATED, EXTREME)
EXPANDING, CONTRACTING, STEADY = "EXPANDING", "CONTRACTING", "STEADY"


def class_shares(percentile: float, cfg: VolatilityConfig) -> dict[str, float]:
    a_low = logistic((percentile - cfg.low_below) / cfg.softness)
    a_elevated = logistic((percentile - cfg.elevated_above) / cfg.softness)
    a_extreme = logistic((percentile - cfg.extreme_above) / cfg.softness)
    return {
        LOW: 1.0 - a_low,
        NORMAL: max(0.0, a_low - a_elevated),
        ELEVATED: max(0.0, a_elevated - a_extreme),
        EXTREME: a_extreme,
    }


def range_state(ratio: float | None, cfg: VolatilityConfig) -> str | None:
    if ratio is None:
        return None
    if ratio >= cfg.expansion_ratio:
        return EXPANDING
    if ratio <= cfg.contraction_ratio:
        return CONTRACTING
    return STEADY


def _top(percentile: float, cfg: VolatilityConfig) -> str:
    shares = class_shares(percentile, cfg)
    return max(shares, key=lambda k: shares[k])


def classify_volatility(
    current: FeatureSet,
    previous: FeatureSet | None,
    spec: TimeframeSpec,
    cfg: VolatilityConfig,
) -> Classification:
    if current.quality is FeatureQuality.INSUFFICIENT_DATA:
        return insufficient(Dimension.VOLATILITY, ALGORITHM, LABELS, _reason(current))
    percentile = current.get("volatility_percentile")
    if percentile is None:
        return insufficient(
            Dimension.VOLATILITY, ALGORITHM, LABELS, "no realized-volatility percentile"
        )
    previous_class = None
    previous_percentile = None
    if previous is not None and previous.quality is not FeatureQuality.INSUFFICIENT_DATA:
        previous_percentile = previous.get("volatility_percentile")
        if previous_percentile is not None:
            previous_class = _top(previous_percentile, cfg)
    current_class = _top(percentile, cfg)
    uncertainty = missing_uncertainty(current.coverage, spec.min_coverage, cfg.missing_uncertainty)
    quality = (
        ClassificationQuality.OK
        if current.quality is FeatureQuality.OK
        else ClassificationQuality.PARTIAL
    )
    return finalize(
        Dimension.VOLATILITY,
        ALGORITHM,
        class_shares(percentile, cfg),
        uncertainty=uncertainty,
        confidence_floor=cfg.confidence_floor,
        quality=quality,
        evidence={
            "volatility_percentile": percentile,
            "realized_volatility": current.get("realized_volatility"),
            "atr_pct": current.get("atr_pct"),
            "bollinger_width": current.get("bollinger_width"),
            "bollinger_width_percentile": current.get("bollinger_width_percentile"),
            "range_ratio": current.get("range_ratio"),
            "range_state": range_state(current.get("range_ratio"), cfg),
            "previous_percentile": previous_percentile,
            "previous_class": previous_class,
            "regime_change": previous_class is not None and previous_class != current_class,
            "coverage": current.coverage,
            "basis": "percentile against the instrument's own recent rolling volatility",
        },
        reasons=current.notes,
    )


def _reason(features: FeatureSet) -> str:
    return features.notes[0] if features.notes else "insufficient data"


__all__ = ["ALGORITHM", "LABELS", "class_shares", "classify_volatility", "range_state"]
