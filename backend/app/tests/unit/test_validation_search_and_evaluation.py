"""Phase 8B: parameter spaces and search, window evaluation, findings, monitoring checks,
model assessment and the candidate lifecycle table. Deterministic test vectors only."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.backtesting.broker import backtest_model
from app.learning import evaluation as ev
from app.learning.enums import (
    ActorKind,
    CandidateStatus,
    Finding,
    MeasurementStatus,
    MonitorMetric,
    ResultStatus,
    SearchMethod,
)
from app.learning.governance import TRANSITIONS, GovernanceError, GovernanceService
from app.learning.models import assess_run
from app.learning.monitoring import MonitorRule, feature_checks, performance_checks
from app.learning.search import (
    DEFAULT_SPACES,
    Constraint,
    Dimension,
    ParameterSpace,
    Searcher,
    feasible_points,
    neighbors,
)
from app.learning.walkforward import ExperimentConfig, stressed_model
from app.ml_engine.enums import ModelType, TaskKind
from app.strategies import catalog
from app.strategies.registry import default_registry

D = Decimal


# ---------------------------------------------------------------------- search
SPACE = ParameterSpace(
    dimensions=(
        Dimension(name="fast", values=(5, 10, 20)),
        Dimension(name="slow", low=D(10), high=D(30), step=D(10), integer=True),
    ),
    constraints=(Constraint(left="fast", op="<", right="slow"),),
)


def _identity(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("fast") == 20 and raw.get("slow") == 30:
        raise ValueError("model invariant (fixture)")
    return {k: str(v) if isinstance(v, Decimal) else v for k, v in raw.items()}


def test_constraints_and_the_parameter_model_decide_feasibility() -> None:
    points, infeasible = feasible_points(SPACE, {"other": 1}, _identity)
    pairs = {(p["fast"], p["slow"]) for p in points}
    assert pairs == {(5, 10), (5, 20), (5, 30), (10, 20), (10, 30)}
    assert infeasible == 4  # 3 constraint violations + 1 model invariant
    assert all(p["other"] == 1 for p in points)  # the base parameters are kept


def test_grid_search_is_refused_above_the_trial_budget() -> None:
    points, _ = feasible_points(SPACE, {}, _identity)
    with pytest.raises(ValueError, match="above the budget"):
        Searcher(SearchMethod.GRID, SPACE, points, budget=3, seed=0)
    grid = Searcher(SearchMethod.GRID, SPACE, points, budget=10, seed=0)
    seen = []
    while (p := grid.next()) is not None:
        seen.append(p)
        grid.report(1.0)
    assert seen == points


@pytest.mark.parametrize("method", [SearchMethod.RANDOM, SearchMethod.BAYESIAN])
def test_random_and_bayesian_search_are_reproducible_from_the_seed(method: SearchMethod) -> None:
    space = ParameterSpace(
        dimensions=(
            Dimension(name="a", low=D(1), high=D(10), step=D(1), integer=True),
            Dimension(name="b", low=D(1), high=D(5), step=D(1), integer=True),
        )
    )
    points, _ = feasible_points(space, {}, _identity)

    def run(seed: int) -> list[dict[str, Any]]:
        s = Searcher(method, space, points, budget=8, seed=seed, initial_random=3)
        out = []
        while (p := s.next()) is not None:
            out.append(p)
            s.report(-((p["a"] - 7) ** 2) - (p["b"] - 2) ** 2)  # a smooth objective
        return out

    first, again, other = run(3), run(3), run(4)
    assert first == again
    assert first != other
    assert len({repr(sorted(p.items())) for p in first}) == 8  # no point is tried twice


def test_neighbors_are_one_step_along_one_parameter() -> None:
    points, _ = feasible_points(SPACE, {}, _identity)
    found = neighbors(SPACE, {"fast": 10, "slow": 20}, points)
    assert {(p["fast"], p["slow"]) for p in found} == {(5, 20), (10, 30)}


@pytest.mark.parametrize("family", sorted(DEFAULT_SPACES))
def test_every_default_space_is_feasible_for_its_strategy(family: str) -> None:
    registry = default_registry()
    strategy = next(s for s in registry if s.family.value.lower() == family)
    base = catalog.parameters_json(strategy.default_parameters())

    def validate(raw: dict[str, Any]) -> dict[str, Any]:
        return catalog.parameters_json(strategy.parse_parameters(raw))

    points, _ = feasible_points(DEFAULT_SPACES[family], base, validate)
    assert len(points) >= 4
    assert len(points) <= 30  # small enough for a grid within the default trial budget


def test_experiment_configuration_rules() -> None:
    base = {
        "name": "x",
        "start_date": "2026-01-02",
        "end_date": "2026-06-30",
        "universe": {"mode": "EXPLICIT", "symbols": ["A"]},
    }
    with pytest.raises(ValueError, match="one strategy at a time"):
        ExperimentConfig.model_validate(
            {**base, "strategies": [{"strategy_id": "a"}, {"strategy_id": "b"}]}
        )
    with pytest.raises(ValueError, match="does not search"):
        ExperimentConfig.model_validate(
            {**base, "kind": "EVALUATION", "search": "GRID", "strategies": [{"strategy_id": "a"}]}
        )
    ok = ExperimentConfig.model_validate(
        {**base, "kind": "EVALUATION", "search": "NONE", "strategies": [{"strategy_id": "a"}]}
    )
    assert ok.sha256() == ExperimentConfig.model_validate(ok.model_dump()).sha256()


def test_stress_scales_costs_within_their_limits_as_a_new_model() -> None:
    model = backtest_model().definition()
    double = stressed_model(model, ("commission_per_unit", "commission_maximum_rate"), D(2))
    eq, eq2 = model["classes"]["EQUITY"], double["classes"]["EQUITY"]
    assert D(eq2["commission_per_unit"]) == 2 * D(eq["commission_per_unit"])
    assert D(eq2["slippage_bps"]) == D(eq["slippage_bps"])  # untouched
    assert double["code"] != model["code"]
    huge = stressed_model(model, ("commission_maximum_rate",), D(50))
    assert D(huge["classes"]["EQUITY"]["commission_maximum_rate"]) == D("0.1")  # field bound


# ---------------------------------------------------------------------- evaluation
DAYS = [date(2026, 3, 2) + timedelta(days=i) for i in range(10)]


def _snap(day: date, equity: str, costs: str = "0", open_positions: int = 0) -> Any:
    return SimpleNamespace(
        session=day, equity=D(equity), total_costs=D(costs), open_positions=open_positions
    )


def _trade(first: date, last: date | None, pnl: str, costs: str = "1", long: bool = True) -> Any:
    return SimpleNamespace(
        first_session=first,
        last_session=last,
        status="CLOSED" if last else "OPEN_AT_END",
        net_pnl=D(pnl),
        costs=D(costs),
        long=long,
    )


def test_window_evaluation_uses_only_the_window_and_purges_its_boundary() -> None:
    snaps = [_snap(d, str(100_000 + 100 * i), str(i)) for i, d in enumerate(DAYS)]
    trades = [
        _trade(DAYS[0], DAYS[2], "50"),  # before the window
        _trade(DAYS[4], DAYS[6], "30"),
        _trade(DAYS[5], DAYS[7], "-10"),
        _trade(DAYS[7], None, "5"),  # open at the window end
    ]
    fills = [SimpleNamespace(session=DAYS[5], quantity=D(10), price=D(100))]
    result = SimpleNamespace(snapshots=snaps, trades=trades, fills=fills)
    w = ev.evaluate_window(result, D(100_000), DAYS[4], DAYS[8], min_trades=2)  # type: ignore[arg-type]
    assert w.sessions == DAYS[4:9]
    assert w.start_equity == 100_300  # the previous session's valuation
    assert w.daily_returns[0] == pytest.approx(100 / 100_300)
    assert w.net_return == pytest.approx(100_800 / 100_300 - 1)
    assert w.costs == pytest.approx(5.0)  # 8 - 3
    assert w.trades.count == 2
    assert w.trades.net_pnl == pytest.approx(20)
    assert w.purge == {"opened_before_window": 1, "open_at_window_end": 1}
    assert w.trades.status is ResultStatus.OK  # 2 trades >= min 2
    assert ev.objective(w, "NET_EXPECTANCY", min_trades=3) is None  # too few to select on
    assert ev.objective(w, "NET_EXPECTANCY", min_trades=2) == pytest.approx(10)


def test_trade_statistics_on_a_known_vector() -> None:
    trades = [_trade(DAYS[0], DAYS[1], p) for p in ("100", "-50", "30", "-20", "40")]
    s = ev.trade_stats(trades, minimum=30)
    assert s.win_rate == pytest.approx(0.6)
    assert s.profit_factor == pytest.approx(170 / 70)
    assert s.expectancy == pytest.approx(20)
    assert s.average_loss == pytest.approx(-35)
    assert s.largest_share_of_profit == pytest.approx(170 / 100)
    assert s.status is ResultStatus.INSUFFICIENT_SAMPLE


def _window(trades: list[Any], returns: list[float]) -> ev.WindowEvaluation:
    return ev.WindowEvaluation(
        first=DAYS[0],
        last=DAYS[-1],
        sessions=DAYS[: len(returns)],
        daily_returns=returns,
        gross_returns=returns,
        start_equity=100_000,
        end_equity=100_000,
        net_return=sum(returns),
        gross_return=sum(returns),
        max_drawdown=0.02,
        sharpe=None,
        sortino=None,
        costs=0,
        turnover=None,
        trades=ev.trade_stats(trades),
    )


def test_findings_never_conclude_on_insufficient_evidence() -> None:
    few = [_trade(DAYS[0], DAYS[1], "100", "1") for _ in range(5)]
    found, reasons = ev.findings(
        oos=_window(few, [0.001] * 10),
        fold_net_returns=[0.01, 0.02],
        in_sample_sharpe=1.0,
        oos_sharpe=2.0,
        regime_stats={},
        dsr=0.99,
        pbo=0.1,
        cost_stress_net=50,
        neighbor_positive_share=1.0,
        thresholds=ev.Thresholds(),
    )
    assert Finding.INSUFFICIENT_EVIDENCE in found
    assert Finding.CONSISTENT not in found
    assert "closed trades (need 30)" in reasons[0]


def test_findings_identify_costs_concentration_degradation_and_overfitting() -> None:
    # Gross positive, net negative; one trade carries the profit.
    trades = [_trade(DAYS[0], DAYS[1], "-1", "2") for _ in range(40)] + [
        _trade(DAYS[0], DAYS[1], "30", "2")
    ]
    found, _ = ev.findings(
        oos=_window(trades, [0.0001] * 80),
        fold_net_returns=[-0.01, 0.0],
        in_sample_sharpe=2.0,
        oos_sharpe=0.1,
        regime_stats={
            "UP": ev.trade_stats([_trade(DAYS[0], DAYS[1], "5")] * 12, 10),
            "DOWN": ev.trade_stats([_trade(DAYS[0], DAYS[1], "-5")] * 12, 10),
        },
        dsr=0.4,
        pbo=0.7,
        cost_stress_net=-5,
        neighbor_positive_share=0.2,
        thresholds=ev.Thresholds(),
    )
    for expected in (
        Finding.UNPROFITABLE_AFTER_COSTS,
        Finding.DEGRADES_OUT_OF_SAMPLE,
        Finding.REGIME_DEPENDENT,
        Finding.LIKELY_OVERFIT,
        Finding.PARAMETER_UNSTABLE,
    ):
        assert expected in found
    assert Finding.CONSISTENT not in found


def test_consistency_needs_every_gate() -> None:
    trades = [_trade(DAYS[0], DAYS[1], p) for p in ["20", "-10"] * 20]
    common: dict[str, Any] = {
        "oos": _window(trades, [0.001] * 80),
        "fold_net_returns": [0.01, 0.02, 0.01],
        "in_sample_sharpe": 1.0,
        "oos_sharpe": 0.9,
        "regime_stats": {},
        "pbo": 0.2,
        "cost_stress_net": 10,
        "neighbor_positive_share": 1.0,
        "thresholds": ev.Thresholds(),
    }
    assert Finding.CONSISTENT in ev.findings(dsr=0.97, **common)[0]
    assert Finding.CONSISTENT not in ev.findings(dsr=0.9, **common)[0]  # luck not ruled out


# ---------------------------------------------------------------------- monitoring
def _trades(mean: float, n: int, cost: float = 0.001) -> list[dict[str, float]]:
    out = []
    for i in range(n):
        r = mean + (0.02 if i % 2 else -0.02)
        out.append({"return": r, "net_pnl": r * 10_000, "cost_rate": cost})
    return out


def test_performance_checks_flag_a_real_shift_and_respect_minimum_samples() -> None:
    rule = MonitorRule()
    same = performance_checks(
        _trades(0.01, 60), _trades(0.01, 60), rule, reference_drawdown=0.05, capital=100_000
    )
    assert {m.metric: m.status for m in same}[
        MonitorMetric.EXPECTANCY_SHIFT
    ] is MeasurementStatus.OK
    worse = performance_checks(
        _trades(0.01, 60),
        _trades(-0.01, 60, cost=0.003),
        rule,
        reference_drawdown=0.02,
        capital=100_000,
    )
    status = {m.metric: m.status for m in worse}
    assert status[MonitorMetric.EXPECTANCY_SHIFT] is MeasurementStatus.BREACH
    assert status[MonitorMetric.COST_PER_TRADE] is MeasurementStatus.BREACH  # 3x the cost
    few = performance_checks(
        _trades(0.01, 60), _trades(-0.05, 10), rule, reference_drawdown=0.02, capital=100_000
    )
    assert all(m.status is MeasurementStatus.INSUFFICIENT_DATA for m in few)


def test_feature_drift_needs_enough_observations() -> None:
    import numpy as np

    rng = np.random.default_rng(0)
    rule = MonitorRule()
    out = feature_checks(
        {"rsi": list(rng.normal(50, 10, 500)), "zscore": list(rng.normal(0, 1, 50))},
        {"rsi": list(rng.normal(65, 10, 500)), "zscore": list(rng.normal(0, 1, 50))},
        rule,
    )
    by = {(m.metric, m.feature): m.status for m in out}
    assert by[(MonitorMetric.FEATURE_PSI, "rsi")] is MeasurementStatus.BREACH
    assert by[(MonitorMetric.FEATURE_PSI, "zscore")] is MeasurementStatus.INSUFFICIENT_DATA
    assert by[(MonitorMetric.FEATURE_KS, "rsi")] is MeasurementStatus.BREACH  # D = 0.5
    # A tiny but "significant" shift on a huge sample is not material.
    tiny = feature_checks(
        {"x": list(rng.normal(0, 1, 50_000))}, {"x": list(rng.normal(0.05, 1, 50_000))}, rule
    )
    ks = next(m for m in tiny if m.metric is MonitorMetric.FEATURE_KS)
    assert ks.p_value is not None
    assert ks.p_value < 0.01
    assert ks.status is MeasurementStatus.OK


# ---------------------------------------------------------------------- models
def _run(test: dict[str, Any], base: dict[str, Any], candidates: int = 3) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        model_type=ModelType.XGBOOST,
        task=TaskKind.CLASSIFICATION,
        test_metrics=test,
        baseline_metrics={"test": base},
        validation_metrics={"ece": 0.01},
        walk_forward={
            "metric": "log_loss",
            "fold_details": [{"metric": 0.69, "baseline_metric": 0.70}] * 3,
        },
        validation_status=None,
        test_period_reuse=0,
        periods={},
        selection={"candidates": candidates},
    )


def _financial(mean: float, trades: int) -> dict[str, Any]:
    return {
        "mean_net_return": mean,
        "std_net_return": 0.02,
        "trades": trades,
        "round_trip_cost": 0.001,
        "take_all_mean_net_return": -0.001,
    }


def test_a_better_classifier_without_financial_value_is_not_incremental() -> None:
    run = _run(
        {"log_loss": 0.68, "brier": 0.24, "ece": 0.02, "financial": _financial(-0.0005, 5000)},
        {"log_loss": 0.69, "brier": 0.25, "financial_take_all": {"mean_net_return": -0.001}},
    )
    row = assess_run(run, concurrency=5, tests=3, approved=False)
    assert row["predictive"]["beats_base_rate"] is True
    assert row["incremental_value"] is False
    assert any("loses money after costs" in r for r in row["reasons"])


def test_incremental_value_requires_significance_after_overlap_and_multiple_tests() -> None:
    strong = _run(
        {"log_loss": 0.68, "brier": 0.24, "financial": _financial(0.004, 5000)},
        {"log_loss": 0.69, "brier": 0.25},
    )
    assert assess_run(strong, concurrency=5, tests=3, approved=False)["incremental_value"] is True
    # The same evidence with heavy overlap and many tries is no longer significant.
    weak = assess_run(strong, concurrency=200, tests=500, approved=False)
    assert weak["incremental_value"] is False
    assert weak["financial"]["effective_observations"] == pytest.approx(25)


def test_walk_forward_wins_follow_each_metrics_direction() -> None:
    from app.learning.models import _fold_wins

    folds = [{"metric": 0.9, "baseline_metric": 1.0}, {"metric": 1.1, "baseline_metric": 1.0}]
    assert _fold_wins({"metric": "rmse", "fold_details": folds})["beating_baseline"] == 1
    assert _fold_wins({"metric": "log_loss", "fold_details": folds[:1]})["beating_baseline"] == 1
    assert _fold_wins({"metric": "roc_auc", "fold_details": folds[:1]})["beating_baseline"] == 0
    assert _fold_wins({"metric": "mystery", "fold_details": folds})["folds"] == 0


# ---------------------------------------------------------------------- lifecycle
def test_the_lifecycle_table_only_reaches_approved_through_shadow() -> None:
    reaching = {s for s, targets in TRANSITIONS.items() if CandidateStatus.APPROVED in targets}
    assert reaching == {CandidateStatus.SHADOW}
    assert TRANSITIONS[CandidateStatus.REJECTED] == frozenset()
    assert TRANSITIONS[CandidateStatus.RETIRED] == frozenset()  # rollback is separate


def test_the_system_actor_can_never_change_a_lifecycle_status() -> None:
    svc = GovernanceService.__new__(GovernanceService)
    cand = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(GovernanceError, match="only an operator"):
        svc._event(
            None,  # type: ignore[arg-type]  # refused before any database access
            cand,  # type: ignore[arg-type]
            CandidateStatus.APPROVED,
            reason="automatic promotion",
            actor="degradation-monitor",
            actor_kind=ActorKind.SYSTEM,
        )
    with pytest.raises(GovernanceError, match="reason"):
        svc._event(None, cand, CandidateStatus.REJECTED, reason=" ", actor="op")  # type: ignore[arg-type]
