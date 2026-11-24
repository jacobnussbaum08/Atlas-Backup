"""ML engine command line (Phase 5A): ``python -m app.ml_engine.cli``.

* ``build-dataset --spec FILE``: build and store a dataset version from a JSON
  ``DatasetSpec`` (``config/ml/`` holds examples). Prints the version, counts, warnings.
* ``verify-dataset --version ID``: rebuild from the stored specification and compare
  content hashes (reproducibility check; nothing is written).
* ``train --spec FILE`` (a JSON ``TrainingSpec``) or ``train --dataset-version ID --model
  TYPE``: one training run; registers a CANDIDATE model version.
* ``runs`` / ``models`` / ``run --id ID``: what is recorded.
* ``transition --model ID --status STATUS --reason TEXT``: an explicit lifecycle change.
* ``predict --model ID --symbol SYM --at ISO [--timeframe] [--direction] [--mode]``:
  point-in-time inference (RECORDED inputs), recorded in ``ml_predictions``.
* ``replay --prediction ID``: recompute a stored prediction.

Nothing here submits, sizes or approves a trade.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from app.core.clocks import SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging
from app.data_engine.runtime import build_market_data_context
from app.database.models import Asset
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.ml_engine import queries
from app.ml_engine.datasets.builder import DatasetBuildError, build_dataset
from app.ml_engine.datasets.spec import DatasetSpec
from app.ml_engine.datasets.store import DatasetNotFoundError, save_dataset, verify_dataset
from app.ml_engine.enums import LifecycleStatus, ModelType, PredictionMode
from app.ml_engine.inference.engine import InferenceError
from app.ml_engine.registry import lifecycle
from app.ml_engine.runtime import build_ml_context
from app.ml_engine.training.trainer import TrainingError, TrainingSpec
from app.strategies.enums import Direction

ACTOR = "operator:cli"


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.ml_engine.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("build-dataset")
    cmd.add_argument("--spec", required=True)
    sub.add_parser("verify-dataset").add_argument("--version", type=uuid.UUID, required=True)
    cmd = sub.add_parser("train")
    cmd.add_argument("--spec")
    cmd.add_argument("--dataset-version", type=uuid.UUID)
    cmd.add_argument("--model", type=lambda v: ModelType(v.upper()))
    sub.add_parser("runs")
    sub.add_parser("models")
    sub.add_parser("run").add_argument("--id", type=uuid.UUID, required=True)
    cmd = sub.add_parser("transition")
    cmd.add_argument("--model", type=uuid.UUID, required=True)
    cmd.add_argument("--status", type=lambda v: LifecycleStatus(v.upper()), required=True)
    cmd.add_argument("--reason", required=True)
    cmd = sub.add_parser("predict")
    cmd.add_argument("--model", type=uuid.UUID, required=True)
    cmd.add_argument("--symbol", required=True)
    cmd.add_argument("--at", type=_timestamp, required=True)
    cmd.add_argument("--timeframe", default="1Day")
    cmd.add_argument("--direction", type=lambda v: Direction(v.upper()), default=Direction.LONG)
    cmd.add_argument(
        "--mode", type=lambda v: PredictionMode(v.upper()), default=PredictionMode.APPROVED
    )
    sub.add_parser("replay").add_argument("--prediction", type=uuid.UUID, required=True)
    return parser


def _run(argv: list[str] | None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=settings.service_name,
        environment=settings.environment.value,
    )
    engine = build_engine(settings)
    clock = SystemClock()
    try:
        sessions = build_session_factory(engine)
        market = build_market_data_context(sessions, clock)
        ctx = build_ml_context(
            sessions, clock, provider=market.provider.provider_id, feed=market.provider.feed
        )
        out: Any
        if args.command == "build-dataset":
            spec = DatasetSpec.model_validate(_json(args.spec))
            with session_scope(sessions) as session:
                built = build_dataset(
                    session,
                    spec,
                    provider=ctx.provider,
                    feed=ctx.feed,
                    calendar=ctx.calendar,
                    now=clock.now(),
                    max_observations=ctx.settings.max_observations,
                )
                version = save_dataset(
                    session, built, actor=ACTOR, provider=ctx.provider, feed=ctx.feed
                )
                out = {
                    "dataset_version_id": version.id,
                    "dataset": spec.name,
                    "version": version.version,
                    "origin": version.origin.value,
                    "data_known_at": version.data_known_at,
                    "observations": version.observation_count,
                    "matured": version.matured_count,
                    "assets": version.asset_count,
                    "content_sha256": version.content_sha256,
                    "statistics": version.statistics,
                    "warnings": version.warnings,
                }
        elif args.command == "verify-dataset":
            with session_scope(sessions) as session:
                check = verify_dataset(
                    session, args.version, calendar=ctx.calendar, now=clock.now()
                )
            out = {**check.__dict__, "reproduced": check.reproduced}
        elif args.command == "train":
            if args.spec:
                training = TrainingSpec.model_validate(_json(args.spec))
            elif args.dataset_version and args.model:
                training = TrainingSpec(
                    dataset_version_id=args.dataset_version, model_type=args.model
                )
            else:
                raise TrainingError("give --spec, or --dataset-version and --model")
            result = ctx.trainer.train(training, actor=ACTOR)
            out = result.__dict__
        elif args.command == "runs":
            with session_scope(sessions) as session:
                out = [r.model_dump(mode="json") for r in queries.runs(session, limit=200)]
        elif args.command == "models":
            with session_scope(sessions) as session:
                out = [m.model_dump(mode="json") for m in queries.models(session, limit=200)]
        elif args.command == "run":
            with session_scope(sessions) as session:
                found = queries.run(session, args.id)
            out = None if found is None else found.model_dump(mode="json")
        elif args.command == "transition":
            with session_scope(sessions) as session:
                result_t = lifecycle.transition(
                    session, args.model, args.status, reason=args.reason, actor=ACTOR
                )
                out = {
                    "model": str(args.model),
                    "from": result_t.previous.value,
                    "to": result_t.event.to_status.value,
                }
        elif args.command == "predict":
            with session_scope(sessions) as session:
                asset = session.scalar(select(Asset).where(Asset.symbol == args.symbol.upper()))
                if asset is None:
                    raise InferenceError(f"no asset {args.symbol}")
                asset_id = asset.id
            prediction = ctx.inference.predict_at(
                args.model,
                asset_id=asset_id,
                timeframe=args.timeframe,
                direction=args.direction,
                decision_at=args.at,
                calendar=ctx.calendar,
                mode=args.mode,
                actor=ACTOR,
            )
            out = prediction.__dict__
        else:
            out = ctx.inference.replay(args.prediction)
        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid specification"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except AtlasError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
    except (
        DatasetBuildError,
        DatasetNotFoundError,
        TrainingError,
        InferenceError,
        lifecycle.ModelNotFoundError,
    ) as exc:
        print(f"ml_error: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
