"""Backtesting command line (Phase 8A): ``python -m app.backtesting.cli``.

* ``run --config FILE.json [--actor operator:NAME]``: create a run from a request (JSON,
  the API's body) and execute it in this process.
* ``list``: recent runs.
* ``show RUN_ID``: one run's configuration summary, portfolio metrics and benchmarks.
* ``reproduce RUN_ID``: re-execute a completed run from its stored configuration and report
  whether the results are identical (same result hash).
* ``fail-stale``: mark runs whose process ended mid-replay as FAILED.

Simulations over stored historical data only. Nothing is ordered, filled or posted to the
paper account; results go to the ``backtest_*`` tables.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.backtesting import queries
from app.backtesting.config import BacktestRequest
from app.backtesting.service import BacktestService
from app.core.clocks import SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging
from app.database.sessions import build_engine, build_session_factory, session_scope


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backtesting.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--actor", default="operator:cli")
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("run_id", type=uuid.UUID)
    rep = sub.add_parser("reproduce")
    rep.add_argument("run_id", type=uuid.UUID)
    rep.add_argument("--actor", default="operator:cli")
    sub.add_parser("fail-stale")
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
        service = BacktestService(sessions, SystemClock())
        if args.command == "run":
            request = BacktestRequest.model_validate_json(args.config.read_text(encoding="utf-8"))
            run_id = service.create(request, actor=args.actor)
            print(f"created backtest run {run_id}; replaying...", file=sys.stderr)
            status = service.execute(run_id)
            with session_scope(sessions) as session:
                _print(queries.run_summary(queries._run(session, run_id)))
            return 0 if status.value == "COMPLETED" else 1
        if args.command == "list":
            with session_scope(sessions) as session:
                _print(queries.runs(session))
            return 0
        if args.command == "show":
            with session_scope(sessions) as session:
                detail = queries.run_detail(session, args.run_id)
                detail.pop("config", None)
                detail.pop("request", None)
                _print(
                    {
                        **detail,
                        "portfolio_metrics": queries.metrics(
                            session, args.run_id, scope="PORTFOLIO"
                        ),
                        "benchmarks": [
                            {k: v for k, v in b.items() if k != "details"}
                            for b in queries.benchmarks(session, args.run_id)
                        ],
                    }
                )
            return 0
        if args.command == "reproduce":
            new_id = service.reproduce(args.run_id, actor=args.actor)
            with session_scope(sessions) as session:
                _print(queries.run_summary(queries._run(session, new_id)))
            return 0
        if args.command == "fail-stale":
            print(f"{service.fail_stale()} stale run(s) marked FAILED")
            return 0
        return 2
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid backtest request"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except AtlasError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
