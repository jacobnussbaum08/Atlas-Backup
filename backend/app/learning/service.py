"""Creating, executing, storing and reproducing validation runs (Phase 8B).

* :meth:`ValidationService.create` validates an :class:`ExperimentConfig`, resolves its base
  replay configuration once (strategy versions, risk configuration, execution model and the
  dataset knowledge time are pinned), plans the session layout (which fixes the holdout
  before anything runs) and stores the experiment and a QUEUED run.
* :meth:`ValidationService.execute` runs the experiment in memory and stores every result in
  one transaction together with COMPLETED and the result hash; progress is committed as it
  goes. A failure marks the run FAILED; partial results are not stored.
* :meth:`ValidationService.reproduce` re-executes a run from its stored configuration and
  records whether the result hash is identical.
* :meth:`ValidationService.evaluate_holdout` is the only code that reads a run's untouched
  final period: it replays the run's frozen final parameters there once per request and
  numbers the access, so a reused holdout is visible as such.

Only ``validation_*`` tables are written; nothing here touches strategy versions, model
statuses, risk limits or any trading table.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.config import ResolvedConfig, sha256_of
from app.backtesting.data import sessions_between
from app.backtesting.service import BacktestService
from app.core.clocks import Clock
from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.database.models.learning import (
    ValidationExperimentRecord,
    ValidationFoldRecord,
    ValidationHoldoutRecord,
    ValidationRobustnessRecord,
    ValidationRunRecord,
    ValidationSegmentRecord,
    ValidationTrialRecord,
)
from app.database.sessions import session_scope
from app.learning import evaluation as ev
from app.learning.enums import ExperimentKind, RunStatus, Sample
from app.learning.search import feasible_points
from app.learning.walkforward import (
    VALIDATION_ENGINE_VERSION,
    ExperimentConfig,
    ExperimentResult,
    WalkForwardRunner,
    base_request,
    derive,
)
from app.learning.windows import Layout

logger = get_logger(__name__)
STALE_AFTER = timedelta(hours=6)


class ValidationRequestError(InvalidRequestError):
    """The experiment cannot be run as configured (the API answers 400)."""


def _series(w: ev.WindowEvaluation) -> list[list[Any]]:
    equity = w.start_equity
    out: list[list[Any]] = []
    for d, r in zip(w.sessions, w.daily_returns, strict=True):
        equity *= 1 + r
        out.append([d.isoformat(), r, round(equity, 2)])
    return out


class ValidationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        backtests: BacktestService,
        *,
        version: Callable[[], str] | None = None,
    ) -> None:
        self._sessions = session_factory
        self.clock = clock
        self.backtests = backtests
        self.runner = WalkForwardRunner(backtests)
        self._version = version or backtests._version

    # ------------------------------------------------------------------ planning
    def plan(self, config: ExperimentConfig, base: ResolvedConfig) -> Layout:
        """The session layout the run will use (raises on an impossible configuration)."""
        sessions = [s.day for s in sessions_between(config.start_date, config.end_date)]
        param_sets = [rs.parameters for rs in base.strategies]
        if config.kind is ExperimentKind.WALK_FORWARD:
            rs = base.strategies[0]
            space = self.runner._space(config, rs)
            points, _ = feasible_points(
                space, rs.parameters, self.runner._validator(rs.strategy_id)
            )
            if not points:
                raise ValidationRequestError("no feasible point in the parameter space")
            param_sets += points
        return self.runner._layout(sessions, config, param_sets)

    # ------------------------------------------------------------------ create
    def create(
        self, config: ExperimentConfig, *, actor: str, reproduction_of: uuid.UUID | None = None
    ) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            try:
                base = self.backtests.resolve(session, base_request(config))
                layout = self.plan(config, base)
            except ValueError as exc:
                raise ValidationRequestError(str(exc)) from None
            # Pin the knowledge time and strategy versions into the stored configuration.
            pinned = config.model_copy(
                update={
                    "data": config.data.model_copy(update={"data_known_at": base.known_at}),
                    "strategies": tuple(
                        s.model_copy(update={"version": rs.version})
                        for s, rs in zip(
                            sorted(config.strategies, key=lambda x: x.strategy_id),
                            base.strategies,
                            strict=True,
                        )
                    ),
                }
            )
            experiment = ValidationExperimentRecord(
                name=config.name,
                kind=config.kind,
                config=pinned.model_dump(mode="json"),
                config_sha256=pinned.sha256(),
                strategy_ids=[rs.strategy_id for rs in base.strategies],
                search_method=config.search,
                max_trials=config.max_trials,
                created_by=actor,
            )
            session.add(experiment)
            session.flush()
            run_id = self._add_run(
                session, experiment.id, base, layout, pinned, actor, reproduction_of
            )
            return run_id

    def _add_run(
        self,
        session: Session,
        experiment_id: uuid.UUID,
        base: ResolvedConfig,
        layout: Layout,
        config: ExperimentConfig,
        actor: str,
        reproduction_of: uuid.UUID | None,
    ) -> uuid.UUID:
        s = layout.sessions
        run = ValidationRunRecord(
            experiment_id=experiment_id,
            status=RunStatus.QUEUED,
            base_config=base.as_json(),
            base_config_sha256=base.sha256(),
            engine_version=VALIDATION_ENGINE_VERSION,
            code_version=base.code_version[:80],
            seed=config.seed,
            start_date=config.start_date,
            end_date=config.end_date,
            data_known_at=base.known_at,
            requested_by=actor,
            reproduction_of=reproduction_of,
            holdout_start=None if layout.holdout is None else s[layout.holdout.start],
            holdout_end=None if layout.holdout is None else s[layout.holdout.end],
            progress={"stage": "queued"},
        )
        session.add(run)
        session.flush()
        return run.id

    # ------------------------------------------------------------------ execute
    def _claim(
        self, run_id: uuid.UUID
    ) -> tuple[ExperimentConfig, ResolvedConfig, uuid.UUID | None]:
        with session_scope(self._sessions) as session:
            run = session.get(ValidationRunRecord, run_id, with_for_update=True)
            if run is None:
                raise NotFoundError(f"validation run {run_id} does not exist")
            if run.status is not RunStatus.QUEUED:
                raise ValidationRequestError(f"validation run {run_id} is {run.status.value}")
            experiment = session.get(ValidationExperimentRecord, run.experiment_id)
            assert experiment is not None  # noqa: S101  # foreign key
            run.status = RunStatus.RUNNING
            run.started_at = self.clock.now()
            return (
                ExperimentConfig.model_validate(experiment.config),
                ResolvedConfig.model_validate(run.base_config),
                run.reproduction_of,
            )

    def _progress(self, run_id: uuid.UUID) -> Callable[[str, int, int], None]:
        def write(stage: str, done: int, total: int) -> None:
            with session_scope(self._sessions) as session:
                session.execute(
                    update(ValidationRunRecord)
                    .where(ValidationRunRecord.id == run_id)
                    .values(progress={"stage": stage, "done": done, "total": total})
                )

        return write

    def prior_trials(self, session: Session, config: ExperimentConfig, run_id: uuid.UUID) -> int:
        """Trials recorded by earlier completed runs of the same strategies on dates that
        overlap this run's: repeated searching of the same history counts against it."""
        e, r, t = ValidationExperimentRecord, ValidationRunRecord, ValidationTrialRecord
        ids = [s.strategy_id for s in config.strategies]
        count = session.scalar(
            select(func.count(t.id))
            .join(r, r.id == t.run_id)
            .join(e, e.id == r.experiment_id)
            .where(
                r.id != run_id,
                r.status == RunStatus.COMPLETED,
                r.reproduction_of.is_(None),
                e.strategy_ids.overlap(ids),
                r.start_date <= config.end_date,
                r.end_date >= config.start_date,
            )
        )
        return int(count or 0)

    def execute(self, run_id: uuid.UUID) -> RunStatus:
        config, base, original = self._claim(run_id)
        try:
            with session_scope(self._sessions) as session:
                # A reproduction re-executes the original's search; it inherits the trial
                # count the original saw instead of counting the original as a prior search.
                source = None if original is None else session.get(ValidationRunRecord, original)
                prior = (
                    self.prior_trials(session, config, run_id)
                    if source is None
                    else int(source.prior_trials or 0)
                )
                result = self.runner.run(
                    session, config, base, prior_trials=prior, progress=self._progress(run_id)
                )
            self._store(run_id, result, prior)
            return RunStatus.COMPLETED
        except Exception as exc:
            logger.warning("validation.run_failed", run_id=str(run_id), error=str(exc))
            with session_scope(self._sessions) as session:
                session.execute(
                    update(ValidationRunRecord)
                    .where(ValidationRunRecord.id == run_id)
                    .values(
                        status=RunStatus.FAILED,
                        finished_at=self.clock.now(),
                        error_message=f"{type(exc).__name__}: {exc}"[:2000],
                    )
                )
            return RunStatus.FAILED

    def fail_stale(self, older_than: timedelta = STALE_AFTER) -> int:
        cutoff = self.clock.now() - older_than
        with session_scope(self._sessions) as session:
            stale = session.scalars(
                select(ValidationRunRecord).where(
                    ValidationRunRecord.status == RunStatus.RUNNING,
                    ValidationRunRecord.started_at < cutoff,
                )
            ).all()
            for run in stale:
                run.status = RunStatus.FAILED
                run.finished_at = self.clock.now()
                run.error_message = "the process running this validation ended before it finished"
            return len(stale)

    def _store(self, run_id: uuid.UUID, result: ExperimentResult, prior: int) -> None:
        with session_scope(self._sessions) as session:
            trial_rows = [
                {
                    "run_id": run_id,
                    "trial_number": t.number,
                    "strategy_id": t.strategy_id,
                    "source": t.source,
                    "status": t.status,
                    "selectable": t.selectable,
                    "parameters": t.parameters,
                    "parameters_sha256": t.parameters_sha256,
                    "config_sha256": t.config_sha256,
                    "dataset_sha256": t.dataset_sha256,
                    "development": None if t.development is None else t.development.as_json(),
                    "development_returns": []
                    if t.development is None
                    else [
                        [d.isoformat(), r]
                        for d, r in zip(
                            t.development.sessions, t.development.daily_returns, strict=True
                        )
                    ],
                    "error": t.error,
                }
                for t in result.trials
            ]
            if trial_rows:
                session.execute(insert(ValidationTrialRecord), trial_rows)
            s = result.layout.sessions
            for f, fw in zip(result.folds, result.layout.folds, strict=True):
                session.add(
                    ValidationFoldRecord(
                        run_id=run_id,
                        fold_index=f.index,
                        refit=f.refit,
                        train_start=s[fw.train.start],
                        train_end=s[fw.train.end],
                        validation_start=None if fw.validation is None else s[fw.validation.start],
                        validation_end=None if fw.validation is None else s[fw.validation.end],
                        test_start=s[fw.test.start],
                        test_end=s[fw.test.end],
                        embargo_sessions=result.layout.embargo,
                        selected_trial=f.selected_trial,
                        selection=f.selection,
                        in_sample=f.in_sample,
                        validation=f.validation,
                        out_of_sample=f.oos.as_json(),
                        champion_out_of_sample=None
                        if f.champion_oos is None
                        else f.champion_oos.as_json(),
                        oos_series=_series(f.oos),
                        champion_series=None if f.champion_oos is None else _series(f.champion_oos),
                        oos_config_sha256=f.oos_config_sha256,
                        oos_dataset_sha256=f.oos_dataset_sha256,
                    )
                )
            for r in result.robustness:
                session.add(
                    ValidationRobustnessRecord(
                        run_id=run_id,
                        method=r.method,
                        scope=r.scope[:200],
                        status=r.status,
                        result=_jsonable(r.result),
                        assumptions=r.assumptions,
                        limitations=r.limitations,
                    )
                )
            for seg in result.segments:
                session.add(
                    ValidationSegmentRecord(
                        run_id=run_id,
                        sample=seg.sample,
                        segment_kind=seg.kind,
                        segment_value=seg.value[:64],
                        metrics=seg.metrics,
                        status=seg.status,
                    )
                )
            session.flush()
            run = session.get(ValidationRunRecord, run_id, with_for_update=True)
            assert run is not None  # noqa: S101
            identical = None
            if run.reproduction_of is not None:
                original = session.get(ValidationRunRecord, run.reproduction_of)
                identical = original is not None and original.result_sha256 == result.sha256()
            run.status = RunStatus.COMPLETED
            run.finished_at = self.clock.now()
            run.prior_trials = prior
            run.layout = result.layout.describe()
            run.summary = _jsonable(result.summary)
            run.verdict = result.verdict
            run.findings = [f.value for f in result.findings]
            run.result_sha256 = result.sha256()
            run.reproduced_identical = identical
            run.progress = {"stage": "completed"}

    # ------------------------------------------------------------------ reproduce
    def prepare_reproduction(self, run_id: uuid.UUID, *, actor: str) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            run = session.get(ValidationRunRecord, run_id)
            if run is None:
                raise NotFoundError(f"validation run {run_id} does not exist")
            if run.status is not RunStatus.COMPLETED:
                raise ValidationRequestError("only a COMPLETED run can be reproduced")
            experiment = session.get(ValidationExperimentRecord, run.experiment_id)
            assert experiment is not None  # noqa: S101
            config = ExperimentConfig.model_validate(experiment.config)
            base = ResolvedConfig.model_validate(run.base_config)
            layout = self.plan(config, base)
            return self._add_run(session, experiment.id, base, layout, config, actor, run_id)

    def reproduce(self, run_id: uuid.UUID, *, actor: str) -> uuid.UUID:
        new_id = self.prepare_reproduction(run_id, actor=actor)
        self.execute(new_id)
        return new_id

    # ------------------------------------------------------------------ holdout
    def evaluate_holdout(self, run_id: uuid.UUID, *, actor: str) -> uuid.UUID:
        """Replay the run's frozen final parameters once on its untouched holdout."""
        with session_scope(self._sessions) as session:
            run = session.get(ValidationRunRecord, run_id)
            if run is None:
                raise NotFoundError(f"validation run {run_id} does not exist")
            if run.status is not RunStatus.COMPLETED or run.summary is None:
                raise ValidationRequestError(
                    "the holdout is evaluated only after the run completed"
                )
            if run.holdout_start is None or run.holdout_end is None:
                raise ValidationRequestError("this run reserved no holdout period")
            experiment = session.get(ValidationExperimentRecord, run.experiment_id)
            assert experiment is not None  # noqa: S101
            base = ResolvedConfig.model_validate(run.base_config)
            params: dict[str, dict[str, Any]] = run.summary["final_parameters"]
            cfg = derive(
                base,
                start=run.holdout_start,
                end=run.holdout_end,
                parameters=params,
                label="holdout",
            )
            result, dataset = self.runner.replay(session, cfg)
            bench = None if base.benchmark is None else uuid.UUID(base.benchmark.asset_id)
            window = ev.evaluate_window(
                result,
                base.initial_capital,
                run.holdout_start,
                run.holdout_end,
                dataset=dataset,
                benchmark=bench,
            )
            previous = self.holdout_accesses(
                session, experiment.strategy_ids, run.holdout_start, run.holdout_end
            )
            record = ValidationHoldoutRecord(
                run_id=run.id,
                strategy_ids=experiment.strategy_ids,
                holdout_start=run.holdout_start,
                holdout_end=run.holdout_end,
                parameters=params,
                parameters_sha256=sha256_of(params),
                access_number=previous + 1,
                result={
                    **window.as_json(),
                    "series": _series(window),
                    "sample": Sample.HOLDOUT.value,
                    "note": "first access: the holdout was untouched"
                    if previous == 0
                    else f"access {previous + 1}: this holdout has been evaluated before for "
                    "these strategies, so it is no longer an untouched test",
                },
                config_sha256=cfg.replay_sha256(),
                dataset_sha256=dataset.sha256(),
                requested_by=actor,
            )
            session.add(record)
            session.flush()
            return record.id

    @staticmethod
    def holdout_accesses(session: Session, strategy_ids: list[str], first: date, last: date) -> int:
        h = ValidationHoldoutRecord
        return int(
            session.scalar(
                select(func.count(h.id)).where(
                    h.strategy_ids.overlap(strategy_ids),
                    h.holdout_start <= last,
                    h.holdout_end >= first,
                )
            )
            or 0
        )


def _jsonable(value: Any) -> Any:
    """Plain JSON (decimals and dates as strings, NaN/inf as None)."""
    import json
    import math

    def default(o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, date):
            return o.isoformat()
        return str(o)

    def clean(o: Any) -> Any:
        if isinstance(o, float) and not math.isfinite(o):
            return None
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, list | tuple):
            return [clean(v) for v in o]
        return o

    return clean(json.loads(json.dumps(value, default=default)))


__all__ = ["ValidationRequestError", "ValidationService"]
