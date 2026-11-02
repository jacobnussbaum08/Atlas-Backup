"""Shared shape of a market-intelligence classification (Phase 3B).

**What the probabilities are.** Each classifier turns measured evidence into a
distribution over its labels (they sum to 1, including ``UNCERTAIN``). These are
*normalized evidence weights* from transparent, configured formulas: they have not been
calibrated against outcomes, so "0.7 bullish" means "most of the configured evidence points
that way", not "a 70 % chance of rising". Nothing here predicts returns or authorizes a trade.

**Uncertainty is explicit.** Missing inputs move probability mass to ``UNCERTAIN``; when
no label reaches the configured confidence floor, the label itself is ``UNCERTAIN``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.market_intelligence.enums import ClassificationQuality, Dimension

UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class Classification:
    dimension: Dimension
    algorithm: str
    label: str
    probabilities: dict[str, float]
    quality: ClassificationQuality
    evidence: dict[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    @property
    def confidence(self) -> float:
        return self.probabilities.get(self.label, 0.0)


def logistic(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def insufficient(
    dimension: Dimension, algorithm: str, labels: tuple[str, ...], reason: str
) -> Classification:
    return Classification(
        dimension=dimension,
        algorithm=algorithm,
        label=UNCERTAIN,
        probabilities={**dict.fromkeys(labels, 0.0), UNCERTAIN: 1.0},
        quality=ClassificationQuality.INSUFFICIENT_DATA,
        reasons=(reason,),
    )


def finalize(
    dimension: Dimension,
    algorithm: str,
    raw: Mapping[str, float],
    *,
    uncertainty: float,
    confidence_floor: float,
    quality: ClassificationQuality,
    evidence: dict[str, Any],
    reasons: tuple[str, ...] = (),
) -> Classification:
    """Scale ``raw`` (summing to 1) by ``1 - uncertainty``, add ``UNCERTAIN``, pick the
    label, and fall back to ``UNCERTAIN`` when no label reaches ``confidence_floor``."""
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("raw probabilities must sum to more than zero")
    keep = 1.0 - clamp(uncertainty)
    probabilities = {k: keep * v / total for k, v in raw.items()}
    probabilities[UNCERTAIN] = 1.0 - sum(probabilities.values())
    best = max(raw, key=lambda k: (probabilities[k], k))
    label = best
    if probabilities[best] < confidence_floor:
        label = UNCERTAIN
        reasons = (
            *reasons,
            f"no label reaches the confidence floor {confidence_floor:.2f} "
            f"(best: {best} {probabilities[best]:.2f})",
        )
    return Classification(
        dimension=dimension,
        algorithm=algorithm,
        label=label,
        probabilities=probabilities,
        quality=quality,
        evidence=evidence,
        reasons=reasons,
    )


def missing_uncertainty(coverage: float, min_coverage: float, weight: float) -> float:
    """Uncertainty from missing bars: 0 at full coverage, ``weight`` at the minimum."""
    if coverage >= 1.0:
        return 0.0
    span = 1.0 - min_coverage
    return weight * (clamp((1.0 - coverage) / span) if span > 0 else 1.0)


__all__ = [
    "UNCERTAIN",
    "Classification",
    "ClassificationQuality",
    "Dimension",
    "clamp",
    "finalize",
    "insufficient",
    "logistic",
    "missing_uncertainty",
]
