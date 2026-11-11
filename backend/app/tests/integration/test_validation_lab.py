"""Phase 8B: walk-forward validation end to end against PostgreSQL, on DETERMINISTIC FIXTURE
bars (see ``validation_support``). One validation run is executed for the module and its
stored records are examined by every test; nothing here is market data or a strategy result.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from app.backtesting.config import ResolvedConfig
from app.backtesting.data import sessions_between
from app.database.models.learning import (
    ValidationFoldRecord,
    ValidationHoldoutRecord,
    ValidationRobustnessRecord,
    ValidationRunRecord,
    ValidationSegmentRecord,
    ValidationTrialRecord,
)
from app.database.sessions import session_scope
from app.learning.enums import RobustnessMethod, RunStatus, Verdict
from app.learning.service import ValidationRequestError
from app.learning.walkforward import ExperimentConfig, derive
from app.learning.windows import required_embargo
from app.tests.integration.validation_support import (
    END,
    START,
    ValidationLab,
    experiment,
    make_validation_lab,
    module_engine,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine_8b(server_url: URL, template_database: str) -> Iterator[Engine]:
    yield from module_engine(server_url, template_database)


@pytest.fixture(scope="module")
def lab(engine_8b: Engine) -> ValidationLab:
    return make_validation_lab(engine_8b)


@pytest.fixture(scope="module")
def run(lab: ValidationLab) -> dict[str, Any]:
    config = ExperimentConfig.model_validate(experiment())
    run_id = lab.ctx.service.create(config, actor="operator:test")
    loads_before = len(lab.spy.loads)
    status = lab.ctx.service.execute(run_id)
    with session_scope(lab.bench.factory) as session:
        record = session.get(ValidationRunRecord, run_id)
        assert record is not None
        session.expunge(record)
    assert status is RunStatus.COMPLETED, record.error_message
    return {"id": run_id, "record": record, "loads": lab.spy.loads[loads_before:]}


def _rows(lab: ValidationLab, model: Any, run_id: uuid.UUID) -> list[Any]:
    with session_scope(lab.bench.factory) as session:
        found = list(
            session.scalars(select(model).where(model.run_id == run_id).order_by(model.id))
        )
        for r in found:
            session.expunge(r)
        return found


def test_the_run_completes_with_a_chronological_layout_and_an_untouched_holdout(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    record: ValidationRunRecord = run["record"]
    layout = record.layout
    assert layout is not None
    assert record.summary is not None
    # The required embargo is the longest label: stored horizon 5 + 1 period to live.
    assert layout["required_embargo_sessions"] == required_embargo([("1Day", 5, 1)]) == 6
    assert layout["embargo_sessions"] == 6
    assert record.holdout_start is not None
    assert record.holdout_end == END
    dev_end = date.fromisoformat(layout["development"]["end"])
    sessions = [s.day for s in sessions_between(START, END)]
    gap = [d for d in sessions if dev_end < d < record.holdout_start]
    assert len(gap) >= 6  # the development period ends an embargo before the holdout
    folds = _rows(lab, ValidationFoldRecord, run["id"])
    assert len(folds) == len(layout["folds"]) >= 3
    previous_test_end = date.min
    for f in folds:
        between = [d for d in sessions if f.train_end < d < f.test_start]
        assert len(between) >= f.embargo_sessions
        assert f.test_start > previous_test_end
        assert f.test_end <= dev_end < record.holdout_start
        previous_test_end = f.test_end
        # The out-of-sample replay covered exactly the test window.
        series_days = [date.fromisoformat(s[0]) for s in f.oos_series]
        assert series_days[0] == f.test_start
        assert series_days[-1] == f.test_end
    assert record.summary["holdout"]["status"].startswith("UNTOUCHED")


def test_no_replay_of_the_run_ever_loaded_data_beyond_the_development_period(
    run: dict[str, Any],
) -> None:
    """Independent look-ahead check: every dataset the run loaded ended by the development
    end (trials) or by its own test window's end (folds, stress); none reached the holdout."""
    record: ValidationRunRecord = run["record"]
    assert record.layout is not None
    assert record.holdout_start is not None
    dev_end = date.fromisoformat(record.layout["development"]["end"])
    test_ends = {date.fromisoformat(f["test"]["end"]) for f in record.layout["folds"]}
    assert run["loads"], "the spy saw no load"
    for start, end in run["loads"]:
        assert end <= dev_end < record.holdout_start
        assert end == dev_end or end in test_ends
        assert start >= START


