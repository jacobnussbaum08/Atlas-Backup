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

Reads: ``status``, ``history``, ``intents [--status S]``, ``jobs [--kind K] [--status S]``,
(10B) ``protection``, ``metrics``, ``alerts [--open] [--min-severity S]``, ``operators``.

Phase 10B operations (``--actor operator:<name>`` and a reason/note are required, audited):

* ``requeue-job --id N``: a DEAD scheduled job (dead letter) gets a fresh attempt budget.
* ``ack-alert --id N --note ...``: acknowledge an alert (append-only; the alert stays).
* ``operator-add --name N --token-file PATH [--trading]``: issue a named API credential; the
  token is written to the new file PATH (never printed) and only its SHA-256 digest is
  stored. ``--trading`` lets it use the API's trading controls (pause, disable, emergency
  stop, kill switch); nothing lets it enable trading.
* ``operator-revoke --name N``: revoke a credential (permanent; never deleted).

In the paper environment every operator action must name an active operator credential
(``operator-add`` bootstraps the first one). ``--account NAME`` selects the paper account
(default ``ORCHESTRATOR_ACCOUNT_NAME``, else the primary account). Exit status 0 on success,
1 when refused, 2 on bad configuration. Nothing here submits an order; paper trading only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.accounting.config import get_accounting_settings
from app.core.clocks import SystemClock
from app.core.config import (
    Environment,
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging
from app.core.security import operators
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.orchestration import controls, queries, reliability
from app.orchestration.config import OrchestratorSettings, get_orchestrator_settings
from app.orchestration.enums import ControlAction, IntentStatus, JobKind, JobStatus
from app.orchestration.jobs import JobRunner
from app.orchestration.protection import assess
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
    # Phase 10B
    sub.add_parser("protection")
    sub.add_parser("metrics")
    sub.add_parser("operators")
    cmd = sub.add_parser("alerts")
    cmd.add_argument("--open", action="store_true", help="only unacknowledged alerts")
    cmd.add_argument(
        "--min-severity", default="HIGH", choices=["INFO", "WARNING", "HIGH", "CRITICAL"]
    )
    cmd.add_argument("--limit", type=int, default=50)
    cmd = sub.add_parser("requeue-job")
    cmd.add_argument("--id", type=int, required=True)
    cmd.add_argument("--actor", required=True, help="operator:<name>")
    cmd.add_argument("--reason", required=True)
    cmd = sub.add_parser("ack-alert")
    cmd.add_argument("--id", type=int, required=True)
    cmd.add_argument("--actor", required=True, help="operator:<name>")
    cmd.add_argument("--note", required=True)
    cmd = sub.add_parser("operator-add")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--trading", action="store_true", help="may use the API trading controls")
    cmd.add_argument(
        "--token-file",
        required=True,
        help="new file the token is written to (never printed; the file must not exist)",
    )
    cmd.add_argument("--actor", required=True, help="operator:<name>")
    cmd.add_argument("--reason", required=True)
    cmd = sub.add_parser("operator-revoke")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--actor", required=True, help="operator:<name>")
    cmd.add_argument("--reason", required=True)
    return parser


OPERATOR_ACTIONS = (*CHANGES, "emergency-stop", "requeue-job", "ack-alert", "operator-revoke")


def _check_actor(session: Any, actor: str, *, bootstrap: bool = False) -> None:
    """In the paper environment an operator action must name an active credential; the
    very first credential may be issued by anyone with shell access (bootstrap)."""
    name = actor.removeprefix("operator:")
    if operators.active(session, name) is not None:
        return
    if bootstrap and not operators.any_active(session):
        return
    raise operators.OperatorCredentialError(
        f"{actor} is not an active operator credential (paper environment)"
    )


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def run(
    ctx: ControlContext,
    settings: OrchestratorSettings,
    args: argparse.Namespace,
    *,
    environment: Environment | None = None,
) -> int:
    with session_scope(ctx.session_factory) as session:
        account = resolve_orchestrated_account(session, args.account or settings.account_name)
        account_id, account_name = account.id, account.name
        if environment is Environment.PAPER and args.command in (
            *OPERATOR_ACTIONS,
            "operator-add",
        ):
            _check_actor(session, args.actor, bootstrap=args.command == "operator-add")
    command = args.command
    handled = _run_10b(ctx, settings, args, account_id)
    if handled is not None:
        return handled
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


def _run_10b(
    ctx: ControlContext,
    settings: OrchestratorSettings,
    args: argparse.Namespace,
    account_id: Any,
) -> int | None:
    """The Phase 10B commands (None: not one of them)."""
    command = args.command
    now = ctx.clock.now()
    if command == "requeue-job":
        runner = JobRunner(
            ctx.session_factory,
            ctx.clock,
            owner="cli",
            lease=timedelta(seconds=settings.job_lease_seconds),
            max_attempts=settings.job_max_attempts,
        )
        row = runner.requeue(args.id, actor=args.actor, reason=args.reason)
        _print(
            {
                "job_id": row.id,
                "kind": row.kind.value,
                "slot": row.slot,
                "status": row.status.value,
                "note": "the orchestrator retries it when its slot is offered again; a slot "
                "that is no longer offered is superseded by a later run",
            }
        )
        return 0
    if command == "operator-add":
        target = Path(args.token_file)
        if target.exists():
            raise operators.OperatorCredentialError(f"{target} exists: choose a new file")
        with session_scope(ctx.session_factory) as session:
            record, token = operators.issue(
                session,
                args.name,
                can_control_trading=args.trading,
                actor=args.actor,
                reason=args.reason,
                now=now,
            )
            name = record.name
            # Written before the credential commits: a failed write issues nothing.
            with target.open("x", encoding="utf-8") as handle:
                handle.write(token + "\n")
            with contextlib.suppress(OSError):
                target.chmod(0o600)
        _print(
            {
                "operator": f"operator:{name}",
                "can_control_trading": args.trading,
                "token_file": str(target),
                "note": "the token is in that file only (never printed; only its SHA-256 "
                "digest is stored). Send it as X-Atlas-Operator-Token, then delete the file.",
            }
        )
        return 0
    if command == "operator-revoke":
        with session_scope(ctx.session_factory) as session:
            operators.revoke(session, args.name, actor=args.actor, reason=args.reason, now=now)
        _print({"operator": f"operator:{args.name}", "revoked": True})
        return 0
    with session_scope(ctx.session_factory) as session:
        if command == "ack-alert":
            out: Any = reliability.acknowledge(
                session, args.id, actor=args.actor, note=args.note, now=now
            )
        elif command == "alerts":
            out = reliability.alerts(
                session,
                account_id,
                min_severity=args.min_severity,
                open_only=args.open,
                limit=args.limit,
            )
        elif command == "protection":
            out = assess(
                session, now, require_workers=settings.require_protection_workers
            ).as_dict()
        elif command == "metrics":
            out = reliability.metrics(session, account_id, now)
        elif command == "operators":
            out = operators.listing(session)
        else:
            return None
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
        return run(ctx, orchestration, args, environment=settings.environment)
    except (AtlasError, ValidationError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
