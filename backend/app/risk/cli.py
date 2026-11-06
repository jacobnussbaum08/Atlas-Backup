"""Risk engine command line (Phase 6B): ``python -m app.risk.cli``.

Operator actions (``--actor operator:<name>`` and ``--reason`` are required; every one is
an immutable, audited record):

* ``init [--account NAME]``: create risk configuration version 1 with the documented
  starting values (not optimal values). Idempotent.
* ``set --hard KEY=VALUE ... --pref KEY=VALUE ...``: write the next configuration version.
* ``classify --symbol S --sector SECTOR [--cluster CLUSTER] [--valid-from ISO]
  --provenance TEXT``: tag an asset's sector / correlation cluster.
* ``kill-switch engage|release``; ``reset --breaker TYPE``: breaker control. A reset is
  refused while its condition is still breached.

Assessment and monitoring:

* ``assess --symbol S --direction LONG|SHORT --entry P --stop P [--quantity Q]
  [--request-key K]`` or ``assess --proposal-id ID`` / ``--decision-id ID``: record a
  deterministic risk assessment. A PASS_FOR_RESERVATION is not an execution approval.
* ``monitor``: evaluate and record the risk state; trips breakers as needed.
* ``replay --assessment ID``: re-run a stored assessment from its stored inputs.
* ``show``: the risk state as JSON (live figures are not recorded).

Exit status 0 on success, 1 when refused (or an assessment does not pass with ``assess
--strict``), 2 on bad configuration. Nothing here can submit, reserve or execute an order.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
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
from app.database.models import Asset, AssetStatus, PortfolioAccount, RiskEventRecord
from app.database.models.risk import RiskAssetClassificationRecord
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.portfolio.queries import resolve_account
from app.risk import governance, proposals, queries
from app.risk.enums import (
    AssessmentResult,
    BreakerType,
    ClassificationSource,
    ProposalSource,
    RiskEventType,
    Severity,
    TradeDirection,
)
from app.risk.models import Proposal
from app.risk.runtime import RiskContext, build_risk_context


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _decimal(value: str) -> Decimal:
    try:
        return to_decimal(value)
    except MoneyError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _pair(value: str) -> tuple[str, str]:
    key, sep, raw = value.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError("expected KEY=VALUE")
    return key.strip(), raw.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.risk.cli")
    parser.add_argument("--account", help="portfolio account name (default: the primary one)")
    sub = parser.add_subparsers(dest="command", required=True)

    def operator(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--actor", required=True, help="operator:<name>")
        cmd.add_argument("--reason", required=True)

    operator(sub.add_parser("init"))
    cmd = sub.add_parser("set")
    operator(cmd)
    cmd.add_argument("--hard", type=_pair, action="append", default=[], metavar="KEY=VALUE")
    cmd.add_argument("--pref", type=_pair, action="append", default=[], metavar="KEY=VALUE")
    cmd = sub.add_parser("classify")
    cmd.add_argument("--actor", required=True, help="operator:<name>")
    cmd.add_argument("--symbol")
    cmd.add_argument("--asset-id")
    cmd.add_argument("--sector")
    cmd.add_argument("--cluster")
    cmd.add_argument("--valid-from", type=_timestamp)
    cmd.add_argument("--provenance", required=True)
    cmd = sub.add_parser("kill-switch")
    cmd.add_argument("action", choices=["engage", "release"])
    operator(cmd)
    cmd = sub.add_parser("reset")
    cmd.add_argument("--breaker", type=BreakerType, choices=list(BreakerType), required=True)
    operator(cmd)
    cmd = sub.add_parser("assess")
    cmd.add_argument("--requested-by", default="operator:cli")
    cmd.add_argument("--symbol")
    cmd.add_argument("--asset-id")
    cmd.add_argument("--direction", type=TradeDirection, choices=list(TradeDirection))
    cmd.add_argument("--entry", type=_decimal)
    cmd.add_argument("--stop", type=_decimal)
    cmd.add_argument("--target", type=_decimal)
    cmd.add_argument("--quantity", type=_decimal)
    cmd.add_argument("--holding-days", type=int)
    cmd.add_argument("--proposal-id")
    cmd.add_argument("--decision-id")
    cmd.add_argument("--request-key")
    cmd.add_argument("--strict", action="store_true", help="exit 1 unless it passes")
    sub.add_parser("monitor")
    cmd = sub.add_parser("replay")
    cmd.add_argument("--assessment", required=True)
    sub.add_parser("show")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _account(ctx: RiskContext, name: str | None) -> PortfolioAccount:
    with session_scope(ctx.session_factory) as session:
        if name is None:
            return resolve_account(session, None)
        found = session.scalar(select(PortfolioAccount).where(PortfolioAccount.name == name))
        if found is None:
            raise AtlasError(f"no portfolio account named {name!r}")
        return found


def _asset(ctx: RiskContext, symbol: str | None, asset_id: str | None) -> Asset:
    with session_scope(ctx.session_factory) as session:
        if asset_id:
            found = session.get(Asset, uuid.UUID(asset_id))
        else:
            matches = session.scalars(
                select(Asset).where(
                    Asset.symbol == (symbol or "").upper(), Asset.status == AssetStatus.ACTIVE
                )
            ).all()
            if len(matches) > 1:
                raise AtlasError(f"{symbol} is ambiguous; pass --asset-id")
            found = matches[0] if matches else None
        if found is None:
            raise AtlasError("asset not found")
        return found


def classify(
    ctx: RiskContext,
    *,
    asset_id: uuid.UUID,
    sector: str | None,
    cluster: str | None,
    valid_from: datetime,
    provenance: str,
    actor: str,
    source: ClassificationSource = ClassificationSource.OPERATOR,
) -> int:
    if source is ClassificationSource.OPERATOR:
        governance.require_operator(actor)
    if sector is None and cluster is None:
        raise AtlasError("give a --sector, a --cluster or both")
    with session_scope(ctx.session_factory) as session:
        row = RiskAssetClassificationRecord(
            asset_id=asset_id,
            sector=None if sector is None else sector.strip().upper(),
            cluster=None if cluster is None else cluster.strip().upper(),
            valid_from=valid_from,
            source=source,
            provenance=provenance,
            created_by=actor,
        )
        session.add(row)
        session.flush()
        session.add(
            RiskEventRecord(
                portfolio_account_id=None,
                event_type=RiskEventType.CLASSIFICATION_RECORDED,
                severity=Severity.INFO,
                occurred_at=ctx.clock.now(),
                actor=actor,
                subject_kind="asset",
                subject_id=str(asset_id),
                summary=f"sector {row.sector}, cluster {row.cluster} from {valid_from.isoformat()}",
                details={"provenance": provenance, "source": source.value},
            )
        )
        return row.id


def run(ctx: RiskContext, args: argparse.Namespace) -> int:
    now = ctx.clock.now()
    account = _account(ctx, args.account)
    if args.command == "init":
        with session_scope(ctx.session_factory) as session:
            result = governance.initialize(
                session, account.id, actor=args.actor, reason=args.reason, at=now
            )
        _print(
            {
                "account": account.name,
                "version": result.version,
                "created": result.created,
                "note": "starting values, not optimal or recommended settings",
            }
        )
        return 0
    if args.command == "set":
        with session_scope(ctx.session_factory) as session:
            result = governance.change(
                session,
                account.id,
                actor=args.actor,
                reason=args.reason,
                at=now,
                hard_limit_updates=_values(args.hard),
                preference_updates=_values(args.pref),
            )
        _print({"version": result.version, "changes": result.changes})
        return 0
    if args.command == "classify":
        asset = _asset(ctx, args.symbol, args.asset_id)
        row_id = classify(
            ctx,
            asset_id=asset.id,
            sector=args.sector,
            cluster=args.cluster,
            valid_from=args.valid_from or now,
            provenance=args.provenance,
            actor=args.actor,
        )
        _print({"classification_id": row_id, "symbol": asset.symbol})
        return 0
    if args.command == "kill-switch":
        if args.action == "engage":
            engaged = ctx.service.engage_kill_switch(
                account.id, actor=args.actor, reason=args.reason
            )
            _print({"kill_switch": "TRIPPED", "already_engaged": not engaged})
            return 0
        ctx.service.reset_breaker(
            account.id, BreakerType.KILL_SWITCH, actor=args.actor, reason=args.reason
        )
        _print({"kill_switch": "CLEAR"})
        return 0
    if args.command == "reset":
        ctx.service.reset_breaker(account.id, args.breaker, actor=args.actor, reason=args.reason)
        _print({"breaker": args.breaker.value, "status": "CLEAR"})
        return 0
    if args.command == "assess":
        proposal = _proposal(ctx, args)
        stored = ctx.service.assess(
            account.id, proposal, requested_by=args.requested_by, request_key=args.request_key
        )
        with session_scope(ctx.session_factory) as session:
            _print(queries.assessment(session, stored.assessment_id) | {"created": stored.created})
        if args.strict and stored.result is not AssessmentResult.PASS_FOR_RESERVATION:
            return 1
        return 0
    if args.command == "monitor":
        state = ctx.service.monitor(account.id)
        _print(
            {
                "snapshot_id": str(state.snapshot_id),
                "mark_status": state.view.mark_status.value,
                "equity": str(state.view.equity),
                "transitions": [f"{t.breaker.value} {t.event.value}" for t in state.transitions],
                "active_breakers": [b.value for b in state.view.active_breakers],
            }
        )
        return 0
    if args.command == "replay":
        same, outcome = ctx.service.replay(uuid.UUID(args.assessment))
        _print({"reproduced": same, "result": outcome.result.value})
        return 0 if same else 1
    if args.command == "show":
        state, _ = ctx.service.preview(account.id)
        with session_scope(ctx.session_factory) as session:
            _print(queries.overview(session, account, state))
        return 0
    raise AssertionError(args.command)  # pragma: no cover


def _values(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, raw in pairs:
        if raw.lower() in ("true", "false"):
            out[key] = raw.lower() == "true"
        elif raw.startswith("{"):
            out[key] = json.loads(raw)
        else:
            out[key] = raw
    return out


def _proposal(ctx: RiskContext, args: argparse.Namespace) -> Proposal:
    with session_scope(ctx.session_factory) as session:
        if args.decision_id:
            return proposals.from_ai_decision(
                session, uuid.UUID(args.decision_id), requested_quantity=args.quantity
            )
        if args.proposal_id:
            return proposals.from_strategy_proposal(
                session, uuid.UUID(args.proposal_id), requested_quantity=args.quantity
            )
    if args.direction is None or args.entry is None:
        raise AtlasError("give --direction and --entry (and --stop), or a proposal/decision id")
    asset = _asset(ctx, args.symbol, args.asset_id)
    return Proposal(
        source=ProposalSource.OPERATOR,
        asset_id=asset.id,
        direction=args.direction,
        entry=args.entry,
        stop=args.stop,
        target=args.target,
        requested_quantity=args.quantity,
        expected_holding_days=args.holding_days,
    )


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
        ctx = build_risk_context(build_session_factory(engine), SystemClock())
        return run(ctx, args)
    except (AtlasError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
