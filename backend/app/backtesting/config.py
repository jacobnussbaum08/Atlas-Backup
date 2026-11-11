"""Backtest configuration: what a user asks for, and the fully resolved form a run uses.

A :class:`BacktestRequest` names what to test (dates, capital, universe, strategies,
timeframes, risk and execution choices, data source, benchmark, seed). The service resolves
it, once, into a :class:`ResolvedConfig` in which everything a replay depends on is pinned:

* strategy versions (database ids, logic versions) and their full effective parameters;
* the complete risk configuration (hard limits and preferences, with its hash);
* the complete 7B execution model (content-addressed) plus the backtest's order policy;
* the data source, the bar series, the knowledge time of the dataset and the policies used
  where the original availability of an observation is unknown;
* the universe definition, the benchmark, the seed, and the code and engine versions.

The resolved configuration is stored with the run and is the only input of a replay, so a
completed run can be re-executed from it and must produce the same results.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.asset_discovery.enums import UniverseLayer
from app.backtesting.enums import (
    BreakerResetPolicy,
    EntryOrderType,
    FeatureAdjustment,
    StopOrderType,
    UniverseMode,
)
from app.paper_broker.execution_model import ExecutionModel
from app.risk.config import RiskConfiguration
from app.strategies.enums import Direction

ENGINE_VERSION = "8A.1"
MAX_SYMBOLS = 200
MAX_SESSIONS = 2600  # about ten years of sessions


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------- request
class UniverseSpec(_Strict):
    mode: UniverseMode = UniverseMode.EXPLICIT
    symbols: tuple[str, ...] = ()
    layer: UniverseLayer | None = None

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(s.strip().upper() for s in value if s.strip()))
        if len(cleaned) > MAX_SYMBOLS:
            raise ValueError(f"at most {MAX_SYMBOLS} symbols")
        return cleaned

    @model_validator(mode="after")
    def _coherent(self) -> UniverseSpec:
        if self.mode is UniverseMode.EXPLICIT and not self.symbols:
            raise ValueError("an EXPLICIT universe needs at least one symbol")
        if self.mode is UniverseMode.POINT_IN_TIME and self.layer is None:
            raise ValueError("a POINT_IN_TIME universe needs a layer")
        return self


class StrategySelection(_Strict):
    strategy_id: str
    # None: the latest stored version when the run is created (pinned then).
    version: int | None = Field(default=None, ge=1)
    # Parameter overrides validated by the strategy's own parameter model. The stored
    # version's parameters are the base; the run records both and labels the result.
    parameters: dict[str, Any] | None = None


class RiskSpec(_Strict):
    # Partial overrides of the stored risk configuration (validated by 6B's models).
    hard_limits: dict[str, Any] | None = None
    preferences: dict[str, Any] | None = None


class ExecutionSpec(_Strict):
    entry_order_type: EntryOrderType = EntryOrderType.LIMIT
    stop_order_type: StopOrderType = StopOrderType.STOP
    # Trailing stops: percent of the reference price (None: not used).
    trail_percent: Decimal | None = Field(default=None, gt=0, le=50)
    use_take_profit: bool = True
    # A market exit after the proposal's holding horizon (in its timeframe's periods).
    exit_at_horizon: bool = True
    # Overrides of the 7B execution model's equity assumptions (AssetClassModel fields).
    equity_overrides: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _trailing(self) -> ExecutionSpec:
        if (self.stop_order_type is StopOrderType.TRAILING_STOP) != (
            self.trail_percent is not None
        ):
            raise ValueError("a TRAILING_STOP needs trail_percent, and only it uses one")
        return self


class DataSpec(_Strict):
    provider: str = "alpaca"
    feed: str = "iex"
    feature_adjustment: FeatureAdjustment = FeatureAdjustment.POINT_IN_TIME_SPLIT
    # A session's bar counts as completed and settled this long after the regular close
    # (Phase 4A STRATEGY_SETTLE_SECONDS default).
    # At most 16 h, so a decision is always outside the next regular session.
    settle_seconds: int = Field(default=3600, ge=60, le=57_600)
    # Conservative delay after a news article's provider publication time before a
    # historical decision may count it (backfilled news has no recorded availability).
    news_delay_minutes: int = Field(default=5, ge=0, le=24 * 60)
    # The dataset's knowledge time: only rows ingested by then are used, so later
    # ingestion cannot change a replay. None: the creation time.
    data_known_at: datetime | None = None


class BacktestRequest(_Strict):
    name: str = Field(min_length=1, max_length=120)
    start_date: date
    end_date: date
    initial_capital: Decimal = Field(default=Decimal("100000"), ge=1000, le=Decimal("1000000000"))
    universe: UniverseSpec
    strategies: tuple[StrategySelection, ...] = Field(min_length=1, max_length=24)
    # Restrict every selected strategy to these timeframes / directions (None: as configured).
    timeframes: tuple[str, ...] | None = None
    directions: tuple[Direction, ...] | None = None
    risk: RiskSpec = Field(default_factory=RiskSpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    data: DataSpec = Field(default_factory=DataSpec)
    benchmark_symbol: str | None = "SPY"
    breaker_reset_policy: BreakerResetPolicy = BreakerResetPolicy.NEXT_SESSION_IF_CLEAR
    seed: int = Field(default=0, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def _dates(self) -> BacktestRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        if (self.end_date - self.start_date) > timedelta(days=MAX_SESSIONS * 7 // 5):
            raise ValueError("the date range is too long")
        ids = [s.strategy_id for s in self.strategies]
        if len(ids) != len(set(ids)):
            raise ValueError("each strategy may be selected once")
        return self


# ---------------------------------------------------------------------- resolved
class ResolvedStrategy(_Strict):
    strategy_id: str
    family: str
    version_id: str
    version: int
    logic_version: str
    configuration_id: str
    stored_parameters: dict[str, Any]
    parameters: dict[str, Any]  # effective, every field explicit
    parameters_sha256: str
    parameters_source: str  # STORED_VERSION | BACKTEST_OVERRIDE
    timeframes: tuple[str, ...]


class ResolvedAsset(_Strict):
    asset_id: str
    symbol: str
    asset_class: str
    exchange_code: str | None
    quote_currency: str


class BacktestPolicies(_Strict):
    """Where an observation's original availability is unknown, what the replay assumes."""

    bar_availability: str = (
        "a daily bar of session d is usable from d's regular close + settle_seconds; the "
        "retrieval time of a REST backfill is never treated as the original availability"
    )
    news_availability: str = (
        "an article counts for a decision only if its provider publication time is at least "
        "news_delay_minutes before the decision cutoff"
    )
    revisions: str = (
        "the newest bar revision ingested by data_known_at is used (RECONSTRUCTED basis): a "
        "later correction of a historical bar is visible to the replay"
    )
    reference_data: str = (
        "tradability, shortability and lot size come from the newest provider reference "
        "version observed by the decision time, else the earliest version Atlas observed "
        "(labelled RECONSTRUCTED_REFERENCE)"
    )
    corporate_actions: str = (
        "splits are inferred from the stored SPLIT and RAW series; features are rescaled to "
        "the split factor known at each decision; a position held through a split is "
        "carried at the last pre-split close; dividends are not modelled"
    )


