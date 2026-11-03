"""The strategy interface (Phase 4A, spec section 11).

A strategy is a class with declarative metadata and two methods:

* ``required_features(params, timeframe)``: the features it reads, as registry requests.
  The engine computes them point in time and calls the strategy only when every required
  one is ``VALID``; a strategy never sees missing or future data and never reads the
  database, the clock or any execution API.
* ``evaluate(ctx, params)``: returns zero or more :class:`ProposalDraft` hypotheses. The
  engine validates them, adds identity, timestamps, feature snapshots and expiry, and
  stores them. Drafts carry no quantity: sizing belongs to the risk engine (spec 14.2).

Identity and versioning: ``strategy_id`` is stable; ``logic_version`` names the behaviour
of ``evaluate`` and must change whenever that code changes. A stored strategy version is
(logic version, parameter configuration); changing either creates a new immutable version
(``app.strategies.catalog``).

Adding a strategy means adding one module under ``app.strategies.families`` exposing
``STRATEGY``; no other strategy module changes (``app.strategies.registry``).
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.database.models import AssetClass
from app.feature_engine.registry import (
    BAR_TIMEFRAMES,
    REGISTRY,
    FeatureRegistry,
    FeatureRequest,
    FeatureValue,
)
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.enums import Direction, EvaluationMode, StrategyFamily, TriggerKind

STRATEGY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,47}$")


class StrategyDefinitionError(ValueError):
    """A strategy class or its parameters are invalid."""


class StrategyNotImplementedError(NotImplementedError):
    """The strategy has an interface but no evaluation logic yet (Phase 4B)."""


class StrategyParameters(BaseModel):
    """Parameters every strategy has. Families extend this with their own fields.

    Frozen and strict: unknown keys are rejected, so a typo cannot silently fall back to a
    default. Stored as JSON in ``strategy_configurations``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeframes: tuple[str, ...] = Field(
        default=("1Day",), min_length=1, description="timeframes the strategy is evaluated on"
    )
    directions: tuple[Direction, ...] = Field(
        default=(Direction.LONG, Direction.SHORT),
        min_length=1,
        description="directions it may propose",
    )
    signal_ttl_periods: int = Field(
        default=1,
        ge=1,
        le=20,
        description="a proposal expires at the close of this many periods after its signal",
    )
    holding_horizon_periods: int = Field(
        default=5, ge=1, le=260, description="expected holding horizon, in periods"
    )
    min_signal_strength: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1,
        description="drafts below this uncalibrated strength are not recorded",
    )

    @field_validator("timeframes", "directions")
    @classmethod
    def _unique(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        if len(set(value)) != len(value):
            raise ValueError("values must be unique")
        return value

    @field_validator("timeframes")
    @classmethod
    def _known_timeframes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [t for t in value if t not in BAR_TIMEFRAMES]
        if unknown:
            raise ValueError(f"unsupported timeframe(s): {', '.join(unknown)}")
        return value


@dataclass(frozen=True)
class EvaluationSchedule:
    """When the engine re-evaluates a strategy."""

    triggers: tuple[TriggerKind, ...]
    description: str

    def describe(self) -> dict[str, Any]:
        return {"triggers": [t.value for t in self.triggers], "description": self.description}


BAR_CLOSE_SCHEDULE = EvaluationSchedule(
    (TriggerKind.BAR_CLOSE,),
    "after every completed and settled period of each configured timeframe",
)


@dataclass(frozen=True)
class AssetView:
    asset_id: uuid.UUID
    symbol: str
    asset_class: AssetClass
    exchange_code: str | None

    @property
    def market(self) -> str:
        return self.asset_class.value


@dataclass(frozen=True)
class EvaluationContext:
    """Everything a strategy may read. Feature values are point in time at ``data_cutoff``."""

    asset: AssetView
    timeframe: str
    mode: EvaluationMode
    basis: KnowledgeBasis
    data_cutoff: datetime
    features: Mapping[str, FeatureValue]  # canonical feature key -> value
    trigger: TriggerKind
    registry: FeatureRegistry = REGISTRY

    def feature(self, name: str, timeframe: str | None = None, **params: Any) -> FeatureValue:
        """The value of a feature the strategy declared (same name and parameters)."""
        key = self.registry.resolve(FeatureRequest(name, timeframe or self.timeframe, params)).key
        value = self.features.get(key)
        if value is None:
            raise StrategyDefinitionError(f"feature {key} was not declared as required")
        return value

    def number(self, name: str, timeframe: str | None = None, **params: Any) -> float:
        value = self.feature(name, timeframe, **params)
        if not value.valid or not isinstance(value.value, float):
            raise StrategyDefinitionError(f"{value.key} has no valid numeric value")
        return value.value


@dataclass(frozen=True)
class Evidence:
    """A feature the proposal rests on. ``feature_key`` must be in the evaluation's feature
    snapshot and VALID; the engine refuses evidence it cannot trace."""

    feature_key: str
    role: str  # e.g. "trigger", "confirmation", "context", "invalidation"
    note: str


@dataclass(frozen=True)
class ProposalDraft:
    """A strategy's hypothesis. Prices are in the stored bar series' units."""

    direction: Direction
    reference_entry: Decimal
    signal_strength: Decimal  # uncalibrated score in [0, 1], not a probability
    reasoning: str
    evidence: tuple[Evidence, ...]
    invalidation_level: Decimal | None = None
    target_reference: Decimal | None = None


@dataclass(frozen=True)
class StrategyOutcome:
    proposals: tuple[ProposalDraft, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


class Strategy(ABC):
    strategy_id: ClassVar[str]
    family: ClassVar[StrategyFamily]
    name: ClassVar[str]
    description: ClassVar[str]
    logic_version: ClassVar[str]
    # False: interface and parameters only; the engine never evaluates it and it cannot
    # be enabled (Phase 4A ships the six families like this; logic arrives in Phase 4B).
    implemented: ClassVar[bool] = False
    supported_asset_classes: ClassVar[tuple[AssetClass, ...]]
    supported_timeframes: ClassVar[tuple[str, ...]]
    schedule: ClassVar[EvaluationSchedule] = BAR_CLOSE_SCHEDULE
    Parameters: ClassVar[type[StrategyParameters]] = StrategyParameters

    def default_parameters(self) -> StrategyParameters:
        return self.parse_parameters({})

    def parse_parameters(self, raw: Mapping[str, Any]) -> StrategyParameters:
        try:
            params = self.Parameters.model_validate(dict(raw))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'parameters'}: {e['msg']}"
                for e in exc.errors()
            )
            raise StrategyDefinitionError(problems) from exc
        unsupported = [t for t in params.timeframes if t not in self.supported_timeframes]
        if unsupported:
            raise StrategyDefinitionError(
                f"timeframes: {', '.join(unsupported)} not supported by {self.strategy_id}"
            )
        return params

    @classmethod
    def parameters_schema(cls) -> dict[str, Any]:
        return cls.Parameters.model_json_schema()

    @abstractmethod
    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]: ...

    @abstractmethod
    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome: ...


def typed[P: StrategyParameters](params: StrategyParameters, kind: type[P]) -> P:
    """``params`` as the family's own parameter class."""
    if not isinstance(params, kind):
        raise StrategyDefinitionError(f"expected {kind.__name__}, got {type(params).__name__}")
    return params


class InterfaceOnlyStrategy(Strategy, ABC):
    """Base of the Phase 4A families: metadata, parameters and features, no logic."""

    implemented: ClassVar[bool] = False

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        raise StrategyNotImplementedError(
            f"{self.strategy_id} has no evaluation logic yet (Phase 4B)"
        )


__all__ = [
    "BAR_CLOSE_SCHEDULE",
    "STRATEGY_ID_PATTERN",
    "AssetView",
    "EvaluationContext",
    "EvaluationSchedule",
    "Evidence",
    "InterfaceOnlyStrategy",
    "ProposalDraft",
    "Strategy",
    "StrategyDefinitionError",
    "StrategyNotImplementedError",
    "StrategyOutcome",
    "StrategyParameters",
    "typed",
]
