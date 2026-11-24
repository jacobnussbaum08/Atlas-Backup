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
from enum import StrEnum
from typing import Any, Literal

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

# 8A.1: the Phase 8A replay (daily-v1 execution conventions, no dividends).
# 8A.2 (D-237): explicit execution conventions (daily-v2, the E7 corrections, by default)
# and cash dividends. Stored 8A.1 configurations replay unchanged (daily-v1, no dividends).
ENGINE_VERSION = "8A.2"
MAX_SYMBOLS = 200
MAX_SESSIONS = 2600  # about ten years of sessions


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExecutionConventions(StrEnum):
    """How the daily replay prices fills (D-237). The conventions are part of the run's
    content-addressed execution model."""

    # Phase 8A / 7B on one daily bar: limits fill at the limit, stops at the worse of the
    # stop and the close, market orders at the next session's close. Every run stored
    # before engine 8A.2 used these; kept for replay and as a pessimistic comparison.
    DAILY_V1 = "daily-v1"
    # E7 corrections (D-236): opened-through limits and market orders at the open, stops
    # at their level or at the gapped open, all with spread, slippage and impact.
    DAILY_V2 = "daily-v2"


# Cash dividends credited (long) or charged (short) at the ex-date on the quantity held
# over the previous close (D-237). Absent from a stored configuration: not modelled.
# v2 (X3.1, D-244) differs only in which provider rows count: coverage needs a retrieval
# whose range is in ex-date terms (v1 trusted ranges the provider applied to processing
# dates, missing late-December ex-dates), exact provider duplicates are credited once, and a
# symbol two run assets share is joined to both (a correction set voids the wrong one).
# Stored configurations keep the policy they name, so they replay unchanged.
DIVIDEND_POLICY_V1 = "cash-at-ex-date-v1"
DIVIDEND_POLICY = "cash-at-ex-date-v2"
DIVIDEND_POLICIES = (DIVIDEND_POLICY_V1, DIVIDEND_POLICY)
# D-239: a held security delisted (its last trade known from the research security
# master) is closed at its last traded close, after costs, the next session. Unknown
# merger or liquidation proceeds are not invented: the last trade is the price used.
DELISTING_POLICY = "exit-at-last-trade-v1"
# D-242: date-bounded corporate-action exclusions. A position in an asset is closed at the
# close of the session before its contaminated observations (after costs), and the asset
# is out of the decision universe from that session until no decision's read window
# (``read_sessions``) contains them (and through ``blocked_through`` for a splice).
EXCLUSION_POLICY = "exit-before-contamination-block-read-window-v1"


# ---------------------------------------------------------------------- request
class UniverseSpec(_Strict):
    mode: UniverseMode = UniverseMode.EXPLICIT
    symbols: tuple[str, ...] = ()
    layer: UniverseLayer | None = None
    # D-239: the research universe's code (RESEARCH mode only). Omitted from the stored
    # configuration when absent, so earlier configurations keep their hashes.
    research_universe: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{2,64}$")

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
        if (self.mode is UniverseMode.RESEARCH) != (self.research_universe is not None):
            raise ValueError("a RESEARCH universe needs research_universe, and only it")
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


class MeasuredSpreadSpec(_Strict):
    """Use quoted spreads measured from stored historical quote samples (D-234).

    A sample is one ``quoted_spread_bps`` measurement (``app.backtesting.spreads``) of the
    named feed shortly before a session's close. The risk engine's spread check sees the
    sample's observed bid and ask (instead of its ASSUMED fallback), and marketable fills
    use the sample's half spread instead of the model's assumed one. A sample older than
    ``max_age_hours`` at the decision or fill is not used: the replay then falls back to the
    assumptions exactly as live trading does without a fresh quote, and counts it."""

    feed: str = Field(pattern=r"^(iex|sip)$")
    max_age_hours: int = Field(default=6, ge=1, le=96)
    # Stress: every measured spread is scaled by this (1 = as measured).
    multiplier: Decimal = Field(default=Decimal("1"), ge=Decimal("0.1"), le=Decimal("10"))


