"""Shared building blocks of the six Phase 4B strategies.

Strategies compute nothing from raw prices: every number comes from a registry feature
(``EvaluationContext.feature``). What is shared here is bookkeeping and arithmetic on
those features:

* :class:`Assessment`: the conditions evaluated for one direction. Every condition is
  evaluated and recorded (passed, failed or informational) so an evaluation without a
  proposal still says exactly why; passed conditions become the proposal's evidence.
* :func:`eligible_directions`: configured directions minus those the asset was not
  eligible for at the cutoff (recorded as failed ``eligibility`` checks).
* :func:`make_levels`: entry, invalidation and target as Decimals, rejecting invalid
  price relationships (non-positive, wrong side, collapsed by rounding).
* :func:`strength`: an uncalibrated score in [0, 1], the mean of components each already
  clamped to [0, 1]. It is a ranking aid, not a probability, and nothing here has been
  validated against outcomes (formal backtesting is Phase 8).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.feature_engine.registry import FeatureValue
from app.strategies.base import (
    Check,
    EvaluationContext,
    Evidence,
    ProposalDraft,
    StrategyOutcome,
    StrategyParameters,
)
from app.strategies.enums import Direction
from app.strategies.proposals import MAX_EVIDENCE, MAX_REASONING

PRICE_QUANTUM = Decimal("0.0001")
STRENGTH_QUANTUM = Decimal("0.0001")


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if math.isnan(value):
        return low
    return max(low, min(high, value))


def fmt(value: float, digits: int = 4) -> str:
    return f"{value:.{digits}g}" if abs(value) < 1e4 else f"{value:,.2f}"


def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def sign(direction: Direction) -> int:
    return 1 if direction is Direction.LONG else -1


def number(feature: FeatureValue) -> float:
    """The feature's numeric value (the engine only calls a strategy when every required
    feature is VALID; optional ones must be checked with :func:`optional_number`)."""
    if not feature.valid or not isinstance(feature.value, float):
        raise ValueError(f"{feature.key} has no valid numeric value")
    return feature.value


def optional_number(feature: FeatureValue) -> float | None:
    return feature.value if feature.valid and isinstance(feature.value, float) else None


def optional_label(feature: FeatureValue) -> str | None:
    return feature.value if feature.valid and isinstance(feature.value, str) else None


class Assessment:
    """The conditions evaluated for one direction."""

    def __init__(self, direction: Direction) -> None:
        self.direction = direction
        self.checks: list[Check] = []
        self._evidence: dict[tuple[str, str], Evidence] = {}

    def require(
        self,
        name: str,
        passed: bool,
        detail: str,
        *features: FeatureValue,
        role: str = "confirmation",
    ) -> bool:
        self.checks.append(
            Check(name, bool(passed), detail, self.direction, tuple(f.key for f in features))
        )
        if passed:
            for f in features:
                self._add(f, role, f"{name}: {detail}")
        return bool(passed)

    def inform(self, name: str, detail: str, *features: FeatureValue) -> None:
        """An informational condition: recorded, never blocking; valid features become
        context evidence."""
        self.checks.append(
            Check(name, None, detail, self.direction, tuple(f.key for f in features))
        )
        for f in features:
            if f.valid:
                self._add(f, "context", f"{name}: {detail}")

    def _add(self, feature: FeatureValue, role: str, note: str) -> None:
        key = (feature.key, role)
        if key not in self._evidence and len(self._evidence) < MAX_EVIDENCE:
            self._evidence[key] = Evidence(feature.key, role, note[:500])

    @property
    def passed(self) -> bool:
        return all(c.passed is not False for c in self.checks)

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if c.passed is False]

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(self._evidence.values())

    def reasoning(self, title: str) -> str:
        details = [f"{c.name}: {c.detail}" for c in self.checks if c.passed]
        text = f"{title} {self.direction.value}: " + "; ".join(details) + "."
        return text[:MAX_REASONING]


def eligible_directions(
    ctx: EvaluationContext, params: StrategyParameters
) -> tuple[list[Direction], list[Check]]:
    """Configured directions the asset was eligible for, and checks for the others."""
    allowed: list[Direction] = []
    checks: list[Check] = []
    for direction in (Direction.LONG, Direction.SHORT):
        if direction not in params.directions:
            checks.append(Check("direction_enabled", None, "disabled by configuration", direction))
            continue
        reason = ctx.direction_block(direction)
        if reason is None:
            allowed.append(direction)
        else:
            checks.append(Check("eligibility", False, reason, direction))
    return allowed, checks


def price(value: float) -> Decimal:
    return Decimal(repr(value)).quantize(PRICE_QUANTUM)


@dataclass(frozen=True)
class Levels:
    entry: Decimal
    invalidation: Decimal
    target: Decimal | None
    description: dict[str, Any]


def make_levels(
    direction: Direction,
    *,
    entry: float,
    invalidation: float,
    target: float | None,
    methods: Mapping[str, str],
    atr: float,
    horizon_periods: int,
    setup: str | None = None,
) -> Levels | str:
    """Validated levels, or why they are invalid. LONG: invalidation < entry < target;
    SHORT: the reverse. Every level must be a positive, finite price that stays distinct
    from the entry after rounding to the stored precision."""
    values = {"entry": entry, "invalidation": invalidation}
    if target is not None:
        values["target"] = target
    for name, value in values.items():
        if not math.isfinite(value) or value <= 0:
            return f"{name} {fmt(value)} is not a positive price"
    e, i = price(entry), price(invalidation)
    t = None if target is None else price(target)
    s = sign(direction)
    if (e - i) * s <= 0:
        side = "below" if s > 0 else "above"
        return f"invalidation {i} is not {side} the entry {e}"
    if t is not None and (t - e) * s <= 0:
        side = "above" if s > 0 else "below"
        return f"target {t} is not {side} the entry {e}"
    risk = abs(e - i)
    reward = None if t is None else abs(t - e)
    description: dict[str, Any] = {
        "entry_method": methods["entry"],
        "invalidation_method": methods["invalidation"],
        "target_method": methods.get("target"),
        "atr": fmt(atr, 6),
        "risk_per_unit": str(risk),
        "reward_per_unit": None if reward is None else str(reward),
        "reward_risk": None if reward is None else str((reward / risk).quantize(Decimal("0.01"))),
        "holding_horizon_periods": horizon_periods,
        "setup": setup,
    }
    return Levels(e, i, t, description)


def strength(*components: float) -> Decimal:
    """Uncalibrated score: the mean of components in [0, 1] (not a probability)."""
    if not components:
        return Decimal(0)
    mean = sum(clamp(c) for c in components) / len(components)
    return Decimal(repr(mean)).quantize(STRENGTH_QUANTUM)


def draft(
    assessment: Assessment,
    levels: Levels,
    score: Decimal,
    title: str,
) -> ProposalDraft:
    return ProposalDraft(
        direction=assessment.direction,
        reference_entry=levels.entry,
        invalidation_level=levels.invalidation,
        target_reference=levels.target,
        signal_strength=score,
        reasoning=assessment.reasoning(title),
        evidence=assessment.evidence,
        levels=levels.description,
    )


def conclude(
    assessments: Sequence[Assessment],
    proposals: Iterable[ProposalDraft],
    *,
    checks: Sequence[Check] = (),
    notes: Sequence[str] = (),
) -> StrategyOutcome:
    """The outcome: proposals, every check recorded and, without a proposal, why."""
    drafts = tuple(proposals)
    recorded = [*checks, *(c for a in assessments for c in a.checks)]
    reason = None
    if not drafts:
        parts = [
            f"{a.direction.value} failed {', '.join(a.failed)}" for a in assessments if a.failed
        ]
        if not assessments:
            parts = ["no direction was eligible or enabled"]
        reason = "; ".join(parts) or "conditions not met"
    return StrategyOutcome(
        proposals=drafts, notes=tuple(notes), checks=tuple(recorded), reason=reason
    )


def invalid_data(
    *, close: float, atr: float | None = None, prices: Mapping[str, float] | None = None
) -> str | None:
    """Inconsistencies VALID features can still carry (bad source data)."""
    if close <= 0:
        return f"non-positive close {fmt(close)}"
    if atr is not None:
        if atr <= 0:
            return f"non-positive ATR {fmt(atr)}"
        if atr >= close:
            return f"ATR {fmt(atr)} is not smaller than the close {fmt(close)}"
    for name, value in (prices or {}).items():
        if value <= 0:
            return f"non-positive {name} {fmt(value)}"
    return None


def assess_all(
    directions: Sequence[Direction], assess: Callable[[Direction], Assessment]
) -> list[Assessment]:
    return [assess(d) for d in directions]


__all__ = [
    "Assessment",
    "Levels",
    "assess_all",
    "clamp",
    "conclude",
    "draft",
    "eligible_directions",
    "fmt",
    "invalid_data",
    "make_levels",
    "number",
    "optional_label",
    "optional_number",
    "pct",
    "price",
    "sign",
    "strength",
]