def test_trials_record_the_stored_baseline_and_every_grid_point(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    trials = _rows(lab, ValidationTrialRecord, run["id"])
    assert trials[0].source.value == "STORED"
    assert trials[0].trial_number == 0
    grid = [t for t in trials if t.source.value == "GRID"]
    assert len(grid) == 6
    assert {
        (t.parameters["sma_length"], t.parameters["holding_horizon_periods"]) for t in grid
    } == {(s, h) for s in (3, 5, 10) for h in (2, 4)}
    for t in trials:
        assert t.status.value == "OK"
        assert t.development is not None
        days = [d for d, _ in t.development_returns]
        assert days == sorted(days)
    summary = run["record"].summary
    assert summary["trials_recorded"] == len(trials)


def test_every_robustness_method_is_recorded_with_its_assumptions(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    rows = _rows(lab, ValidationRobustnessRecord, run["id"])
    methods = {r.method for r in rows}
    assert methods >= {
        RobustnessMethod.PBO,
        RobustnessMethod.CPCV,
        RobustnessMethod.PURGED_KFOLD,
        RobustnessMethod.DEFLATED_SHARPE,
        RobustnessMethod.MULTIPLE_TESTING,
        RobustnessMethod.PARAMETER_SENSITIVITY,
        RobustnessMethod.OOS_DEGRADATION,
        RobustnessMethod.MONTE_CARLO_TRADES,
        RobustnessMethod.BLOCK_BOOTSTRAP,
        RobustnessMethod.COST_STRESS,
        RobustnessMethod.SLIPPAGE_STRESS,
        RobustnessMethod.REGIME_SEGMENTATION,
    }
    for r in rows:
        assert r.assumptions
        assert r.limitations
    by = {r.method: r for r in rows}
    cost = by[RobustnessMethod.COST_STRESS].result["levels"]
    # Same parameters at every level: only the stressed assumptions differ.
    assert set(cost) == {"0", "1", "2"}
    assert cost["0"]["costs"] == 0 < cost["1"]["costs"] < cost["2"]["costs"]
    assert cost["0"]["trade_net_pnl"] >= cost["1"]["trade_net_pnl"] >= cost["2"]["trade_net_pnl"]
    cpcv = by[RobustnessMethod.CPCV].result
    assert cpcv["purge_sessions"] == cpcv["embargo_sessions"] == 6
    assert len(cpcv["splits"]) == 6  # C(4, 2)
    dsr = by[RobustnessMethod.DEFLATED_SHARPE].result
    assert dsr["trials_this_run"] >= 7


def test_selection_never_prefers_a_candidate_on_too_few_trades_and_says_why(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    folds = _rows(lab, ValidationFoldRecord, run["id"])
    for f in folds:
        ranking = f.selection["ranking"]
        scored = {r["trial"] for r in ranking}
        assert f.selected_trial in scored or f.selection["note"]
        assert f.selection["min_trades"] == 5


def test_strategy_evaluation_is_segmented_and_the_verdict_never_claims_proof(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    segments = _rows(lab, ValidationSegmentRecord, run["id"])
    kinds = {s.segment_kind.value for s in segments}
    assert {"OVERALL", "ASSET", "DIRECTION", "PERIOD", "MARKET_TREND", "VOLATILITY"} <= kinds
    record: ValidationRunRecord = run["record"]
    assert record.summary is not None
    assert record.layout is not None
    assert record.verdict in set(Verdict)
    assert "not evidence of future performance" in record.summary["statement"]
    oos = record.summary["out_of_sample"]
    champion = record.summary["champion_out_of_sample"]
    assert oos["windows"] == champion["windows"] == len(record.layout["folds"])


def test_completed_results_are_immutable(lab: ValidationLab, run: dict[str, Any]) -> None:
    rid = run["id"]
    statements = [
        ("UPDATE validation_runs SET verdict = 'CANDIDATE_FOR_REVIEW' WHERE id = :id", "immutable"),
        ("DELETE FROM validation_runs WHERE id = :id", "never deleted"),
        (
            "UPDATE validation_trials SET selectable = false WHERE run_id = :id",
            "append-only|cannot|not allowed|modif",
        ),
        ("DELETE FROM validation_folds WHERE run_id = :id", "append-only|cannot|not allowed|modif"),
        (
            "INSERT INTO validation_segment_results (run_id, sample, segment_kind, segment_value, "
            "metrics, status) VALUES (:id, 'OUT_OF_SAMPLE', 'OVERALL', 'x', '{}', 'OK')",
            "only while it runs",
        ),
    ]
    for sql, _message in statements:
        with pytest.raises(DBAPIError), session_scope(lab.bench.factory) as session:
            session.execute(text(sql), {"id": rid})


def test_a_reproduction_from_the_stored_configuration_is_identical(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    new_id = lab.ctx.service.reproduce(run["id"], actor="operator:test")
    with session_scope(lab.bench.factory) as session:
        copy = session.get(ValidationRunRecord, new_id)
        assert copy is not None
        assert copy.status is RunStatus.COMPLETED, copy.error_message
        assert copy.reproduction_of == run["id"]
        assert copy.reproduced_identical is True
        assert copy.result_sha256 == run["record"].result_sha256
        assert (copy.holdout_start, copy.holdout_end) == (
            run["record"].holdout_start,
            run["record"].holdout_end,
        )
        # A reproduction is not a new search: it does not add to the trial count of later runs.
        assert (
            lab.ctx.service.prior_trials(
                session, ExperimentConfig.model_validate(experiment()), uuid.uuid4()
            )
            == run["record"].summary["trials_recorded"]
        )


def test_the_holdout_is_read_only_on_request_and_every_reuse_is_counted(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    rid = run["id"]
    before = len(lab.spy.loads)
    first = lab.ctx.service.evaluate_holdout(rid, actor="operator:test")
    second = lab.ctx.service.evaluate_holdout(rid, actor="operator:test")
    record: ValidationRunRecord = run["record"]
    assert lab.spy.loads[before:] == [(record.holdout_start, record.holdout_end)] * 2
    with session_scope(lab.bench.factory) as session:
        a = session.get(ValidationHoldoutRecord, first)
        b = session.get(ValidationHoldoutRecord, second)
        assert a is not None
        assert b is not None
        assert (a.access_number, b.access_number) == (1, 2)
        assert "no longer an untouched test" in b.result["note"]
        assert record.summary is not None
        assert record.holdout_start is not None
        assert a.parameters == record.summary["final_parameters"]
        assert a.result["first"] == record.holdout_start.isoformat()
    # A run that has not completed cannot have its holdout read, by the service or the DB.
    queued = lab.ctx.service.create(ExperimentConfig.model_validate(experiment()), actor="t")
    with pytest.raises(ValidationRequestError):
        lab.ctx.service.evaluate_holdout(queued, actor="operator:test")
    premature = ValidationHoldoutRecord(
        run_id=queued,
        strategy_ids=["fixture_proposer"],
        holdout_start=record.holdout_start,
        holdout_end=record.holdout_end,
        parameters={},
        parameters_sha256="0" * 64,
        access_number=1,
        result={},
        config_sha256="0" * 64,
        dataset_sha256="0" * 64,
        requested_by="test",
    )
    with pytest.raises(DBAPIError), session_scope(lab.bench.factory) as session:
        session.add(premature)


def test_earlier_sessions_are_identical_whatever_comes_later(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    """Independent look-ahead check of the slicing the folds rely on: a replay through T and
    a replay through T + 15 sessions agree on every session up to T (snapshots, trades
    opened by T), so a training window's statistics cannot depend on later data."""
    base = ResolvedConfig.model_validate(run["record"].base_config)
    sessions = [s.day for s in sessions_between(START, END)]
    t = sessions[40]
    later = sessions[55]
    with session_scope(lab.bench.factory) as session:
        short, _ = lab.ctx.service.runner.replay(session, derive(base, start=START, end=t))
        long, _ = lab.ctx.service.runner.replay(session, derive(base, start=START, end=later))
    a = [(s.session, s.equity, s.total_costs) for s in short.snapshots]
    b = [(s.session, s.equity, s.total_costs) for s in long.snapshots if s.session <= t]
    assert a == b
    opened = [x.as_json()["proposal_seq"] for x in short.trades]
    assert (
        opened
        == [x.as_json()["proposal_seq"] for x in long.trades if x.first_session <= t][: len(opened)]
    )


def test_repeated_searching_is_counted_against_later_runs(
    lab: ValidationLab, run: dict[str, Any]
) -> None:
    second = ExperimentConfig.model_validate(
        experiment(name="second search", search="RANDOM", max_trials=3, seed=1)
    )
    run_id = lab.ctx.service.create(second, actor="operator:test")
    assert lab.ctx.service.execute(run_id) is RunStatus.COMPLETED
    with session_scope(lab.bench.factory) as session:
        record = session.get(ValidationRunRecord, run_id)
        assert record is not None
        assert record.prior_trials == run["record"].summary["trials_recorded"]
        dsr = session.scalar(
            select(ValidationRobustnessRecord).where(
                ValidationRobustnessRecord.run_id == run_id,
                ValidationRobustnessRecord.method == RobustnessMethod.DEFLATED_SHARPE,
            )
        )
        assert dsr is not None
        assert dsr.result["prior_trials_on_overlapping_data"] == record.prior_trials


def test_an_impossible_configuration_is_refused_before_anything_is_stored(
    lab: ValidationLab,
) -> None:
    short = experiment(start_date="2026-09-01", windows={"train_sessions": 60, "test_sessions": 20})
    with pytest.raises(ValidationRequestError, match="one fold needs"):
        lab.ctx.service.create(ExperimentConfig.model_validate(short), actor="operator:test")
    too_short = experiment(
        windows={"train_sessions": 25, "test_sessions": 10, "embargo_sessions": 2}
    )
    with pytest.raises(ValidationRequestError, match="shorter than the longest label"):
        lab.ctx.service.create(ExperimentConfig.model_validate(too_short), actor="operator:test")


def test_a_combined_evaluation_reports_each_strategy_and_their_correlation(
    lab: ValidationLab,
) -> None:
    config = ExperimentConfig.model_validate(
        experiment(
            name="fixture evaluation",
            kind="EVALUATION",
            search="NONE",
            strategies=[{"strategy_id": "fixture_proposer"}],
        )
    )
    run_id = lab.ctx.service.create(config, actor="operator:test")
    assert lab.ctx.service.execute(run_id) is RunStatus.COMPLETED
    with session_scope(lab.bench.factory) as session:
        record = session.get(ValidationRunRecord, run_id)
        assert record is not None
        assert record.summary is not None
        assert record.summary["kind"] == "EVALUATION"
        assert record.summary["trials_recorded"] == 1
        methods = set(
            session.scalars(
                select(ValidationRobustnessRecord.method).where(
                    ValidationRobustnessRecord.run_id == run_id
                )
            )
        )
        assert RobustnessMethod.PBO not in methods  # nothing was selected
        assert RobustnessMethod.COST_STRESS in methods
