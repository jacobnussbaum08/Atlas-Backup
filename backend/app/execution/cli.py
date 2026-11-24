"""Paper execution command line (Phases 7A-7B): ``python -m app.execution.cli``.

**Simulated paper orders only.** The only broker is the isolated paper broker; there is
no brokerage connection, no live mode and no switch that could enable one.

Reads:

* ``orders list [--group open|closed|filled|cancelled|rejected|expired|failed]
  [--status S]``, ``orders show --id ID``, ``orders events``, ``orders requests``
* ``broker status``
* ``positions list [--status open|closed]``, ``positions show --id ID``,
  ``positions summary``, ``analytics``, ``models`` (Phase 7B, simulated positions)

Operator actions (``--actor operator:<name>``; every one is an audited, immutable record):

* ``submit --reservation-id ID --type T --quantity Q [--limit P] [--stop P]
  [--trail-amount A | --trail-percent P] [--expires-at ISO] --key K``: an ENTRY order
  executing an approved 6C reservation (the same authorization as the coordinator).
* ``exit --parent-id ID --type T --quantity Q ... --key K``: an EXIT order reducing the
  position an ENTRY order filled.
* ``cancel --id ID --key K --reason TEXT``: request cancellation (the reservation is
  released only when the paper broker's confirmation is applied).
* ``test-fill --id ID --quantity Q --price P [--commission C] [--fees F] --ref REF``:
  a CONTROLLED TEST execution event, only on CONTROLLED_TEST accounts.
* ``close --position-id ID --key K --reason TEXT``: ask the position monitor to exit a
  simulated position (one market exit at its next pass).
* ``protect --position-id ID [--stop P] [--trail-amount A | --trail-percent P] --reason
  TEXT``: tighten a position's protection (a looser stop is refused).

Jobs: ``dispatch [--limit N]`` (deliver outbox events), ``expire`` (expire due orders),
``recover`` (paper broker start + catch-up after a restart), ``reconcile``, ``broker
start|pause|stop``, ``simulate`` (one simulator pass: simulated fills from stored market
data), ``monitor`` (one position-monitor pass).

Exit status 0 on success, 1 when refused (or a submission is refused, or reconciliation
finds a discrepancy), 2 on bad configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from app.accounting.config import get_accounting_settings
from app.accounting.money import MoneyError, to_decimal
from app.core.clocks import SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging
from app.database.models import PortfolioAccount
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.execution import position_queries, queries
from app.execution.enums import (
    ExecutionDecision,
    OrderPurpose,
    OrderStatus,
    OrderType,
    PositionStatus,
)
from app.execution.models import ControlledFill, OrderRequest
from app.execution.runtime import ExecutionContext, build_execution_context
from app.portfolio.queries import resolve_account

GROUPS = sorted(queries.GROUPS)


def _decimal(value: str) -> Decimal:
    try:
        return to_decimal(value)
    except MoneyError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _order_options(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--type", type=OrderType, choices=list(OrderType), required=True)
    cmd.add_argument("--quantity", type=_decimal, required=True)
    cmd.add_argument("--limit", type=_decimal)
    cmd.add_argument("--stop", type=_decimal)
    cmd.add_argument("--trail-amount", type=_decimal)
    cmd.add_argument("--trail-percent", type=_decimal)
    cmd.add_argument("--expires-at", type=_timestamp)
    cmd.add_argument("--key", required=True, help="idempotency key (8-160 characters)")
    cmd.add_argument("--actor", required=True, help="operator:<name>")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.execution.cli")
    parser.add_argument("--account", help="portfolio account name (default: the primary one)")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("orders")
    cmd.add_argument("action", choices=["list", "show", "events", "requests"])
    cmd.add_argument("--id")
    cmd.add_argument("--group", choices=GROUPS)
    cmd.add_argument("--status", type=OrderStatus, choices=list(OrderStatus))
    cmd.add_argument("--limit", type=int, default=50)
    cmd = sub.add_parser("submit")
    cmd.add_argument("--reservation-id", required=True)
    _order_options(cmd)
    cmd = sub.add_parser("exit")
    cmd.add_argument("--parent-id", required=True)
    _order_options(cmd)
    cmd = sub.add_parser("cancel")
    cmd.add_argument("--id", required=True)
    cmd.add_argument("--key", required=True)
    cmd.add_argument("--actor", required=True)
    cmd.add_argument("--reason", required=True)
    cmd = sub.add_parser("test-fill", help="CONTROLLED TEST execution event")
    cmd.add_argument("--id", required=True)
    cmd.add_argument("--quantity", type=_decimal, required=True)
    cmd.add_argument("--price", type=_decimal, required=True)
    cmd.add_argument("--commission", type=_decimal, default=Decimal(0))
    cmd.add_argument("--fees", type=_decimal, default=Decimal(0))
    cmd.add_argument("--ref", required=True, help="idempotency key of the test event")
    cmd.add_argument("--actor", required=True)
    cmd = sub.add_parser("dispatch")
    cmd.add_argument("--limit", type=int, default=500)
    sub.add_parser("expire")
    sub.add_parser("recover")
    sub.add_parser("reconcile")
    cmd = sub.add_parser("broker")
    cmd.add_argument("action", choices=["status", "start", "pause", "stop"])
    cmd.add_argument("--reason", default="operator request")
    cmd = sub.add_parser("positions")
    cmd.add_argument("action", choices=["list", "show", "summary"])
    cmd.add_argument("--id")
    cmd.add_argument("--status", choices=["open", "closed"])
    cmd.add_argument("--limit", type=int, default=100)
    sub.add_parser("analytics")
    sub.add_parser("models")
    sub.add_parser("simulate")
    sub.add_parser("monitor")
    cmd = sub.add_parser("close")
    cmd.add_argument("--position-id", required=True)
    cmd.add_argument("--key", required=True)
    cmd.add_argument("--actor", required=True)
    cmd.add_argument("--reason", required=True)
    cmd = sub.add_parser("protect")
    cmd.add_argument("--position-id", required=True)
    cmd.add_argument("--stop", type=_decimal)
    trail = cmd.add_mutually_exclusive_group()
    trail.add_argument("--trail-amount", type=_decimal)
    trail.add_argument("--trail-percent", type=_decimal)
    cmd.add_argument("--actor", required=True)
    cmd.add_argument("--reason", required=True)
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _account(ctx: ExecutionContext, name: str | None) -> PortfolioAccount:
    with session_scope(ctx.session_factory) as session:
        if name is None:
            return resolve_account(session, None)
        found = session.scalar(select(PortfolioAccount).where(PortfolioAccount.name == name))
        if found is None:
            raise AtlasError(f"no portfolio account named {name!r}")
        return found


def _request(
    account: PortfolioAccount, args: argparse.Namespace, purpose: OrderPurpose
) -> OrderRequest:
    return OrderRequest(
        account_id=account.id,
        purpose=purpose,
        reservation_id=uuid.UUID(args.reservation_id) if purpose is OrderPurpose.ENTRY else None,
        parent_order_id=uuid.UUID(args.parent_id) if purpose is OrderPurpose.EXIT else None,
        order_type=args.type,
        quantity=args.quantity,
        limit_price=args.limit,
        stop_price=args.stop,
        trail_amount=args.trail_amount,
        trail_percent=args.trail_percent,
        expires_at=args.expires_at,
    )


def _require_operator(actor: str) -> None:
    if not actor.startswith("operator:"):
        raise AtlasError("this command is an operator action: --actor operator:<name>")


def run(ctx: ExecutionContext, args: argparse.Namespace) -> int:
    service = ctx.service
    if args.command == "orders":
        if args.action == "show":
            if not args.id:
                raise AtlasError("orders show needs --id")
            with session_scope(ctx.session_factory) as session:
                _print(queries.order(session, uuid.UUID(args.id)))
            return 0
        account = _account(ctx, args.account)
        with session_scope(ctx.session_factory) as session:
            if args.action == "events":
                _print(queries.events(session, account.id, limit=args.limit))
            elif args.action == "requests":
                _print(queries.execution_requests(session, account.id, limit=args.limit))
            else:
                _print(
                    queries.orders(
                        session, account.id, limit=args.limit, status=args.status, group=args.group
                    )
                )
        return 0
    if args.command in ("submit", "exit"):
        _require_operator(args.actor)
        account = _account(ctx, args.account)
        purpose = OrderPurpose.ENTRY if args.command == "submit" else OrderPurpose.EXIT
        result = service.submit(
            _request(account, args, purpose), idempotency_key=args.key, requested_by=args.actor
        )
        _print({"simulated": True, **asdict(result)})
        return 0 if result.decision is ExecutionDecision.ORDER_CREATED else 1
    if args.command == "cancel":
        _require_operator(args.actor)
        _print(
            asdict(
                service.request_cancel(
                    uuid.UUID(args.id),
                    idempotency_key=args.key,
                    actor=args.actor,
                    reason=args.reason,
                )
            )
        )
        return 0
    if args.command == "test-fill":
        _require_operator(args.actor)
        filled = service.record_controlled_fill(
            ControlledFill(
                order_id=uuid.UUID(args.id),
                quantity=args.quantity,
                price=args.price,
                commission=args.commission,
                fees=args.fees,
                external_ref=args.ref,
            ),
            actor=args.actor,
        )
        _print({"controlled_test": True, **asdict(filled)})
        return 0
    if args.command == "dispatch":
        summary = service.dispatcher().dispatch(limit=args.limit)
        _print({"delivered": summary.delivered, "failed": summary.failed, "dead": summary.dead})
        return 0
    if args.command == "expire":
        _print(asdict(service.expire_due()))
        return 0
    if args.command == "recover":
        report = service.recover()
        _print(asdict(report))
        return 0 if not report.errors else 1
    if args.command == "reconcile":
        scope = None if args.account is None else _account(ctx, args.account).id
        outcome = ctx.reconciler.run(scope)
        _print({"passed": outcome.passed, **asdict(outcome)})
        return 0 if outcome.passed else 1
    if args.command == "positions":
        with session_scope(ctx.session_factory) as session:
            if args.action == "show":
                if not args.id:
                    raise AtlasError("positions show needs --id")
                _print(position_queries.position(session, uuid.UUID(args.id), ctx.clock.now()))
                return 0
            account = _account(ctx, args.account)
            if args.action == "summary":
                _print(position_queries.position_summary(session, account.id))
                return 0
            status = None if args.status is None else PositionStatus(args.status.upper())
            _print(
                position_queries.positions(
                    session, account.id, status=status, limit=args.limit, now=ctx.clock.now()
                )
            )
        return 0
    if args.command == "analytics":
        account = _account(ctx, args.account)
        with session_scope(ctx.session_factory) as session:
            _print(position_queries.analytics(session, account.id))
        return 0
    if args.command == "models":
        with session_scope(ctx.session_factory) as session:
            _print(position_queries.models(session))
        return 0
    if args.command == "simulate":
        _print(asdict(ctx.simulator.run_once()))
        return 0
    if args.command == "monitor":
        _print(asdict(ctx.monitor.run_once()))
        return 0
    if args.command == "close":
        _require_operator(args.actor)
        closing = ctx.monitor.request_close(
            uuid.UUID(args.position_id),
            idempotency_key=args.key,
            actor=args.actor,
            reason=args.reason,
        )
        _print(asdict(closing))
        return 0
    if args.command == "protect":
        _require_operator(args.actor)
        position = ctx.monitor.tighten(
            uuid.UUID(args.position_id),
            actor=args.actor,
            reason=args.reason,
            stop=args.stop,
            trail_amount=args.trail_amount,
            trail_percent=args.trail_percent,
        )
        _print(position_queries.position_row(position, ctx.clock.now()))
        return 0
    if args.command == "broker":
        with session_scope(ctx.session_factory) as session:
            now = ctx.clock.now()
            if args.action == "start":
                ctx.broker.start(session, now)
            elif args.action == "pause":
                ctx.broker.pause(session, now, args.reason)
            elif args.action == "stop":
                ctx.broker.shutdown(session, now)
            _print(queries.broker_status(session, ctx.broker))
        return 0
    raise AtlasError(f"unknown command {args.command}")  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = get_settings()
        get_accounting_settings()
    except ValidationError as exc:
        print(f"Invalid Atlas configuration:\n{describe_validation_error(exc)}", file=sys.stderr)
        return 2
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid Atlas configuration:\n{exc}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level, json_output=settings.log_json)
    engine = build_engine(settings)
    try:
        ctx = build_execution_context(build_session_factory(engine), SystemClock())
        return run(ctx, args)
    except (AtlasError, ValidationError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
