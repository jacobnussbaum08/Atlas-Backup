"""Trading orchestration command line (Phase 10A): ``python -m app.orchestration.cli``.

Operator controls (``--actor operator:<name>`` and ``--reason`` are required; each is an
append-only, numbered, audited ``trading_control_events`` row):

* ``enable``: DISABLED -> ENABLED. The only way new autonomous paper entries start.
* ``pause`` / ``resume``: ENABLED <-> PAUSED.
* ``disable``: ENABLED or PAUSED -> DISABLED.
* ``emergency-stop``: any mode -> EMERGENCY_STOP; engages the risk kill switch and requests
  cancellation of every working ENTRY order (exits are never cancelled). Safe to repeat.
* ``clear-emergency``: EMERGENCY_STOP -> DISABLED (never straight to ENABLED). The kill
  switch stays engaged: release it with ``python -m app.risk.cli kill-switch release``.

Reads: ``status``, ``history``, ``intents [--status S]``, ``jobs [--kind K] [--status S]``.

``--account NAME`` selects the paper account (default ``ORCHESTRATOR_ACCOUNT_NAME``, else the
primary account). Exit status 0 on success, 1 when refused, 2 on bad configuration. Nothing
here submits an order itself; paper trading only.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from pydantic import ValidationError

from app.accounting.config import get_accounting_settings
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
from app.orchestration import controls, queries
from app.orchestration.config import OrchestratorSettings, get_orchestrator_settings
from app.orchestration.enums import ControlAction, IntentStatus, JobKind, JobStatus
from app.orchestration.runtime import (
    ControlContext,
    build_control_context,
    resolve_orchestrated_account,
)

CHANGES = {
    "enable": ControlAction.ENABLE,
    "pause": ControlAction.PAUSE,
    "resume": ControlAction.RESUME,
    "disable": ControlAction.DISABLE,
    "clear-emergency": ControlAction.CLEAR_EMERGENCY,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.orchestration.cli")
    parser.add_argument("--account", help="paper account name (default: the configured one)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (*CHANGES, "emergency-stop"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--actor", required=True, help="operator:<name>")
        cmd.add_argument("--reason", required=True)
    sub.add_parser("status")
    sub.add_parser("history").add_argument("--limit", type=int, default=50)
    cmd = sub.add_parser("intents")
    cmd.add_argument("--status", type=IntentStatus)
    cmd.add_argument("--limit", type=int, default=50)
    cmd = sub.add_parser("jobs")
    cmd.add_argument("--kind", type=JobKind)
    cmd.add_argument("--status", type=JobStatus)
    cmd.add_argument("--limit", type=int, default=50)
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def run(ctx: ControlContext, settings: OrchestratorSettings, args: argparse.Namespace) -> int:
    with session_scope(ctx.session_factory) as session:
        account = resolve_orchestrated_account(session, args.account or settings.account_name)
        account_id, account_name = account.id, account.name
    command = args.command
    if command in CHANGES:
        result = ctx.controls.change(
            account_id, CHANGES[command], actor=args.actor, reason=args.reason
        )
        _print({"account": account_name, "changed": result.changed, **result.state.as_dict()})
        return 0
    if command == "emergency-stop":
        report = ctx.controls.emergency_stop(account_id, actor=args.actor, reason=args.reason)
        _print({"account": account_name, **report.__dict__})
        return 1 if report.cancel_failed else 0
    with session_scope(ctx.session_factory) as session:
        if command == "status":
            out: Any = {
                "account": account_name,
                **queries.overview(
                    session,
                    account_id,
                    ctx.clock.now(),
                    enabled=settings.worker_enabled,
                    tick_seconds=settings.tick_seconds,
                ),
            }
        elif command == "history":
            out = controls.history(session, account_id, args.limit)
        elif command == "intents":
            out = queries.intents(session, account_id, status=args.status, limit=args.limit)
        else:
            out = queries.jobs(session, kind=args.kind, status=args.status, limit=args.limit)
    _print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = get_settings()
        get_accounting_settings()
        orchestration = get_orchestrator_settings()
    except ValidationError as exc:
        print(f"Invalid Atlas configuration:\n{describe_validation_error(exc)}", file=sys.stderr)
        return 2
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid Atlas configuration:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, json_output=settings.log_json)
    engine = build_engine(settings)
    try:
        ctx = build_control_context(build_session_factory(engine), SystemClock())
        return run(ctx, orchestration, args)
    except (AtlasError, ValidationError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