class OpeningEntrySpec(_Strict):
    """An entry that executes at the next session's open, bounded by a worst price (E1,
    D-235). Research option for the backtest only; live entries are unchanged.

    The worst entry price W is the signal close moved against the trade by
    ``tolerance_pct`` of it, or by ``tolerance_atr`` times the strategy's own ATR (exactly
    one). The proposal reaches the unchanged risk engine with entry W, so size and potential
    loss cover the worst permitted fill. On the next session's bar: an open within W fills
    at the open, adjusted adversely by the half spread (the measured opening spread when
    ``open_spreads`` and a sample exist, else the model's assumption), slippage and impact,
    never beyond W and inside the bar's range; otherwise the order rests as a limit at W
    under the configured rules."""

    tolerance_pct: Decimal | None = Field(default=None, ge=0, le=Decimal("0.02"))
    tolerance_atr: Decimal | None = Field(default=None, ge=0, le=5)
    open_spreads: bool = True

    @model_validator(mode="after")
    def _one_tolerance(self) -> OpeningEntrySpec:
        if (self.tolerance_pct is None) == (self.tolerance_atr is None):
            raise ValueError("set exactly one of tolerance_pct and tolerance_atr")
        return self


class ExecutionFidelitySpec(_Strict):
    """E7 (D-236) research option: corrected daily-bar execution conventions, for audit.

    Absent (the default) the replay keeps the 7B/8A conventions unchanged. The paper
    broker never reads this option. Each flag corrects one convention of the daily replay:

    * ``limit_marketable_at_open``: a limit or take-profit whose session *opens* better than
      its limit was marketable when the session began: it fills at the open, adjusted
      adversely by the half spread, slippage and impact, never worse than the limit and
      inside the bar's range (the baseline fills it at the limit, even when the whole
      session traded better). A take-profit that opened beyond its target fills before a
      stop reached later in that session (the open is the session's first price).
    * ``stop_fill_at_trigger``: a stop triggered inside the session fills at its level
      adjusted adversely by the session's half spread, slippage and impact (the baseline
      takes the worse of the stop at no cost and the session's close with costs); a stop
      the session opened beyond fills at the open with the same adjustments (the baseline
      can take the close when it is worse).
    * ``market_orders_at_open``: a market order submitted after a close (holding-horizon
      exits) fills at the next session's open with the same adjustments, before any stop
      or take-profit of that session (the baseline uses that session's close, after them).

    The half spread at the open is the measured opening sample (``open_spreads``) when one
    exists, else the session's measured spread or the model's assumption, counted.
    """

    limit_marketable_at_open: bool = True
    stop_fill_at_trigger: bool = True
    market_orders_at_open: bool = True
    open_spreads: bool = True


class PipelineSpec(_Strict):
    """Optional live-pipeline stages (D-234). Absent: the Phase 8A replay, unchanged."""

    # Phase 5B coordinator between the strategies and the risk engine, fed point-in-time
    # Phase 3B market intelligence (market and asset regimes, asset features) computed
    # from the replay's own bars. Only PRIORITIZE_FOR_REVIEW decisions reach the risk
    # engine, with their lead proposal's levels, as in live trading (10A intents).
    coordinator: bool = False
    # Live order lifetime: an entry gets the next regular session to fill (10A
    # ``order_expiry``) instead of the proposal's own expiry.
    live_entry_expiry: bool = False
    measured_spreads: MeasuredSpreadSpec | None = None
    # E1 entry-order experiment (D-235). None: the entry is the configured order type.
    opening_entry: OpeningEntrySpec | None = None
    # E7 execution-model audit (D-236). None: the 7B/8A daily-bar conventions.
    execution_fidelity: ExecutionFidelitySpec | None = None


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
    pipeline: PipelineSpec | None = None
    # D-237: new runs use the corrected conventions and model cash dividends by default.
    # DAILY_V1 reproduces the Phase 8A conventions (a pessimistic comparison).
    execution_conventions: ExecutionConventions = ExecutionConventions.DAILY_V2
    dividends: bool = True
    # D-239 research options (absent: unchanged behaviour). ``correlation_source``
    # "replay-bars-v1": the risk engine's correlations are computed point in time from the
    # replay's own bars instead of read from stored measurements. ``preregistration``: the
    # code of a registered pre-registration; required when a run touches reserved data.
    correlation_source: Literal["replay-bars-v1"] | None = None
    preregistration: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{2,64}$")
    # D-243: the code of a registered corporate-action correction set to apply (None: none).
    corporate_actions: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{2,64}$")

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