class ResolvedConfig(_Strict):
    engine_version: str = ENGINE_VERSION
    code_version: str
    name: str
    start_date: date
    end_date: date
    initial_capital: Decimal
    currency: str = "USD"
    universe: UniverseSpec
    assets: tuple[ResolvedAsset, ...]  # EXPLICIT: the symbols; POINT_IN_TIME: every candidate
    strategies: tuple[ResolvedStrategy, ...]
    risk: dict[str, Any]  # RiskConfiguration as JSON
    risk_source: str  # STORED_VERSION:<id> | ENGINE_DEFAULTS, plus +OVERRIDES
    risk_sha256: str
    execution_model: dict[str, Any]  # ExecutionModel as JSON
    execution_model_sha256: str
    execution: ExecutionSpec
    data: DataSpec  # data_known_at always set
    benchmark: ResolvedAsset | None
    # Assets read only as references: the benchmark and benchmark features' symbols.
    reference_assets: tuple[ResolvedAsset, ...] = ()
    breaker_reset_policy: BreakerResetPolicy
    seed: int
    feature_registry_version: str
    risk_engine_version: str
    model_versions: tuple[dict[str, Any], ...] = ()
    model_usage: str = (
        "none: Phase 8A replays strategy proposals through the deterministic risk engine; no "
        "ML prediction or LLM output enters a backtest decision"
    )
    policies: BacktestPolicies = Field(default_factory=BacktestPolicies)

    def risk_configuration(self) -> RiskConfiguration:
        return RiskConfiguration.model_validate(self.risk)

    def model(self) -> ExecutionModel:
        return ExecutionModel.model_validate(self.execution_model)

    @property
    def known_at(self) -> datetime:
        assert self.data.data_known_at is not None  # noqa: S101  # resolved
        return self.data.data_known_at

    def as_json(self) -> dict[str, Any]:
        return json.loads(self.model_dump_json())  # type: ignore[no-any-return]

    def sha256(self) -> str:
        return sha256_of(self.as_json())

    def replay_sha256(self) -> str:
        """What a replay depends on: the configuration without its name and code label."""
        data = self.as_json()
        data.pop("name", None)
        data.pop("code_version", None)
        return sha256_of(data)


__all__ = [
    "ENGINE_VERSION",
    "BacktestPolicies",
    "BacktestRequest",
    "DataSpec",
    "ExecutionSpec",
    "ResolvedAsset",
    "ResolvedConfig",
    "ResolvedStrategy",
    "RiskSpec",
    "StrategySelection",
    "UniverseSpec",
    "canonical_json",
    "sha256_of",
]
