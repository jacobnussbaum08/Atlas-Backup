"""Walk-forward validation and controlled parameter experimentation (Phase 8B).

An :class:`ExperimentConfig` names a strategy (or several, for a combined evaluation), a
universe, a date range, a parameter space and search method, the walk-forward windows and
the robustness methods. :class:`WalkForwardRunner` executes it entirely in memory on the
Phase 8A replay (:class:`~app.backtesting.engine.BacktestEngine`), the same rule code paper
trading uses; persistence is the service's job.

Method (WALK_FORWARD experiments):

1. Resolve the base configuration once (strategy version, risk configuration, execution
   model, dataset knowledge time pinned). Every replay derives from it deterministically.
2. Build the session layout (:mod:`app.learning.windows`): development folds, embargoes from
   the longest label of every candidate, and the untouched holdout.
3. **Trials.** Trial 0 is the stored (champion) parameter set; the search adds up to
   ``max_trials - 1`` feasible points. Each trial is replayed **once over the development
   period only** (its dataset is loaded up to the development end: holdout bars are never
   read). Bayesian search is guided only by the first fold's selection window, the earliest
   data, so the choice of what to try never sees a test window.
4. **Folds.** For each fold, every trial's training window is scored from the slice of its
   development replay (mark-to-market returns up to the window end; trades purged at the
   boundary). The score used for ranking is the median of the trial's own score and those
   of its evaluated neighbours (one step along one parameter), so an isolated peak loses to
   a stable plateau. The best ``top_k`` are scored on the validation window and the best of
   those is selected (ties: training score, then trial number). With too few trades for any
   candidate the stored parameters are kept, and that is recorded.
5. **Out of sample.** The selected parameters, and the stored ones as the champion
   baseline, are replayed fresh over the fold's test window with data loaded only up to
   that window's end. Test windows are stitched into one out-of-sample record.
6. **Robustness.** PBO by CSCV and CPCV / purged k-fold over the trials' development
   returns; deflated Sharpe with the recorded number of trials; multiple-testing
   adjustments; parameter sensitivity around the final selection; Monte Carlo trade
   sequences; stationary block bootstrap; transaction-cost and slippage stress (fresh
   test-window replays with scaled assumptions); regime segmentation.
7. **Findings and verdict.** There is no "proven" verdict: INSUFFICIENT_EVIDENCE,
   NOT_ROBUST, or CANDIDATE_FOR_REVIEW (every gate passed; a human decides).

EVALUATION experiments skip the search: the given configurations are replayed over the same
test windows individually and together, with the same robustness methods except those
that measure selection (PBO, CPCV, deflation over trials).
"""

from __future__ import annotations

import itertools
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.backtesting.config import (
    BacktestRequest,
    DataSpec,
    ExecutionSpec,
    ResolvedConfig,
    ResolvedStrategy,
    RiskSpec,
    StrategySelection,
    UniverseSpec,
    sha256_of,
)
from app.backtesting.data import HistoricalDataset, load_dataset, sessions_back, sessions_between
from app.backtesting.engine import BacktestEngine, BacktestResult, Trade
from app.backtesting.service import BacktestService
from app.learning import evaluation as ev
from app.learning import regimes as rg
from app.learning import statistics as st
from app.learning.enums import (
    ExperimentKind,
    Finding,
    ResultStatus,
    RobustnessMethod,
    Sample,
    SearchMethod,
    SegmentKind,
    TrialSource,
    TrialStatus,
    Verdict,
)
from app.learning.search import (
    DEFAULT_SPACES,
    ParameterSpace,
    Searcher,
    feasible_points,
    neighbors,
)
from app.learning.windows import (
    FoldWindows,
    Layout,
    WindowSpec,
    build_layout,
    required_embargo,
)
from app.paper_broker.execution_model import ExecutionModel
from app.strategies import catalog
from app.strategies.base import StrategyDefinitionError
from app.strategies.enums import Direction

VALIDATION_ENGINE_VERSION = "8B.1"
MAX_TRIALS = 60
REGIME_LOOKBACK = 400  # sessions of benchmark history before the first session

COST_FIELDS = (
    "commission_per_unit",
    "commission_rate",
    "commission_minimum",
    "commission_maximum_rate",
    "sell_regulatory_fee_rate",
    "sell_per_unit_fee",
    "sell_per_unit_fee_maximum",
    "exchange_fee_rate",
    "borrow_rate_annual",
)
SLIPPAGE_FIELDS = ("slippage_bps", "assumed_half_spread_bps", "impact_bps_at_full_participation")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StrategySearch(_Strict):
    strategy_id: str
    # None: the latest stored version when the experiment is created (pinned then).
    version: int | None = Field(default=None, ge=1)
    # Fixed overrides of the stored parameters (the base of every trial).
    parameters: dict[str, Any] | None = None
    # WALK_FORWARD: the space to search (None: the family's default space).
    space: ParameterSpace | None = None


class RobustnessSpec(_Strict):
    pbo_groups: int = Field(default=8, ge=4, le=16)
    cpcv_groups: int = Field(default=6, ge=3, le=12)
    cpcv_test_groups: int = Field(default=2, ge=1, le=6)
    purged_kfold: int = Field(default=5, ge=2, le=12)
    bootstrap_resamples: int = Field(default=1000, ge=100, le=20000)
    monte_carlo_resamples: int = Field(default=1000, ge=100, le=20000)
    cost_multipliers: tuple[Decimal, ...] = (Decimal(0), Decimal(2), Decimal(3))
    slippage_multipliers: tuple[Decimal, ...] = (Decimal(2), Decimal(4))
    sensitivity: bool = True

    @model_validator(mode="after")
    def _ranges(self) -> RobustnessSpec:
        if self.cpcv_test_groups >= self.cpcv_groups:
            raise ValueError("cpcv_test_groups must be fewer than cpcv_groups")
        if self.pbo_groups % 2:
            raise ValueError("pbo_groups must be even")
        for m in (*self.cost_multipliers, *self.slippage_multipliers):
            if m < 0 or m > 10:
                raise ValueError("stress multipliers must be between 0 and 10")
        return self


class ThresholdSpec(_Strict):
    min_oos_trades: int = Field(default=ev.MIN_TRADES, ge=1, le=10_000)
    min_oos_sessions: int = Field(default=ev.MIN_RETURNS, ge=5, le=10_000)
    max_drawdown: float = Field(default=0.15, gt=0, le=1)
    max_top5_share: float = Field(default=0.5, gt=0, le=10)
    degradation_ratio: float = Field(default=0.5, ge=0, le=1)
    min_dsr: float = Field(default=0.95, ge=0.5, le=1)
    max_pbo: float = Field(default=0.5, ge=0, le=1)
    min_profit_factor: float = Field(default=1.2, ge=1, le=10)
    min_positive_fold_share: float = Field(default=0.6, ge=0, le=1)

    def thresholds(self) -> ev.Thresholds:
        return ev.Thresholds(**self.model_dump())


