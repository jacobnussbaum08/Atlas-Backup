"""The standardized proposal (Phase 4A, spec 11.7) and validation of strategy drafts.

A proposal is a hypothesis, not an approved trade: it has no quantity, no order type and
no approval state. Risk approval and sizing belong to later phases (spec 14). The signal
strength is an uncalibrated score in [0, 1] (``StrengthKind.UNCALIBRATED_SCORE``), never a
probability.

The engine turns each :class:`~app.strategies.base.ProposalDraft` into a
:class:`ProposalSpec` only if it passes every check below; one invalid draft fails the
whole evaluation (``ErrorStage.VALIDATION``) rather than storing a partial result.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.feature_engine.registry import FeatureValue
from app.strategies.base import (
    EvaluationContext,
    Evidence,
    ProposalDraft,
    StrategyParameters,
)
from app.strategies.enums import Direction, StrengthKind

PRICE_PLACES = Decimal("0.00000001")  # NUMERIC(20, 8)
STRENGTH_PLACES = Decimal("0.0001")  # NUMERIC(5, 4)
MAX_REASONING = 2000
MAX_EVIDENCE = 32


class ProposalValidationError(ValueError):
    """A strategy returned a draft that cannot be recorded."""


@dataclass(frozen=True)
class ProposalSpec:
    """Everything stored for one proposal (``strategy_proposals``)."""

    id: uuid.UUID
    direction: Direction
    timeframe: str
    signal_at: datetime
    data_cutoff: datetime
    reference_entry: Decimal
    invalidation_level: Decimal | None
    target_reference: Decimal | None
    holding_horizon_periods: int
    signal_strength: Decimal
    strength_kind: StrengthKind
    evidence: list[dict[str, Any]]
    reasoning: str
    expires_at: datetime
    market: str
    feature_snapshot_ids: list[uuid.UUID] = field(default_factory=list)


def _price(name: str, value: Decimal | None, *, required: bool) -> Decimal | None:
    if value is None:
        if required:
            raise ProposalValidationError(f"{name} is required")
        return None
    if not isinstance(value, Decimal):
        raise ProposalValidationError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise ProposalValidationError(f"{name} is not a finite price")
    try:
        quantized = value.quantize(PRICE_PLACES)
    except InvalidOperation as exc:
        raise ProposalValidationError(f"{name} is out of range") from exc
    if quantized <= 0:
        raise ProposalValidationError(f"{name} must be a positive price")
    if quantized.adjusted() >= 12:
        raise ProposalValidationError(f"{name} is out of range")
    return quantized


def _evidence(
    items: Sequence[Evidence], features: Mapping[str, FeatureValue]
) -> list[dict[str, Any]]:
    if not items:
        raise ProposalValidationError("a proposal needs supporting evidence")
    if len(items) > MAX_EVIDENCE:
        raise ProposalValidationError(f"at most {MAX_EVIDENCE} evidence items")
    out = []
    for item in items:
        value = features.get(item.feature_key)
        if value is None:
            raise ProposalValidationError(
                f"evidence {item.feature_key} is not in the evaluation's feature snapshot"
            )
        if not value.valid:
            raise ProposalValidationError(f"evidence {item.feature_key} is {value.status}")
        if not item.role.strip() or not item.note.strip():
            raise ProposalValidationError("evidence needs a role and a note")
        out.append(
            {
                "feature_key": item.feature_key,
                "role": item.role.strip()[:64],
                "note": item.note.strip()[:500],
                "value": value.to_json()["value"],
                "available_at": None
                if value.available_at is None
                else value.available_at.isoformat(),
            }
        )
    return out


def validate_drafts(
    drafts: Sequence[ProposalDraft],
    ctx: EvaluationContext,
    params: StrategyParameters,
    *,
    expires_at: datetime,
    snapshot_ids: Sequence[uuid.UUID],
) -> tuple[list[ProposalSpec], list[str]]:
    """Validated proposals and notes about drafts left out (below the minimum strength)."""
    if expires_at <= ctx.data_cutoff:
        raise ProposalValidationError("the expiry must be after the signal")
    seen: set[Direction] = set()
    specs: list[ProposalSpec] = []
    notes: list[str] = []
    for draft in drafts:
        if not isinstance(draft, ProposalDraft):
            raise ProposalValidationError("a strategy must return ProposalDraft objects")
        direction = Direction(draft.direction)
        if direction not in params.directions:
            raise ProposalValidationError(f"{direction} proposals are not enabled")
        if direction in seen:
            raise ProposalValidationError(f"more than one {direction} proposal")
        seen.add(direction)
        entry = _price("reference_entry", draft.reference_entry, required=True)
        if entry is None:
            raise ProposalValidationError("reference_entry is required")
        invalidation = _price("invalidation_level", draft.invalidation_level, required=False)
        target = _price("target_reference", draft.target_reference, required=False)
        long = direction is Direction.LONG
        if invalidation is not None and (invalidation >= entry if long else invalidation <= entry):
            raise ProposalValidationError(
                f"the invalidation level must be {'below' if long else 'above'} the entry"
            )
        if target is not None and (target <= entry if long else target >= entry):
            raise ProposalValidationError(
                f"the target must be {'above' if long else 'below'} the entry"
            )
        if not isinstance(draft.signal_strength, Decimal) or not draft.signal_strength.is_finite():
            raise ProposalValidationError("signal_strength must be a finite Decimal")
        strength = draft.signal_strength.quantize(STRENGTH_PLACES)
        if not Decimal(0) <= strength <= Decimal(1):
            raise ProposalValidationError("signal_strength must be within [0, 1]")
        reasoning = draft.reasoning.strip()
        if not reasoning:
            raise ProposalValidationError("a reasoning summary is required")
        if len(reasoning) > MAX_REASONING:
            raise ProposalValidationError(f"the reasoning is longer than {MAX_REASONING}")
        evidence = _evidence(draft.evidence, ctx.features)
        if strength < params.min_signal_strength:
            notes.append(
                f"{direction} draft left out: strength {strength} below "
                f"{params.min_signal_strength}"
            )
            continue
        specs.append(
            ProposalSpec(
                id=uuid.uuid4(),
                direction=direction,
                timeframe=ctx.timeframe,
                signal_at=ctx.data_cutoff,
                data_cutoff=ctx.data_cutoff,
                reference_entry=entry,
                invalidation_level=invalidation,
                target_reference=target,
                holding_horizon_periods=params.holding_horizon_periods,
                signal_strength=strength,
                strength_kind=StrengthKind.UNCALIBRATED_SCORE,
                evidence=evidence,
                reasoning=reasoning,
                expires_at=expires_at,
                market=ctx.asset.market,
                feature_snapshot_ids=list(snapshot_ids),
            )
        )
    return specs, notes


__all__ = [
    "MAX_REASONING",
    "ProposalSpec",
    "ProposalValidationError",
    "validate_drafts",
]
