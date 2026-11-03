"""Dataset and target specifications (Phase 5A).

A dataset version is built from one :class:`DatasetSpec`. The specification is validated,
stored with the version and hashed: building the same specification (including its
``data_known_at``) against the same stored data gives the same observations, features and
labels, byte for byte (``content_sha256``).

Every target is documented in :data:`TARGET_DOCUMENTATION` and in the stored
``target_definition``. No target is a claim that predicting it is profitable: the
financial evaluation of every model uses the realized net outcome of each observation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.data_engine.enums import PriceAdjustment
from app.feature_engine.registry import BAR_TIMEFRAMES, SYMBOL_PATTERN, ParamValue, sha256_json
from app.ml_engine.enums import (
    BarrierSource,
    EntryReference,
    ObservationSource,
    TargetKind,
    TaskKind,
    UniversePolicy,
)
from app.strategies.enums import Direction

TARGET_VERSION = "target-v1"
DATASET_SPEC_VERSION = "dataset-spec-v1"
NAME_PATTERN = r"^[a-z][a-z0-9_.-]{2,63}$"

CLASSIFICATION_TARGETS = (
    TargetKind.DIRECTION,
    TargetKind.VOL_ADJUSTED_DIRECTION,
    TargetKind.BARRIER,
)

TARGET_DOCUMENTATION: dict[TargetKind, str] = {
    TargetKind.FORWARD_RETURN: (
        "Regression. net_return = direction * (exit / entry - 1) - round_trip_cost, where "
        "entry is the open of the first period after the decision (or the decision "
        "period's close for DECISION_CLOSE), exit is the close of the horizon-th period "
        "after the decision, direction is +1 LONG / -1 SHORT and the cost is the stated "
        "round-trip assumption in basis points."
    ),
    TargetKind.DIRECTION: (
        "Classification. 1 when net_return (as FORWARD_RETURN, after costs) is above "
        "`threshold`, else 0. With threshold 0 the positive class means 'the position "
        "would have made money after the assumed costs', not 'the price went up'."
    ),
    TargetKind.VOL_ADJUSTED_RETURN: (
        "Regression. net_return / (atr_pct(atr_length) * sqrt(horizon)), the outcome in "
        "units of the volatility known at the decision (ATR relative to the close, per "
        "period, computed from completed periods only)."
    ),
    TargetKind.VOL_ADJUSTED_DIRECTION: (
        "Classification. 1 when the volatility-adjusted return is above `threshold` "
        "(in volatility units), else 0."
    ),
    TargetKind.BARRIER: (
        "Classification (triple barrier). From the entry, the daily path is followed for "
        "the horizon. 1 when the profit barrier is reached before the stop barrier, else "
        "0 (stop first, or neither before the horizon). Barriers: entry +/- multiples of "
        "the ATR known at the decision (ATR_MULTIPLES) or the proposal's own target and "
        "invalidation (STRATEGY_LEVELS). When one daily bar crosses both, the stop is "
        "assumed first (spec 18.1); a gap through a barrier exits at the open. The "
        "realized net_return is that of the exit."
    ),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must carry a time zone")
    return value.astimezone(UTC)


class BarrierSpec(_Strict):
    source: BarrierSource = BarrierSource.ATR_MULTIPLES
    profit_multiple: float = Field(default=2.0, gt=0, le=20)
    stop_multiple: float = Field(default=1.0, gt=0, le=20)


class TargetSpec(_Strict):
    kind: TargetKind
    # Periods of the dataset timeframe; None: the strategy's holding horizon (strategy
    # sources only).
    horizon_periods: int | None = Field(default=5, ge=1, le=60)
    entry: EntryReference = EntryReference.NEXT_OPEN
    # Assumed round-trip cost (commissions, spread, slippage) in basis points. Atlas has no
    # execution cost model before Phase 7: this is a stated assumption, not a measurement.
    round_trip_cost_bps: float = Field(default=10.0, ge=0, le=500)
    threshold: float = Field(default=0.0, ge=-1, le=10)
    atr_length: int = Field(default=14, ge=2, le=100)
    barrier: BarrierSpec | None = None

    @property
    def task(self) -> TaskKind:
        return (
            TaskKind.CLASSIFICATION if self.kind in CLASSIFICATION_TARGETS else TaskKind.REGRESSION
        )

    @model_validator(mode="after")
    def _consistent(self) -> TargetSpec:
        if self.kind is TargetKind.BARRIER and self.barrier is None:
            raise ValueError("a BARRIER target needs barrier settings")
        if self.kind is not TargetKind.BARRIER and self.barrier is not None:
            raise ValueError("barrier settings apply to BARRIER targets only")
        return self

    def definition(self) -> dict[str, Any]:
        """Everything that defines the label, with its documentation (stored and hashed)."""
        return {
            "version": TARGET_VERSION,
            "kind": self.kind.value,
            "task": self.task.value,
            "horizon_periods": self.horizon_periods,
            "horizon_source": "fixed" if self.horizon_periods else "strategy holding horizon",
            "entry": self.entry.value,
            "round_trip_cost_bps": self.round_trip_cost_bps,
            "cost_basis": "ASSUMPTION (no execution cost model before Phase 7); short "
            "borrow fees are not modelled",
            "threshold": self.threshold,
            "atr_length": self.atr_length,
            "barrier": None if self.barrier is None else self.barrier.model_dump(mode="json"),
            "same_bar_rule": "STOP_FIRST",
            "maturity": "a label is MATURED only when the horizon's last period completed and "
            "settled (close + settle delay) by the dataset's data_known_at",
            "documentation": TARGET_DOCUMENTATION[self.kind],
        }


class FeatureItem(_Strict):
    """A registry feature requested on every dataset timeframe."""

    name: str = Field(min_length=1, max_length=64)
    params: dict[str, ParamValue] = Field(default_factory=dict)
    # Optional features may be missing (non-VALID) without rejecting the observation; the
    # preprocessing imputes them and adds a missing indicator.
    optional: bool = False


class DatasetSpec(_Strict):
    spec_version: Literal["dataset-spec-v1"] = "dataset-spec-v1"
    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(default="", max_length=2000)
    source: ObservationSource = ObservationSource.PERIOD_CLOSE
    universe: UniversePolicy = UniversePolicy.FIXED_ASSETS
    symbols: tuple[str, ...] = Field(min_length=1, max_length=500)
    timeframes: tuple[str, ...] = ("1Day",)
    directions: tuple[Direction, ...] = (Direction.LONG, Direction.SHORT)
    # Strategy sources: which strategies (empty: all six).
    strategies: tuple[str, ...] = ()
    decision_start: datetime
    decision_end: datetime
    # The data snapshot: bars and stored features as Atlas knew them at this instant.
    # None: the build time (recorded in the stored specification).
    data_known_at: datetime | None = None
    features: tuple[FeatureItem, ...] = Field(min_length=1, max_length=200)
    target: TargetSpec
    adjustment: PriceAdjustment = PriceAdjustment.SPLIT
    settle_seconds: int = Field(default=3600, ge=0, le=86_400)
    # Segment definitions used for evaluation by market condition (descriptive).
    market_benchmark: str = "SPY"
    market_trend_periods: int = Field(default=60, ge=5, le=260)
    volatility_length: int = Field(default=20, ge=5, le=120)
    volatility_history: int = Field(default=60, ge=20, le=260)
    # Test fixtures only: the series is synthetic. Real datasets leave this False.
    fixture: bool = False

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(s.strip().upper() for s in value))
        bad = [s for s in cleaned if not SYMBOL_PATTERN.match(s)]
        if bad:
            raise ValueError(f"invalid symbol(s): {', '.join(bad)}")
        return cleaned

    @field_validator("timeframes")
    @classmethod
    def _timeframes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one timeframe is required")
        bad = [t for t in value if t not in BAR_TIMEFRAMES]
        if bad:
            raise ValueError(f"unsupported timeframe(s): {', '.join(bad)}")
        return tuple(dict.fromkeys(value))

    @field_validator("directions")
    @classmethod
    def _directions(cls, value: tuple[Direction, ...]) -> tuple[Direction, ...]:
        if not value:
            raise ValueError("at least one direction is required")
        return tuple(dict.fromkeys(value))

    @field_validator("decision_start", "decision_end", "data_known_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def _consistent(self) -> DatasetSpec:
        if self.decision_start >= self.decision_end:
            raise ValueError("decision_start must precede decision_end")
        if self.data_known_at is not None and self.data_known_at < self.decision_end:
            raise ValueError("data_known_at cannot precede decision_end")
        strategy_source = self.source is not ObservationSource.PERIOD_CLOSE
        if self.target.horizon_periods is None and not strategy_source:
            raise ValueError("PERIOD_CLOSE datasets need a fixed horizon_periods")
        if self.strategies and not strategy_source:
            raise ValueError("strategies apply to strategy observation sources only")
        barrier = self.target.barrier
        if (
            barrier is not None
            and barrier.source is BarrierSource.STRATEGY_LEVELS
            and not strategy_source
        ):
            raise ValueError("STRATEGY_LEVELS barriers need a strategy observation source")
        names = [(f.name, tuple(sorted(f.params.items()))) for f in self.features]
        if len(names) != len(set(names)):
            raise ValueError("a feature is listed twice")
        return self

    def frozen(self, data_known_at: datetime) -> DatasetSpec:
        """The specification with its data snapshot time fixed (what is stored)."""
        return self.model_copy(update={"data_known_at": _utc(data_known_at)})

    def stored(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def sha256(self) -> str:
        return sha256_json(self.stored())


__all__ = [
    "CLASSIFICATION_TARGETS",
    "DATASET_SPEC_VERSION",
    "TARGET_DOCUMENTATION",
    "TARGET_VERSION",
    "BarrierSpec",
    "DatasetSpec",
    "FeatureItem",
    "TargetSpec",
]