class ExperimentConfig(_Strict):
    name: str = Field(min_length=1, max_length=120)
    kind: ExperimentKind = ExperimentKind.WALK_FORWARD
    strategies: tuple[StrategySearch, ...] = Field(min_length=1, max_length=6)
    start_date: date
    end_date: date
    initial_capital: Decimal = Field(default=Decimal("100000"), ge=1000, le=Decimal("1000000000"))
    universe: UniverseSpec
    timeframes: tuple[str, ...] | None = None
    # Long/short separation: restrict to one direction (a run per direction), or both.
    directions: tuple[Direction, ...] | None = None
    risk: RiskSpec = Field(default_factory=RiskSpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    data: DataSpec = Field(default_factory=DataSpec)
    benchmark_symbol: str | None = "SPY"
    windows: WindowSpec = Field(default_factory=WindowSpec)
    search: SearchMethod = SearchMethod.GRID
    max_trials: int = Field(default=20, ge=1, le=MAX_TRIALS)
    bayesian_initial: int = Field(default=5, ge=2, le=20)
    objective: str = "NET_SHARPE"
    # A training window with fewer closed trades than this cannot select a candidate.
    min_selection_trades: int = Field(default=5, ge=1, le=1000)
    top_k: int = Field(default=3, ge=1, le=10)
    robustness: RobustnessSpec = Field(default_factory=RobustnessSpec)
    thresholds: ThresholdSpec = Field(default_factory=ThresholdSpec)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def _coherent(self) -> ExperimentConfig:
        if self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        ids = [s.strategy_id for s in self.strategies]
        if len(ids) != len(set(ids)):
            raise ValueError("each strategy may be selected once")
        if self.objective not in ev.OBJECTIVES:
            raise ValueError(f"objective must be one of {', '.join(ev.OBJECTIVES)}")
        if self.kind is ExperimentKind.WALK_FORWARD:
            if len(self.strategies) != 1:
                raise ValueError("a WALK_FORWARD experiment searches one strategy at a time")
            if self.search is SearchMethod.NONE:
                raise ValueError("a WALK_FORWARD experiment needs a search method")
        elif self.search is not SearchMethod.NONE or any(s.space for s in self.strategies):
            raise ValueError("an EVALUATION experiment does not search (search NONE, no space)")
        return self

    def sha256(self) -> str:
        return sha256_of(self.model_dump(mode="json"))


# ---------------------------------------------------------------------- results
@dataclass
class Trial:
    number: int
    strategy_id: str
    source: TrialSource
    parameters: dict[str, Any]
    status: TrialStatus = TrialStatus.OK
    error: str | None = None
    config_sha256: str | None = None
    dataset_sha256: str | None = None
    development: ev.WindowEvaluation | None = None
    result: BacktestResult | None = field(default=None, repr=False)
    selectable: bool = True

    @property
    def parameters_sha256(self) -> str:
        return sha256_of(self.parameters)

    def returns_on(self, sessions: list[date]) -> list[float]:
        """Net daily returns of the development replay on ``sessions`` (0 where absent)."""
        if self.development is None:
            return [0.0] * len(sessions)
        lookup = dict(zip(self.development.sessions, self.development.daily_returns, strict=True))
        return [lookup.get(d, 0.0) for d in sessions]

    def as_json(self) -> dict[str, Any]:
        return {
            "trial_number": self.number,
            "strategy_id": self.strategy_id,
            "source": self.source.value,
            "parameters": self.parameters,
            "parameters_sha256": self.parameters_sha256,
            "status": self.status.value,
            "error": self.error,
            "selectable": self.selectable,
            "config_sha256": self.config_sha256,
            "dataset_sha256": self.dataset_sha256,
            "development": None if self.development is None else self.development.as_json(),
        }


@dataclass
class FoldResult:
    index: int
    windows: dict[str, Any]
    refit: bool
    selected_trial: int
    selection: dict[str, Any]
    in_sample: dict[str, Any] | None
    validation: dict[str, Any] | None
    oos: ev.WindowEvaluation
    champion_oos: ev.WindowEvaluation | None
    oos_config_sha256: str
    oos_dataset_sha256: str


@dataclass
class RobustnessRecord:
    method: RobustnessMethod
    scope: str
    status: ResultStatus
    result: dict[str, Any]
    assumptions: list[str]
    limitations: list[str]

    def as_json(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "scope": self.scope,
            "status": self.status.value,
            "result": self.result,
            "assumptions": self.assumptions,
            "limitations": self.limitations,
        }


@dataclass
class SegmentRecord:
    sample: Sample
    kind: SegmentKind
    value: str
    metrics: dict[str, Any]
    status: ResultStatus


@dataclass
class ExperimentResult:
    layout: Layout
    base_config: ResolvedConfig
    trials: list[Trial]
    folds: list[FoldResult]
    robustness: list[RobustnessRecord]
    segments: list[SegmentRecord]
    summary: dict[str, Any]
    verdict: Verdict
    findings: list[Finding]
    reasons: list[str]
    infeasible_points: int

    def content(self) -> dict[str, Any]:
        """What a reproduction must reproduce."""
        return {
            "layout": self.layout.describe(),
            "trials": [t.as_json() for t in self.trials],
            "folds": [
                {
                    "index": f.index,
                    "selected_trial": f.selected_trial,
                    "selection": f.selection,
                    "oos": f.oos.as_json(),
                    "champion_oos": None if f.champion_oos is None else f.champion_oos.as_json(),
                }
                for f in self.folds
            ],
            "robustness": [r.as_json() for r in self.robustness],
            "summary": self.summary,
            "verdict": self.verdict.value,
        }

    def sha256(self) -> str:
        return sha256_of(self.content())


Progress = Callable[[str, int, int], None]


# ---------------------------------------------------------------------- configuration
def derive(
    base: ResolvedConfig,
    *,
    start: date,
    end: date,
    parameters: dict[str, dict[str, Any]] | None = None,
    execution_model: dict[str, Any] | None = None,
    label: str = "",
) -> ResolvedConfig:
    """A replay configuration for [start, end] that differs from ``base`` only in dates,
    strategy parameters (full effective sets, already validated) and execution model."""
    strategies: list[ResolvedStrategy] = []
    for rs in base.strategies:
        params = (parameters or {}).get(rs.strategy_id)
        if params is None:
            strategies.append(rs)
            continue
        strategies.append(
            rs.model_copy(
                update={
                    "parameters": params,
                    "parameters_sha256": sha256_of(params),
                    "parameters_source": "STORED_VERSION"
                    if params == rs.stored_parameters
                    else "VALIDATION_TRIAL",
                    "timeframes": tuple(params["timeframes"]),
                }
            )
        )
    update: dict[str, Any] = {
        "start_date": start,
        "end_date": end,
        "strategies": tuple(strategies),
        "name": (base.name + (f" [{label}]" if label else ""))[:120],
    }
    if execution_model is not None:
        model = ExecutionModel.model_validate(execution_model)
        update["execution_model"] = model.definition()
        update["execution_model_sha256"] = model.definition_sha256()
    return base.model_copy(update=update)


def stressed_model(
    base: dict[str, Any], fields: tuple[str, ...], multiplier: Decimal
) -> dict[str, Any]:
    """The execution model with ``fields`` of every asset class scaled by ``multiplier``
    (bounded by each field's validation limits); a new content-addressed model."""
    model = ExecutionModel.model_validate(base)
    classes = {}
    for name, cls in model.classes.items():
        values = cls.model_dump()
        for f in fields:
            v = values.get(f)
            if v is None:
                continue
            scaled = Decimal(str(v)) * multiplier
            limit = _upper_bound(type(cls), f)
            values[f] = min(scaled, limit) if limit is not None else scaled
        classes[name] = type(cls).model_validate(values)
    code = f"{model.code[:40]}-stress-{sha256_of([list(fields), str(multiplier)])[:8]}"
    stressed = model.model_copy(update={"classes": classes, "code": code})
    return stressed.definition()


def _upper_bound(cls: type[BaseModel], name: str) -> Decimal | None:
    for meta in cls.model_fields[name].metadata:
        le = getattr(meta, "le", None)
        if le is not None:
            return Decimal(str(le))
    return None


def base_request(config: ExperimentConfig, *, end: date | None = None) -> BacktestRequest:
    return BacktestRequest(
        name=config.name[:120],
        start_date=config.start_date,
        end_date=end or config.end_date,
        initial_capital=config.initial_capital,
        universe=config.universe,
        strategies=tuple(
            StrategySelection(strategy_id=s.strategy_id, version=s.version, parameters=s.parameters)
            for s in config.strategies
        ),
        timeframes=config.timeframes,
        directions=config.directions,
        risk=config.risk,
        execution=config.execution,
        data=config.data,
        benchmark_symbol=config.benchmark_symbol,
        seed=config.seed,
    )


# ---------------------------------------------------------------------- runner
class WalkForwardRunner:
    def __init__(self, service: BacktestService) -> None:
        self.service = service
        self.registry = service.registry
        self._regime_cache: dict[tuple[str, date], tuple[dict[date, rg.Regime], list[date]]] = {}

    # .................................................................. replays
    def replay(
        self, session: Session, config: ResolvedConfig
    ) -> tuple[BacktestResult, HistoricalDataset]:
        """Load the dataset **up to the configuration's end date** and replay it."""
        dataset = self.service.load(session, config)
        result = BacktestEngine(session, config, dataset, registry=self.registry).run()
        return result, dataset

    def _validator(self, strategy_id: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
        strategy = self.registry.get(strategy_id)

        def validate(raw: dict[str, Any]) -> dict[str, Any]:
            try:
                return catalog.parameters_json(strategy.parse_parameters(raw))
            except StrategyDefinitionError as exc:
                raise ValueError(str(exc)) from None

        return validate

    # .................................................................. main
    def run(
        self,
        session: Session,
        config: ExperimentConfig,
        base: ResolvedConfig,
        *,
        prior_trials: int = 0,
        progress: Progress | None = None,
    ) -> ExperimentResult:
        report = progress or (lambda _stage, _done, _total: None)
        sessions = [s.day for s in sessions_between(config.start_date, config.end_date)]
        if config.kind is ExperimentKind.WALK_FORWARD:
            return self._walk_forward(session, config, base, sessions, prior_trials, report)
        return self._evaluation(session, config, base, sessions, report)

    def _space(self, config: ExperimentConfig, rs: ResolvedStrategy) -> ParameterSpace:
        selection = config.strategies[0]
        if selection.space is not None:
            return selection.space
        space = DEFAULT_SPACES.get(rs.family.lower())
        if space is None:
            raise ValueError(f"{rs.strategy_id} has no default parameter space; give one")
        return space

    def _layout(
        self, sessions: list[date], config: ExperimentConfig, param_sets: list[dict[str, Any]]
    ) -> Layout:
        horizons = [
            (tf, int(p["holding_horizon_periods"]), int(p["signal_ttl_periods"]))
            for p in param_sets
            for tf in p["timeframes"]
        ]
        return build_layout(sessions, config.windows, required_embargo(horizons))

    def _walk_forward(
        self,
        session: Session,
        config: ExperimentConfig,
        base: ResolvedConfig,
        sessions: list[date],
        prior_trials: int,
        report: Progress,
    ) -> ExperimentResult:
        rs = base.strategies[0]
        space = self._space(config, rs)
        validate = self._validator(rs.strategy_id)
        points, infeasible = feasible_points(space, rs.parameters, validate)
        stored = rs.parameters
        layout = self._layout(sessions, config, [stored, *points])
        dev_first = sessions[layout.development.start]
        dev_last = sessions[layout.development.end]
        others = [p for p in points if p != stored]
        searcher = Searcher(
            config.search,
            space,
            others,
            budget=config.max_trials - 1,
            seed=config.seed,
            initial_random=config.bayesian_initial,
        )
        guide = layout.folds[0]
        guide_first = sessions[guide.train.start]
        guide_last = sessions[guide.selection_end]
        trials: list[Trial] = []
        total = searcher.budget + 1

        def run_trial(params: dict[str, Any], source: TrialSource) -> Trial:
            trial = Trial(len(trials), rs.strategy_id, source, params)
            cfg = derive(
                base,
                start=dev_first,
                end=dev_last,
                parameters={rs.strategy_id: params},
                label=f"trial {trial.number}",
            )
            trial.config_sha256 = cfg.replay_sha256()
            try:
                result, dataset = self.replay(session, cfg)
            except Exception as exc:  # recorded and counted; never retried with other data
                trial.status, trial.error = TrialStatus.FAILED, f"{type(exc).__name__}: {exc}"[:500]
                trials.append(trial)
                return trial
            trial.result = result
            trial.dataset_sha256 = dataset.sha256()
            trial.development = ev.evaluate_window(
                result,
                base.initial_capital,
                dev_first,
                dev_last,
                min_trades=config.min_selection_trades,
            )
            trials.append(trial)
            report("trials", len(trials), max(total, len(trials)))
            return trial

        run_trial(stored, TrialSource.STORED)
        source = TrialSource(config.search.value)
        while (params := searcher.next()) is not None:
            trial = run_trial(params, source)
            score = None
            if trial.result is not None:
                window = ev.evaluate_window(
                    trial.result, base.initial_capital, guide_first, guide_last
                )
                score = ev.objective(window, config.objective, config.min_selection_trades)
            searcher.report(score)

        # Neighbour map for the plateau-smoothed score (evaluated trials only).
        ok = [t for t in trials if t.status is TrialStatus.OK]
        neighbour_map = {
            t.number: [
                o.number
                for o in ok
                if o.number != t.number
                and o.parameters in neighbors(space, t.parameters, [o.parameters])
            ]
            for t in ok
        }

        folds: list[FoldResult] = []
        previous: int | None = None
        for fw in layout.folds:
            report("folds", fw.index, len(layout.folds))
            folds.append(
                self._fold(session, config, base, layout, fw, trials, neighbour_map, previous)
            )
            previous = folds[-1].selected_trial
        final_trial = trials[folds[-1].selected_trial]
        # Sensitivity: neighbours of the final selection not yet evaluated are replayed on
        # the development period (labelled NEIGHBOR; never selectable; counted).
        if config.robustness.sensitivity:
            evaluated = [t.parameters for t in trials]
            for params in neighbors(space, final_trial.parameters, points):
                if params not in evaluated:
                    t = run_trial(params, TrialSource.NEIGHBOR)
                    t.selectable = False
        report("robustness", 0, 1)
        robustness = self._robustness(
            session, config, base, layout, trials, folds, final_trial, space, prior_trials
        )
        segments = self._segments(session, base, layout, folds)
        return self._conclude(
            config, base, layout, trials, folds, robustness, segments, final_trial, infeasible
        )

    # .................................................................. folds
    def _score(
        self,
        trial: Trial,
        base: ResolvedConfig,
        first: date,
        last: date,
        config: ExperimentConfig,
    ) -> tuple[ev.WindowEvaluation | None, float | None]:
        if trial.result is None:
            return None, None
        window = ev.evaluate_window(
            trial.result, base.initial_capital, first, last, min_trades=config.min_selection_trades
        )
        return window, ev.objective(window, config.objective, config.min_selection_trades)

    def _fold(
        self,
        session: Session,
        config: ExperimentConfig,
        base: ResolvedConfig,
        layout: Layout,
        fw: FoldWindows,
        trials: list[Trial],
        neighbour_map: dict[int, list[int]],
        previous: int | None,
    ) -> FoldResult:
        s = layout.sessions
        train_first, train_last = s[fw.train.start], s[fw.train.end]
        candidates = [t for t in trials if t.status is TrialStatus.OK and t.selectable]
        train_scores: dict[int, float | None] = {}
        train_evals: dict[int, ev.WindowEvaluation] = {}
        for t in candidates:
            window, score = self._score(t, base, train_first, train_last, config)
            train_scores[t.number] = score
            if window is not None:
                train_evals[t.number] = window
        robust: dict[int, float | None] = {}
        for t in candidates:
            own = train_scores.get(t.number)
            if own is None:
                robust[t.number] = None
                continue
            near = [train_scores.get(n) for n in neighbour_map.get(t.number, [])]
            values = [own, *[v for v in near if v is not None]]
            robust[t.number] = float(np.median(values))
        ranking = sorted(
            (n for n, v in robust.items() if v is not None),
            key=lambda n: (-(robust[n] or 0.0), n),
        )
        top = ranking[: config.top_k]
        val_scores: dict[int, float | None] = {}
        val_evals: dict[int, ev.WindowEvaluation] = {}
        note = ""
        if not fw.refit and previous is not None:
            selected = previous
            note = "refit schedule: the previous fold's selection is reused"
        elif not top:
            selected = 0
            note = (
                f"no candidate had {config.min_selection_trades} closed trades in the training "
                "window: the stored parameters are kept"
            )
        elif fw.validation is not None:
            vf, vl = s[fw.validation.start], s[fw.validation.end]
            for n in top:
                window, score = self._score(trials[n], base, vf, vl, config)
                val_scores[n] = score
                if window is not None:
                    val_evals[n] = window
            scored = [n for n in top if val_scores[n] is not None]
            if scored:
                selected = sorted(
                    scored, key=lambda n: (-(val_scores[n] or 0.0), -(robust[n] or 0.0), n)
                )[0]
            else:
                selected = top[0]
                note = "no top candidate had enough validation trades: the training rank decides"
        else:
            selected = top[0]
        chosen = trials[selected]
        # Out of sample: fresh replays, data loaded only up to the test window's end.
        test_first, test_last = s[fw.test.start], s[fw.test.end]
        oos_cfg = derive(
            base,
            start=test_first,
            end=test_last,
            parameters={chosen.strategy_id: chosen.parameters},
            label=f"fold {fw.index} test",
        )
        result, dataset = self.replay(session, oos_cfg)
        bench = None if base.benchmark is None else uuid.UUID(base.benchmark.asset_id)
        oos = ev.evaluate_window(
            result, base.initial_capital, test_first, test_last, dataset=dataset, benchmark=bench
        )
        champion_oos = oos
        if selected != 0:
            champ_cfg = derive(
                base,
                start=test_first,
                end=test_last,
                parameters={chosen.strategy_id: trials[0].parameters},
                label=f"fold {fw.index} champion",
            )
            champ_result, champ_dataset = self.replay(session, champ_cfg)
            champion_oos = ev.evaluate_window(
                champ_result,
                base.initial_capital,
                test_first,
                test_last,
                dataset=champ_dataset,
                benchmark=bench,
            )
        return FoldResult(
            index=fw.index,
            windows={
                "train": fw.train.describe(s),
                "validation": None if fw.validation is None else fw.validation.describe(s),
                "test": fw.test.describe(s),
                "embargo_sessions": layout.embargo,
            },
            refit=fw.refit,
            selected_trial=selected,
            selection={
                "objective": config.objective,
                "min_trades": config.min_selection_trades,
                "rule": "rank by the median of the trial's training score and its evaluated "
                "neighbours' scores; best validation score among the top "
                f"{config.top_k}; ties by training score, then trial number",
                "note": note,
                "ranking": [
                    {
                        "trial": n,
                        "train_score": train_scores.get(n),
                        "plateau_score": robust.get(n),
                        "validation_score": val_scores.get(n),
                    }
                    for n in ranking
                ],
                "unscored_trials": sorted(n for n, v in robust.items() if v is None),
            },
            in_sample=None if selected not in train_evals else train_evals[selected].as_json(),
            validation=None if selected not in val_evals else val_evals[selected].as_json(),
            oos=oos,
            champion_oos=champion_oos,
            oos_config_sha256=oos_cfg.replay_sha256(),
            oos_dataset_sha256=dataset.sha256(),
        )

    # .................................................................. stitching
    @staticmethod
    def stitched(windows: list[ev.WindowEvaluation], capital: Decimal) -> dict[str, Any]:
        returns = [r for w in windows for r in w.daily_returns]
        gross = [r for w in windows for r in w.gross_returns]
        trades = [t for w in windows for t in w.kept_trades]
        stats = ev.trade_stats(trades)
        equity = []
        value = float(capital)
        for r in returns:
            value *= 1 + r
            equity.append(value)
        sr = st.sharpe(returns)
        bench = [w.benchmark_return for w in windows]
        bench_total = None
        if bench and all(b is not None for b in bench):
            bench_total = math.prod(1 + b for b in bench if b is not None) - 1
        net = math.prod(1 + r for r in returns) - 1 if returns else 0.0
        return {
            "sessions": len(returns),
            "windows": len(windows),
            "net_return": net,
            "gross_return": math.prod(1 + r for r in gross) - 1 if gross else 0.0,
            "max_drawdown": ev._drawdown(equity, float(capital)),
            "sharpe_annualized": None if sr is None else sr * ev.ANNUAL,
            "sortino_annualized": ev._sortino(returns),
            "returns_status": (
                ResultStatus.OK
                if len(returns) >= ev.MIN_RETURNS
                else ResultStatus.INSUFFICIENT_SAMPLE
            ).value,
            "costs": sum(w.costs for w in windows),
            "turnover_mean": float(np.mean([w.turnover for w in windows if w.turnover is not None]))
            if any(w.turnover is not None for w in windows)
            else None,
            "benchmark_return": bench_total,
            "excess_return": None if bench_total is None else net - bench_total,
            "trade_statistics": stats.as_json(),
            "fold_net_returns": [w.net_return for w in windows],
        }

    # .................................................................. robustness
    def _stress(
        self,
        session: Session,
        base: ResolvedConfig,
        layout: Layout,
        parameters: dict[str, dict[str, Any]] | None,
        fields: tuple[str, ...],
        multipliers: tuple[Decimal, ...],
    ) -> dict[str, Any]:
        s = layout.sessions
        levels: dict[str, Any] = {}
        # The same parameters at every level, 1x included (the assumed costs), so the levels
        # differ only in the stressed assumptions.
        for m in sorted({Decimal(1), *multipliers}):
            model = stressed_model(base.execution_model, fields, m)
            windows: list[ev.WindowEvaluation] = []
            for fw in layout.folds:
                first, last = s[fw.test.start], s[fw.test.end]
                cfg = derive(
                    base,
                    start=first,
                    end=last,
                    parameters=parameters,
                    execution_model=model,
                    label=f"stress x{m}",
                )
                result, _ = self.replay(session, cfg)
                windows.append(ev.evaluate_window(result, base.initial_capital, first, last))
            levels[str(m.normalize()) if m == m.to_integral() else str(m)] = {
                "net_return": math.prod(1 + w.net_return for w in windows) - 1,
                "trade_net_pnl": sum(w.trades.net_pnl for w in windows),
                "costs": sum(w.costs for w in windows),
                "trades": sum(w.trades.count for w in windows),
            }
        breakeven = None
        ordered = sorted((Decimal(k), v["net_return"]) for k, v in levels.items())
        for (m0, r0), (m1, r1) in itertools.pairwise(ordered):
            if r0 > 0 >= r1:
                breakeven = float(m0) + (float(m1) - float(m0)) * r0 / (r0 - r1)
                break
        return {"levels": levels, "breakeven_multiplier": breakeven, "fields": list(fields)}

    def _robustness(
        self,
        session: Session,
        config: ExperimentConfig,
        base: ResolvedConfig,
        layout: Layout,
        trials: list[Trial],
        folds: list[FoldResult],
        final: Trial,
        space: ParameterSpace | None,
        prior_trials: int,
    ) -> list[RobustnessRecord]:
        R = RobustnessRecord
        rb = config.robustness
        out: list[RobustnessRecord] = []
        dev_sessions = layout.sessions[layout.development.start : layout.development.end + 1]
        ok = [t for t in trials if t.status is TrialStatus.OK and t.development is not None]
        selectable = [t for t in ok if t.selectable]
        oos_windows = [f.oos for f in folds]
        oos_returns = [r for w in oos_windows for r in w.daily_returns]
        oos_trades = [t for w in oos_windows for t in w.kept_trades]
        if space is not None:
            matrix = np.array([t.returns_on(dev_sessions) for t in selectable]).T
            pbo = st.pbo_cscv(matrix, rb.pbo_groups) if len(selectable) >= 2 else None
            out.append(
                R(
                    RobustnessMethod.PBO,
                    "development period, selectable trials",
                    ResultStatus.OK if pbo is not None else ResultStatus.NOT_APPLICABLE,
                    pbo.as_json() if pbo is not None else {"trials": len(selectable)},
                    [
                        "every trial evaluated on the same development sessions",
                        "performance measure: per-session Sharpe of net daily returns",
                        "contiguous blocks; every half/half split (CSCV)",
                    ],
                    [
                        "needs at least 2 trials and 4 sessions per block",
                        "blocks are short with little data, so PBO is itself noisy",
                    ],
                )
            )
            for method, tg, groups in (
                (RobustnessMethod.CPCV, rb.cpcv_test_groups, rb.cpcv_groups),
                (RobustnessMethod.PURGED_KFOLD, 1, rb.purged_kfold),
            ):
                splits = st.purged_combinatorial(matrix, groups, tg, layout.embargo, layout.embargo)
                oos_sr = [x.oos_sharpe for x in splits if x.oos_sharpe is not None]
                out.append(
                    R(
                        method,
                        "development period, selectable trials",
                        ResultStatus.OK if splits else ResultStatus.NOT_APPLICABLE,
                        {
                            "groups": groups,
                            "test_groups": tg,
                            "purge_sessions": layout.embargo,
                            "embargo_sessions": layout.embargo,
                            "splits": [x.as_json() for x in splits],
                            "oos_sharpe_mean": float(np.mean(oos_sr)) if oos_sr else None,
                            "oos_sharpe_quantiles": None
                            if not oos_sr
                            else dict(
                                zip(
                                    ("5", "50", "95"),
                                    np.percentile(oos_sr, (5, 50, 95)).tolist(),
                                    strict=True,
                                )
                            ),
                            "share_positive_oos": float(np.mean([x.oos_mean > 0 for x in splits]))
                            if splits
                            else None,
                        },
                        [
                            "selection-only cross-validation: the strategy has no fitted state "
                            "beyond its parameters",
                            f"training sessions within {layout.embargo} sessions before a test "
                            "block are purged and within the same distance after it embargoed",
                        ],
                        [
                            "supplements the chronological walk-forward; it lets later data "
                            "select parameters tested on earlier data, which never happens live",
                            "each trial's returns come from one continuous replay, so a test "
                            "block can include positions opened just before it",
                        ],
                    )
                )
            # Deflated Sharpe of the final selection's out-of-sample record.
            dev_sr = [st.sharpe(t.development.daily_returns) for t in ok if t.development]
            finite = [x for x in dev_sr if x is not None]
            matrix_all = np.array([t.returns_on(dev_sessions) for t in ok]).T
            n_eff, rho = st.effective_trials(matrix_all) if len(ok) > 1 else (float(len(ok)), 0.0)
            trials_total = n_eff + prior_trials
            dsr = st.deflated_sharpe(oos_returns, finite, trials=trials_total)
            out.append(
                R(
                    RobustnessMethod.DEFLATED_SHARPE,
                    "stitched out-of-sample returns of the walk-forward selections",
                    ResultStatus.NOT_APPLICABLE
                    if dsr is None
                    else (
                        ResultStatus.OK
                        if len(oos_returns) >= ev.MIN_RETURNS
                        else ResultStatus.INSUFFICIENT_SAMPLE
                    ),
                    {
                        **({} if dsr is None else dsr.as_json()),
                        "trials_this_run": len(trials),
                        "average_trial_correlation": rho,
                        "prior_trials_on_overlapping_data": prior_trials,
                    },
                    [
                        "the trial count is this run's effective trials plus every earlier "
                        "trial of this strategy on overlapping dates",
                        "the null: every trial has zero true Sharpe",
                        "returns stationary and serially independent",
                    ],
                    [
                        "the trial Sharpe variance is this run's only",
                        "overlapping positions make returns serially dependent, so the "
                        "probability is optimistic",
                    ],
                )
            )
            # Multiple testing.
            p_values = []
            for t in ok:
                _, p = st.t_test_mean(t.development.daily_returns if t.development else [])
                p_values.append(p)
            t_oos, p_oos = st.t_test_mean(oos_returns)
            out.append(
                R(
                    RobustnessMethod.MULTIPLE_TESTING,
                    "development trials and the out-of-sample record",
                    ResultStatus.OK if p_oos is not None else ResultStatus.NOT_APPLICABLE,
                    {
                        "trials_recorded": len(trials),
                        "trial_p_values": p_values,
                        "trial_p_values_holm": st.holm(p_values),
                        "trials_significant_raw_5pct": sum(
                            1 for p in p_values if p is not None and p < 0.05
                        ),
                        "trials_significant_holm_5pct": sum(
                            1 for p in st.holm(p_values) if p is not None and p < 0.05
                        ),
                        "oos_t_statistic": t_oos,
                        "oos_p_value": p_oos,
                        "oos_p_value_bonferroni_all_trials": st.bonferroni(p_oos, trials_total),
                    },
                    [
                        "one-sided t-test of mean daily net return > 0",
                        "Holm controls the family-wise error across trials under any dependence",
                    ],
                    ["daily returns overlap in positions; the tests are approximate"],
                )
            )
            # Parameter sensitivity around the final selection.
            near = neighbors(space, final.parameters, [t.parameters for t in ok])
            near_trials = [t for t in ok if t.parameters in near]
            dev_final = final.development
            rows = [
                {
                    "trial": t.number,
                    "changed": {
                        k: [final.parameters.get(k), v]
                        for k, v in t.parameters.items()
                        if final.parameters.get(k) != v
                    },
                    "net_return": t.development.net_return if t.development else None,
                    "sharpe_annualized": t.development.sharpe if t.development else None,
                    "trades": t.development.trades.count if t.development else None,
                }
                for t in near_trials
            ]
            positive = [
                t.development is not None and t.development.net_return > 0 for t in near_trials
            ]
            out.append(
                R(
                    RobustnessMethod.PARAMETER_SENSITIVITY,
                    f"neighbours of trial {final.number} on the development period",
                    ResultStatus.OK if rows else ResultStatus.NOT_APPLICABLE,
                    {
                        "selected_trial": final.number,
                        "selected_net_return": None if dev_final is None else dev_final.net_return,
                        "selected_sharpe": None if dev_final is None else dev_final.sharpe,
                        "neighbors": rows,
                        "share_positive": float(np.mean(positive)) if positive else None,
                        "surface": [
                            {
                                "trial": t.number,
                                "parameters": {
                                    d.name: t.parameters.get(d.name) for d in space.dimensions
                                },
                                "net_return": t.development.net_return if t.development else None,
                                "sharpe_annualized": t.development.sharpe
                                if t.development
                                else None,
                            }
                            for t in ok
                        ],
                    },
                    ["one step along one parameter of the space; development period"],
                    ["the development period includes the windows the selection was made on"],
                )
            )
        # OOS degradation per fold.
        rows = []
        for f in folds:
            is_sr = None if f.in_sample is None else f.in_sample.get("sharpe_annualized")
            rows.append(
                {
                    "fold": f.index,
                    "selected_trial": f.selected_trial,
                    "in_sample_net_return": None
                    if f.in_sample is None
                    else f.in_sample["net_return"],
                    "in_sample_sharpe": is_sr,
                    "oos_net_return": f.oos.net_return,
                    "oos_sharpe": f.oos.sharpe,
                    "champion_oos_net_return": None
                    if f.champion_oos is None
                    else f.champion_oos.net_return,
                }
            )
        out.append(
            R(
                RobustnessMethod.OOS_DEGRADATION,
                "per fold",
                ResultStatus.OK if rows else ResultStatus.NOT_APPLICABLE,
                {"folds": rows},
                ["in-sample = the training window the parameters were selected on"],
                ["short windows give noisy Sharpe ratios"],
            )
        )
        # Monte Carlo trade sequences and block bootstrap of the OOS record.
        mc = st.monte_carlo_trades(
            [float(t.net_pnl) for t in oos_trades],
            float(base.initial_capital),
            resamples=rb.monte_carlo_resamples,
            seed=config.seed,
        )
        out.append(
            R(
                RobustnessMethod.MONTE_CARLO_TRADES,
                "out-of-sample closed trades",
                ResultStatus.OK if mc is not None else ResultStatus.INSUFFICIENT_SAMPLE,
                mc or {"trades": len(oos_trades), "minimum": 10},
                ["trades are exchangeable and independent", f"seed {config.seed}"],
                ["ignores overlap in time and clustering of losses"],
            )
        )
        block = max(float(layout.embargo), len(oos_returns) ** (1 / 3) if oos_returns else 1.0)
        bs = st.block_bootstrap(
            oos_returns, mean_block=block, resamples=rb.bootstrap_resamples, seed=config.seed
        )
        out.append(
            R(
                RobustnessMethod.BLOCK_BOOTSTRAP,
                "stitched out-of-sample daily returns",
                ResultStatus.OK if bs is not None else ResultStatus.INSUFFICIENT_SAMPLE,
                bs or {"observations": len(oos_returns), "minimum": 20},
                [
                    "stationary bootstrap (Politis-Romano), mean block = max(embargo, n^(1/3))",
                    f"seed {config.seed}",
                ],
                ["cannot produce market conditions absent from the sample"],
            )
        )
        params = None if space is None else {final.strategy_id: final.parameters}
        for method, fields, mults in (
            (RobustnessMethod.COST_STRESS, COST_FIELDS, rb.cost_multipliers),
            (RobustnessMethod.SLIPPAGE_STRESS, SLIPPAGE_FIELDS, rb.slippage_multipliers),
        ):
            result = self._stress(session, base, layout, params, fields, mults)
            out.append(
                R(
                    method,
                    "final parameters replayed fresh on every test window",
                    ResultStatus.OK,
                    result,
                    [
                        "every listed field of the execution model scaled, bounded by its limits",
                        "0x costs = before commissions, fees and borrow",
                    ],
                    [
                        "uses the final parameters on every test window (not each fold's own "
                        "selection), so it measures cost sensitivity, not the walk-forward",
                        "fills and participation are unchanged; market impact is modelled only "
                        "by the impact field",
                    ],
                )
            )
        # Regime segmentation of the OOS record.
        regimes, days = self._regimes(session, base, layout)
        trend = ev.returns_by_regime(
            [d for w in oos_windows for d in w.sessions], oos_returns, regimes, "trend"
        )
        vol = ev.returns_by_regime(
            [d for w in oos_windows for d in w.sessions], oos_returns, regimes, "volatility"
        )
        lookup = ev.regime_of_trade(regimes, days)
        out.append(
            R(
                RobustnessMethod.REGIME_SEGMENTATION,
                "out-of-sample record",
                ResultStatus.OK if regimes else ResultStatus.NOT_APPLICABLE,
                {
                    "definition": rg.DEFINITION,
                    "returns_by_trend": trend,
                    "returns_by_volatility": vol,
                    "trades_by_trend": {
                        k: v.as_json()
                        for k, v in ev.segment_trades(oos_trades, lambda t: lookup(t).trend).items()
                    },
                    "trades_by_volatility": {
                        k: v.as_json()
                        for k, v in ev.segment_trades(
                            oos_trades, lambda t: lookup(t).volatility
                        ).items()
                    },
                },
                ["regime labels from the benchmark's own past closes (see definition)"],
                [
                    "one benchmark defines the regime for every asset",
                    "a regime the sample never saw cannot be evaluated",
                ],
            )
        )
        return out

    def _regimes(
        self, session: Session, base: ResolvedConfig, layout: Layout
    ) -> tuple[dict[date, rg.Regime], list[date]]:
        """Regime labels up to the last session the run evaluates (the development end, or
        the holdout end for a holdout evaluation): later bars are never loaded."""
        key = (base.replay_sha256(), layout.sessions[-1])
        if key in self._regime_cache:
            return self._regime_cache[key]
        if base.benchmark is None:
            return {}, []
        bench = uuid.UUID(base.benchmark.asset_id)
        dataset = load_dataset(
            session,
            [bench],
            provider=base.data.provider,
            feed=base.data.feed,
            first=sessions_back(layout.sessions[0], REGIME_LOOKBACK),
            last=self._last_evaluated(layout),
            known_at=base.known_at,
            settle=timedelta(seconds=base.data.settle_seconds),
            split_adjusted=True,
        )
        series = dataset.split.get(bench) or dataset.raw.get(bench) or {}
        closes = [(d, float(b.close)) for d, b in series.items() if b.valid]
        regimes = rg.classify(closes)
        self._regime_cache[key] = (regimes, sorted(regimes))
        return self._regime_cache[key]

    @staticmethod
    def _last_evaluated(layout: Layout) -> date:
        last = max(f.test.end for f in layout.folds)
        return layout.sessions[last]

    def _segments(
        self, session: Session, base: ResolvedConfig, layout: Layout, folds: list[FoldResult]
    ) -> list[SegmentRecord]:
        trades = [t for f in folds for t in f.oos.kept_trades]
        fold_of: dict[int, int] = {}
        for f in folds:
            for t in f.oos.kept_trades:
                fold_of[id(t)] = f.index
        regimes, days = self._regimes(session, base, layout)
        lookup = ev.regime_of_trade(regimes, days)
        keys: list[tuple[SegmentKind, Callable[[Trade], str]]] = [
            (SegmentKind.OVERALL, lambda _t: "all"),
            (SegmentKind.STRATEGY, lambda t: t.strategy_id),
            (SegmentKind.ASSET, lambda t: t.symbol),
            (SegmentKind.ASSET_CLASS, lambda t: t.asset_class),
            (SegmentKind.TIMEFRAME, lambda t: t.timeframe),
            (SegmentKind.DIRECTION, lambda t: t.direction.value),
            (SegmentKind.MARKET_TREND, lambda t: lookup(t).trend),
            (SegmentKind.VOLATILITY, lambda t: lookup(t).volatility),
            (SegmentKind.PERIOD, lambda t: f"fold {fold_of[id(t)]}"),
        ]
        out: list[SegmentRecord] = []
        for kind, key in keys:
            minimum = ev.MIN_TRADES if kind is SegmentKind.OVERALL else ev.MIN_SEGMENT_TRADES
            for value, stats in ev.segment_trades(trades, key, minimum).items():
                out.append(
                    SegmentRecord(Sample.OUT_OF_SAMPLE, kind, value, stats.as_json(), stats.status)
                )
        return out

    # .................................................................. conclusion
    def _conclude(
        self,
        config: ExperimentConfig,
        base: ResolvedConfig,
        layout: Layout,
        trials: list[Trial],
        folds: list[FoldResult],
        robustness: list[RobustnessRecord],
        segments: list[SegmentRecord],
        final: Trial | None,
        infeasible: int,
    ) -> ExperimentResult:
        oos_windows = [f.oos for f in folds]
        stitched = self.stitched(oos_windows, base.initial_capital)
        champion_windows = [f.champion_oos for f in folds if f.champion_oos is not None]
        # EVALUATION runs select nothing, so there is no separate champion record.
        champion = (
            self.stitched(champion_windows, base.initial_capital) if champion_windows else None
        )
        by_method = {r.method: r for r in robustness}
        dsr = by_method.get(RobustnessMethod.DEFLATED_SHARPE)
        pbo = by_method.get(RobustnessMethod.PBO)
        cost = by_method.get(RobustnessMethod.COST_STRESS)
        sens = by_method.get(RobustnessMethod.PARAMETER_SENSITIVITY)
        oos_all = ev.WindowEvaluation(
            first=oos_windows[0].first,
            last=oos_windows[-1].last,
            sessions=[d for w in oos_windows for d in w.sessions],
            daily_returns=[r for w in oos_windows for r in w.daily_returns],
            gross_returns=[r for w in oos_windows for r in w.gross_returns],
            start_equity=float(base.initial_capital),
            end_equity=float(base.initial_capital) * (1 + stitched["net_return"]),
            net_return=stitched["net_return"],
            gross_return=stitched["gross_return"],
            max_drawdown=stitched["max_drawdown"],
            sharpe=stitched["sharpe_annualized"],
            sortino=stitched["sortino_annualized"],
            costs=stitched["costs"],
            turnover=stitched["turnover_mean"],
            trades=ev.trade_stats(
                [t for w in oos_windows for t in w.kept_trades], config.thresholds.min_oos_trades
            ),
        )
        in_sample = [
            f.in_sample["sharpe_annualized"]
            for f in folds
            if f.in_sample and f.in_sample.get("sharpe_annualized") is not None
        ]
        regime_stats = {
            seg.value: ev.trade_stats_from_json(seg.metrics, seg.status)
            for seg in segments
            if seg.kind is SegmentKind.MARKET_TREND
        }
        cost_net = None
        if cost is not None:
            two = cost.result["levels"].get("2")
            cost_net = None if two is None else two["trade_net_pnl"]
        found, reasons = ev.findings(
            oos=oos_all,
            fold_net_returns=stitched["fold_net_returns"],
            in_sample_sharpe=float(np.mean(in_sample)) if in_sample else None,
            oos_sharpe=stitched["sharpe_annualized"],
            regime_stats=regime_stats,
            dsr=None if dsr is None else dsr.result.get("deflated_sharpe"),
            pbo=None if pbo is None or pbo.status is not ResultStatus.OK else pbo.result.get("pbo"),
            cost_stress_net=cost_net,
            neighbor_positive_share=None if sens is None else sens.result.get("share_positive"),
            thresholds=config.thresholds.thresholds(),
        )
        negative = {
            Finding.UNPROFITABLE,
            Finding.UNPROFITABLE_AFTER_COSTS,
            Finding.EXCESSIVE_DRAWDOWN,
            Finding.DEPENDS_ON_FEW_TRADES,
            Finding.DEGRADES_OUT_OF_SAMPLE,
            Finding.LIKELY_OVERFIT,
            Finding.COST_SENSITIVE,
            Finding.PARAMETER_UNSTABLE,
            Finding.REGIME_DEPENDENT,
        }
        if Finding.INSUFFICIENT_EVIDENCE in found:
            verdict = Verdict.INSUFFICIENT_EVIDENCE
        elif Finding.CONSISTENT in found and not (set(found) & negative):
            verdict = Verdict.CANDIDATE_FOR_REVIEW
        else:
            verdict = Verdict.NOT_ROBUST
        final_params = {
            rs.strategy_id: final.parameters
            if final is not None and final.strategy_id == rs.strategy_id
            else rs.parameters
            for rs in base.strategies
        }
        summary = {
            "engine_version": VALIDATION_ENGINE_VERSION,
            "kind": config.kind.value,
            "layout": layout.describe(),
            "trials_recorded": len(trials),
            "trials_failed": sum(1 for t in trials if t.status is TrialStatus.FAILED),
            "infeasible_points": infeasible,
            "selected_trials": [f.selected_trial for f in folds],
            "final_trial": None if final is None else final.number,
            "final_parameters": final_params,
            "final_parameters_sha256": {k: sha256_of(v) for k, v in final_params.items()},
            "stored_parameters": {rs.strategy_id: rs.stored_parameters for rs in base.strategies},
            "out_of_sample": stitched,
            "champion_out_of_sample": champion,
            "verdict": verdict.value,
            "findings": [x.value for x in found],
            "reasons": reasons,
            "thresholds": config.thresholds.model_dump(),
            "holdout": None
            if layout.holdout is None
            else {
                **layout.holdout.describe(layout.sessions),
                "status": "UNTOUCHED: never read by this run",
            },
            "statement": "Historical simulation. Out-of-sample here means data not used to "
            "choose these parameters; it is not evidence of future performance.",
        }
        return ExperimentResult(
            layout=layout,
            base_config=base,
            trials=trials,
            folds=folds,
            robustness=robustness,
            segments=segments,
            summary=summary,
            verdict=verdict,
            findings=found,
            reasons=reasons,
            infeasible_points=infeasible,
        )

    # .................................................................. evaluation
    def _evaluation(
        self,
        session: Session,
        config: ExperimentConfig,
        base: ResolvedConfig,
        sessions: list[date],
        report: Progress,
    ) -> ExperimentResult:
        layout = self._layout(sessions, config, [rs.parameters for rs in base.strategies])
        s = layout.sessions
        bench = None if base.benchmark is None else uuid.UUID(base.benchmark.asset_id)
        folds: list[FoldResult] = []
        individual: dict[str, list[ev.WindowEvaluation]] = {
            rs.strategy_id: [] for rs in base.strategies
        }
        for fw in layout.folds:
            report("folds", fw.index, len(layout.folds))
            first, last = s[fw.test.start], s[fw.test.end]
            cfg = derive(base, start=first, end=last, label=f"fold {fw.index} combined")
            result, dataset = self.replay(session, cfg)
            combined = ev.evaluate_window(
                result, base.initial_capital, first, last, dataset=dataset, benchmark=bench
            )
            if len(base.strategies) > 1:
                for rs in base.strategies:
                    single = base.model_copy(update={"strategies": (rs,)})
                    scfg = derive(
                        single, start=first, end=last, label=f"fold {fw.index} {rs.strategy_id}"
                    )
                    r1, d1 = self.replay(session, scfg)
                    individual[rs.strategy_id].append(
                        ev.evaluate_window(
                            r1, base.initial_capital, first, last, dataset=d1, benchmark=bench
                        )
                    )
            folds.append(
                FoldResult(
                    index=fw.index,
                    windows={"test": fw.test.describe(s), "embargo_sessions": layout.embargo},
                    refit=False,
                    selected_trial=0,
                    selection={"note": "EVALUATION: fixed configuration, no selection"},
                    in_sample=None,
                    validation=None,
                    oos=combined,
                    champion_oos=None,
                    oos_config_sha256=cfg.replay_sha256(),
                    oos_dataset_sha256=dataset.sha256(),
                )
            )
        trial = Trial(0, ",".join(rs.strategy_id for rs in base.strategies), TrialSource.STORED, {})
        robustness = self._robustness(session, config, base, layout, [trial], folds, trial, None, 0)
        if len(base.strategies) > 1:
            series = {k: [r for w in v for r in w.daily_returns] for k, v in individual.items()}
            names = sorted(series)
            corr = None
            if len(names) > 1:
                # A strategy with no position has zero variance: its correlations are undefined
                # (stored as null).
                with np.errstate(divide="ignore", invalid="ignore"):
                    corr = np.corrcoef(np.array([series[n] for n in names]))
            robustness.append(
                RobustnessRecord(
                    RobustnessMethod.REGIME_SEGMENTATION,
                    "individual strategies on the same test windows",
                    ResultStatus.OK,
                    {
                        "individual": {
                            k: self.stitched(v, base.initial_capital) for k, v in individual.items()
                        },
                        "daily_return_correlation": None
                        if corr is None
                        else {
                            a: {b: float(corr[i, j]) for j, b in enumerate(names)}
                            for i, a in enumerate(names)
                        },
                    },
                    ["each strategy replayed alone with the same capital, risk and windows"],
                    [
                        "the combined replay shares capital and risk limits, so it is not the "
                        "sum of the individual replays"
                    ],
                )
            )
        segments = self._segments(session, base, layout, folds)
        return self._conclude(config, base, layout, [trial], folds, robustness, segments, None, 0)


__all__ = [
    "MAX_TRIALS",
    "VALIDATION_ENGINE_VERSION",
    "ExperimentConfig",
    "ExperimentResult",
    "FoldResult",
    "RobustnessRecord",
    "RobustnessSpec",
    "SegmentRecord",
    "StrategySearch",
    "ThresholdSpec",
    "Trial",
    "WalkForwardRunner",
    "base_request",
    "derive",
    "stressed_model",
]