class ExclusionWindowSpec(_Strict):
    """One WINDOW exclusion (D-242) as a run applies it to one of its assets."""

    code: str
    asset_id: str
    symbol: str
    event_session: date
    contaminated_from: date
    contaminated_to: date
    # The first and last sessions on which the asset is out of the decision universe;
    # a position held at the close of ``blocked_from`` is closed there.
    blocked_from: date
    blocked_through: date


class ExclusionSpec(_Strict):
    """The research exclusions a run was resolved under (D-242): every set registered by
    the run's knowledge time, by content hash, and the windows that apply to its assets."""

    policy: str = EXCLUSION_POLICY
    read_sessions: int
    sets: dict[str, str]  # code -> content hash (research.store.exclusion_sets)
    windows: tuple[ExclusionWindowSpec, ...] = ()


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
    # Left out of the JSON when absent, so every earlier configuration keeps its hash.
    pipeline: PipelineSpec | None = None
    # D-237 (engine 8A.2). Absent (every configuration stored before 8A.2): daily-v1, no
    # dividends. Left out of the JSON when absent.
    execution_conventions: ExecutionConventions | None = None
    dividend_policy: str | None = None
    # D-239 (absent in every earlier configuration; left out of the JSON when absent):
    # delisting exits (RESEARCH universes), point-in-time correlations, and the
    # pre-registration a run on reserved data was made under ({code, document_sha256}).
    delisting_policy: str | None = None
    correlation_source: str | None = None
    preregistration: dict[str, str] | None = None
    # D-242 (absent in every earlier configuration; left out of the JSON when absent).
    exclusions: ExclusionSpec | None = None
    # D-243: {code, content_sha256, policy, preregistration_code} of the applied correction
    # set (absent: none; left out of the JSON when absent).
    corporate_actions: dict[str, str] | None = None

    @property
    def conventions(self) -> ExecutionConventions:
        return self.execution_conventions or ExecutionConventions.DAILY_V1

    def risk_configuration(self) -> RiskConfiguration:
        return RiskConfiguration.model_validate(self.risk)

    def model(self) -> ExecutionModel:
        return ExecutionModel.model_validate(self.execution_model)

    @property
    def known_at(self) -> datetime:
        assert self.data.data_known_at is not None  # noqa: S101  # resolved
        return self.data.data_known_at

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.model_dump_json())
        if data.get("universe", {}).get("research_universe") is None:
            data["universe"].pop("research_universe", None)  # before D-239
        for key in (
            "execution_conventions",
            "dividend_policy",
            "delisting_policy",
            "correlation_source",
            "preregistration",
            "exclusions",
            "corporate_actions",
        ):
            if data.get(key) is None:
                data.pop(key, None)  # configurations stored before engine 8A.2
        if data.get("pipeline") is None:
            data.pop("pipeline", None)
        else:
            # Runs configured before D-235 / D-236 keep their hash.
            for option in ("opening_entry", "execution_fidelity"):
                if data["pipeline"].get(option) is None:
                    data["pipeline"].pop(option, None)
        return data

    def sha256(self) -> str:
        return sha256_of(self.as_json())

    def replay_sha256(self) -> str:
        """What a replay depends on: the configuration without its name and code label."""
        data = self.as_json()
        data.pop("name", None)
        data.pop("code_version", None)
        return sha256_of(data)


__all__ = [
    "DELISTING_POLICY",
    "DIVIDEND_POLICIES",
    "DIVIDEND_POLICY",
    "DIVIDEND_POLICY_V1",
    "ENGINE_VERSION",
    "EXCLUSION_POLICY",
    "BacktestPolicies",
    "BacktestRequest",
    "DataSpec",
    "ExclusionSpec",
    "ExclusionWindowSpec",
    "ExecutionConventions",
    "ExecutionFidelitySpec",
    "ExecutionSpec",
    "MeasuredSpreadSpec",
    "OpeningEntrySpec",
    "PipelineSpec",
    "ResolvedAsset",
    "ResolvedConfig",
    "ResolvedStrategy",
    "RiskSpec",
    "StrategySelection",
    "UniverseSpec",
    "canonical_json",
    "sha256_of",
]
