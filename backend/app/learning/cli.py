"""Validation laboratory command line (Phase 8B): ``python -m app.learning.cli``.

* ``run --config FILE.json``: create a validation run from an experiment configuration (the
  API's body) and execute it in this process.
* ``list`` / ``show RUN_ID``: recent runs; one run's summary, folds and robustness.
* ``reproduce RUN_ID``: re-execute a run from its stored configuration; reports whether the
  result hash is identical.
* ``holdout RUN_ID``: evaluate a completed run's untouched final period (numbered access).
* ``models [--store]``: compare the stored 5A training runs (and optionally record it).
* ``candidate-strategy STRATEGY --run RUN_ID --reason TEXT``: a challenger with the run's
  frozen final parameters. ``transition CANDIDATE ACTION --reason TEXT [--run ID]
  [--replaces ID]``: an explicit lifecycle decision.
* ``monitor``: one degradation-monitoring pass.

Simulations over stored historical data only; nothing is ordered, filled or posted.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.clocks import SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging
from app.database.models.learning import ValidationRunRecord
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.learning import models as model_eval
from app.learning import queries
from app.learning.runtime import build_validation_context
from app.learning.walkforward import ExperimentConfig


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.learning.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("run_id", type=uuid.UUID)
    for name in ("reproduce", "holdout"):
        sub.add_parser(name).add_argument("run_id", type=uuid.UUID)
    models = sub.add_parser("models")
    models.add_argument("--store", action="store_true")
    cand = sub.add_parser("candidate-strategy")
    cand.add_argument("strategy_id")
    cand.add_argument("--run", required=True, type=uuid.UUID)
    cand.add_argument("--reason", required=True)
    tr = sub.add_parser("transition")
    tr.add_argument("candidate_id", type=uuid.UUID)
    tr.add_argument(
        "action", choices=["start_validation", "shadow", "approve", "reject", "retire", "rollback"]
    )
    tr.add_argument("--reason", required=True)
    tr.add_argument("--run", type=uuid.UUID)
    tr.add_argument("--replaces", type=uuid.UUID)
    sub.add_parser("monitor")
    for p in sub.choices.values():
        p.add_argument("--actor", default="operator:cli")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=settings.service_name,
        environment=settings.environment.value,
    )
    engine = build_engine(settings)
    try:
        sessions = build_session_factory(engine)
        ctx = build_validation_context(sessions, SystemClock())
        svc, gov = ctx.service, ctx.governance
        if args.command == "run":
            config = ExperimentConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
            run_id = svc.create(config, actor=args.actor)
            print(f"created validation run {run_id}; running...", file=sys.stderr)
            status = svc.execute(run_id)
            with session_scope(sessions) as session:
                _print(queries.run_detail(session, run_id))
            return 0 if status.value == "COMPLETED" else 1
        if args.command == "list":
            with session_scope(sessions) as session:
                _print(queries.runs(session))
            return 0
        if args.command == "show":
            with session_scope(sessions) as session:
                _print(
                    {
                        **queries.run_detail(session, args.run_id),
                        "folds": [
                            {k: v for k, v in f.items() if not k.endswith("series")}
                            for f in queries.folds(session, args.run_id)
                        ],
                        "robustness": queries.robustness(session, args.run_id),
                    }
                )
            return 0
        if args.command == "reproduce":
            new_id = svc.reproduce(args.run_id, actor=args.actor)
            with session_scope(sessions) as session:
                _print(queries.run_detail(session, new_id))
            return 0
        if args.command == "holdout":
            hid = svc.evaluate_holdout(args.run_id, actor=args.actor)
            with session_scope(sessions) as session:
                _print(queries.run_detail(session, args.run_id)["holdout_evaluations"])
            print(f"holdout evaluation {hid}", file=sys.stderr)
            return 0
        if args.command == "models":
            with session_scope(sessions) as session:
                if args.store:
                    print(model_eval.store_comparison(session, actor=args.actor), file=sys.stderr)
                _print(model_eval.compare(session))
            return 0
        if args.command == "candidate-strategy":
            with session_scope(sessions) as session:
                run_row = session.get(ValidationRunRecord, args.run)
                if run_row is None or run_row.summary is None:
                    raise SystemExit("the run does not exist or has not completed")
                params = run_row.summary["final_parameters"][args.strategy_id]
            cid = gov.create_strategy_candidate(
                args.strategy_id,
                params,
                actor=args.actor,
                reason=args.reason,
                source_run_id=args.run,
            )
            with session_scope(sessions) as session:
                _print(queries.candidate_detail(session, cid))
            return 0
        if args.command == "transition":
            fn: Callable[[], object] = {
                "start_validation": lambda: gov.start_validation(
                    args.candidate_id, actor=args.actor, reason=args.reason
                ),
                "shadow": lambda: gov.to_shadow(
                    args.candidate_id, actor=args.actor, reason=args.reason, run_id=args.run
                ),
                "approve": lambda: gov.approve(
                    args.candidate_id, actor=args.actor, reason=args.reason, replaces=args.replaces
                ),
                "reject": lambda: gov.reject(
                    args.candidate_id, actor=args.actor, reason=args.reason
                ),
                "retire": lambda: gov.retire(
                    args.candidate_id, actor=args.actor, reason=args.reason
                ),
                "rollback": lambda: gov.rollback(
                    args.candidate_id, actor=args.actor, reason=args.reason
                ),
            }[args.action]
            fn()
            with session_scope(sessions) as session:
                _print(queries.candidate_detail(session, args.candidate_id))
            return 0
        if args.command == "monitor":
            _id, report = ctx.monitoring.run()
            _print(report)
            return 0
        return 2
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid experiment configuration"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except AtlasError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
