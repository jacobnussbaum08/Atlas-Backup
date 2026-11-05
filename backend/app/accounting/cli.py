"""Accounting command line (Phase 6A): ``python -m app.accounting.cli``.

* ``init``: create the PRIMARY paper account and post its simulated opening funding
  (``ACCOUNTING_OPENING_FUNDING``, default 100,000.00 USD). Idempotent.
* ``create-test-account --name N [--funding AMOUNT]``: a CONTROLLED_TEST account, funded,
  for controlled test transactions. The primary account never receives those.
* ``fx-rate --base EUR --quote USD --rate R --observed-at ISO --source OPERATOR|TEST_FIXTURE
  --provenance TEXT``: record a timestamped rate (TEST_FIXTURE rates value test accounts only).
* ``instrument-spec --symbol S [--asset-id ID] (--increment Q [--min-quantity Q] |
  --from-reference) [--valid-from ISO]``: register a lot size.
* ``post --account NAME --template T --event-id ID --params JSON [--effective-at ISO]``:
  a controlled test transaction on a CONTROLLED_TEST account (templates: long_purchase,
  long_sale, short_open, short_cover, fee, financing_cost, accrual_settlement, fx_conversion).
* ``reverse --account NAME --entry ID --reason TEXT``: post a reversal (operator correction).
* ``checkpoint [--account NAME] [--as-of ISO]``: valuation checkpoint and snapshot, then a
  reconciliation run.
* ``reconcile [--account NAME]``: a reconciliation run.
* ``show [--account NAME]``: the portfolio overview as JSON.

Exit status 0 on success, 1 when refused or when reconciliation fails, 2 on bad
configuration. Nothing here can submit an order: there is no broker.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from app.accounting.config import get_accounting_settings
from app.accounting.enums import EntrySource, FxRateSource, InstrumentSpecSource
from app.accounting.money import MoneyError, to_decimal
from app.accounting.posting import PostingRequest
from app.accounting.reference import (
    ReferenceDataError,
    record_fx_rate,
    register_instrument_spec,
    spec_from_reference,
)
from app.accounting.runtime import AccountingContext, build_accounting_context
from app.accounting.templates import TEMPLATES
from app.core.clocks import SystemClock
from app.core.config import (
    InvalidSettingsError,
    UnknownSettingError,
    describe_validation_error,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging
from app.database.models import Asset, AssetStatus, PortfolioAccount
from app.database.models.portfolio_account import PortfolioAccountPurpose
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.portfolio import queries
from app.portfolio.accounts import open_and_fund

ACTOR = "operator:cli"
TEST_TEMPLATES = sorted(code for code, t in TEMPLATES.items() if t.controlled_test_only)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _decimal(value: str) -> Decimal:
    try:
        return to_decimal(value)
    except MoneyError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.accounting.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    cmd = sub.add_parser("create-test-account")
    cmd.add_argument("--name", required=True)
    cmd.add_argument("--funding", type=_decimal, default=None)
    cmd = sub.add_parser("fx-rate")
    cmd.add_argument("--base", required=True)
    cmd.add_argument("--quote", required=True)
    cmd.add_argument("--rate", type=_decimal, required=True)
    cmd.add_argument("--observed-at", type=_timestamp, required=True)
    cmd.add_argument(
        "--source",
        type=FxRateSource,
        choices=[FxRateSource.OPERATOR, FxRateSource.TEST_FIXTURE],
        required=True,
    )
    cmd.add_argument("--provenance", required=True)
    cmd = sub.add_parser("instrument-spec")
    cmd.add_argument("--symbol")
    cmd.add_argument("--asset-id")
    cmd.add_argument("--increment", type=_decimal)
    cmd.add_argument("--min-quantity", type=_decimal)
    cmd.add_argument("--from-reference", action="store_true")
    cmd.add_argument("--valid-from", type=_timestamp)
    cmd = sub.add_parser("post")
    cmd.add_argument("--account", required=True)
    cmd.add_argument("--template", required=True, choices=TEST_TEMPLATES)
    cmd.add_argument("--event-id", required=True)
    cmd.add_argument("--params", required=True, help="JSON object, or @path to a JSON file")
    cmd.add_argument("--effective-at", type=_timestamp)
    cmd = sub.add_parser("reverse")
    cmd.add_argument("--account", required=True)
    cmd.add_argument("--entry", required=True)
    cmd.add_argument("--reason", required=True)
    for name in ("checkpoint", "reconcile", "show"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--account")
        if name == "checkpoint":
            cmd.add_argument("--as-of", type=_timestamp)
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _account(ctx: AccountingContext, name: str | None) -> PortfolioAccount:
    with session_scope(ctx.session_factory) as session:
        if name is None:
            return queries.resolve_account(session, None)
        found = session.scalar(select(PortfolioAccount).where(PortfolioAccount.name == name))
        if found is None:
            raise AtlasError(f"no portfolio account named {name!r}")
        return found


def _asset(ctx: AccountingContext, symbol: str | None, asset_id: str | None) -> Asset:
    with session_scope(ctx.session_factory) as session:
        if asset_id:
            found = session.get(Asset, asset_id)
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


def run(ctx: AccountingContext, args: argparse.Namespace) -> int:
    now = ctx.clock.now()
    settings = ctx.settings
    if args.command == "init":
        with session_scope(ctx.session_factory) as session:
            funded = open_and_fund(
                session,
                ctx.posting,
                name=settings.primary_account_name,
                purpose=PortfolioAccountPurpose.PRIMARY,
                base_currency=settings.functional_currency,
                amount=settings.opening_funding,
                at=now,
                actor=ACTOR,
                description="Simulated paper-trading account (paper only; no broker).",
            )
            _print(
                {
                    "account_id": funded.account.id,
                    "account_created": funded.created,
                    "funding_entry": funded.funding.entry_id,
                    "funding_posted": funded.funding.created,
                    "opening_funding": f"{settings.opening_funding} {settings.functional_currency}",
                }
            )
        ctx.reconciliation.run(funded.account.id, actor=ACTOR, trigger="INIT")
        return 0
    if args.command == "create-test-account":
        with session_scope(ctx.session_factory) as session:
            funded = open_and_fund(
                session,
                ctx.posting,
                name=args.name,
                purpose=PortfolioAccountPurpose.CONTROLLED_TEST,
                base_currency=settings.functional_currency,
                amount=args.funding or settings.opening_funding,
                at=now,
                actor=ACTOR,
                description="Controlled test transactions only; not the paper account.",
            )
            _print({"account_id": funded.account.id, "created": funded.created})
        return 0
    if args.command == "fx-rate":
        with session_scope(ctx.session_factory) as session:
            row = record_fx_rate(
                session,
                base=args.base.upper(),
                quote=args.quote.upper(),
                rate=args.rate,
                observed_at=args.observed_at,
                source=args.source,
                provenance=args.provenance,
                recorded_at=now,
                created_by=ACTOR,
            )
            _print({"fx_rate_id": row.id})
        return 0
    if args.command == "instrument-spec":
        asset = _asset(ctx, args.symbol, args.asset_id)
        valid_from = args.valid_from or now
        with session_scope(ctx.session_factory) as session:
            if args.from_reference:
                derived = spec_from_reference(session, asset.id, valid_from)
                spec = register_instrument_spec(
                    session,
                    asset_id=asset.id,
                    quantity_increment=derived["quantity_increment"],  # type: ignore[arg-type]
                    min_quantity=derived["min_quantity"],  # type: ignore[arg-type]
                    price_increment=derived["price_increment"],  # type: ignore[arg-type]
                    valid_from=valid_from,
                    source=InstrumentSpecSource.PROVIDER_REFERENCE,
                    provenance=str(derived["provenance"]),
                    created_by=ACTOR,
                )
            else:
                if args.increment is None:
                    raise AtlasError("pass --increment or --from-reference")
                spec = register_instrument_spec(
                    session,
                    asset_id=asset.id,
                    quantity_increment=args.increment,
                    min_quantity=args.min_quantity
                    if args.min_quantity is not None
                    else args.increment,
                    valid_from=valid_from,
                    source=InstrumentSpecSource.OPERATOR,
                    provenance="operator registration (cli)",
                    created_by=ACTOR,
                )
            _print({"instrument_spec_id": spec.id, "symbol": asset.symbol})
        return 0
    if args.command in ("post", "reverse"):
        account = _account(ctx, args.account)
        test_account = account.purpose is PortfolioAccountPurpose.CONTROLLED_TEST
        if args.command == "post":
            if not test_account:
                raise AtlasError("controlled test transactions go to a CONTROLLED_TEST account")
            raw = args.params
            params = json.loads(Path(raw[1:]).read_text("utf-8") if raw.startswith("@") else raw)
            request = PostingRequest(
                account_id=account.id,
                template=args.template,
                params=params,
                event_id=args.event_id,
                effective_at=args.effective_at or now,
                source=EntrySource.CONTROLLED_TEST,
                actor=ACTOR,
                source_ref="cli controlled test",
            )
        else:
            request = PostingRequest(
                account_id=account.id,
                template="reversal",
                params={"entry_id": args.entry, "reason": args.reason},
                event_id=f"reversal:{args.entry}",
                effective_at=now,
                source=EntrySource.CONTROLLED_TEST if test_account else EntrySource.OPERATOR,
                actor=ACTOR,
            )
        result = ctx.posting.post(request)
        _print(
            {
                "entry_id": result.entry_id,
                "sequence": result.sequence,
                "entry_type": result.entry_type.value,
                "created": result.created,
            }
        )
        return 0
    if args.command == "checkpoint":
        account = _account(ctx, args.account)
        checkpoint = ctx.valuation.checkpoint(account.id, as_of=args.as_of, actor=ACTOR)
        summary = ctx.reconciliation.run(account.id, actor=ACTOR, trigger="CHECKPOINT")
        _print(
            {
                "snapshot_id": checkpoint.snapshot_id,
                "as_of": checkpoint.as_of,
                "valuation_status": checkpoint.valuation_status.value,
                "equity": checkpoint.equity,
                "issues": checkpoint.issues,
                "valuation_entry_id": checkpoint.valuation_entry_id,
                "reconciliation": summary.status.value,
                "checks": summary.checks_total,
                "failed": [f"{c.code} {c.scope}: {c.message}" for c in summary.failures],
            }
        )
        return 0 if not summary.failures else 1
    if args.command == "reconcile":
        account = _account(ctx, args.account)
        summary = ctx.reconciliation.run(account.id, actor=ACTOR, trigger="MANUAL")
        _print(
            {
                "run_id": summary.run_id,
                "status": summary.status.value,
                "checks": summary.checks_total,
                "entry_sequence": summary.entry_sequence,
                "failed": [f"{c.code} {c.scope}: {c.message}" for c in summary.failures],
            }
        )
        return 0 if not summary.failures else 1
    if args.command == "show":
        account = _account(ctx, args.account)
        with session_scope(ctx.session_factory) as session:
            _print(queries.overview(session, account).model_dump(mode="json"))
        return 0
    raise AssertionError(args.command)  # pragma: no cover


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
        ctx = build_accounting_context(build_session_factory(engine), SystemClock())
        return run(ctx, args)
    except (AtlasError, ReferenceDataError, json.JSONDecodeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
