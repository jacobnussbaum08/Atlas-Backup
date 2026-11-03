"""Strategy Laboratory command line (Phase 4A): ``python -m app.strategies.cli``.

* ``sync``: register feature definitions, strategies and versions.
* ``list``: the catalog with current versions and status.
* ``features --strategy ID --symbol SYM [--timeframe 1Day] [--at ISO] [--basis ...]``:
  compute (without storing) the point-in-time features a strategy would receive. A
  read-only check of the feature pipeline on stored data.
* ``evaluate --strategy ID --symbol SYM --at ISO [--timeframe] [--basis]``: historical
  evaluation of an implemented strategy at a past decision time (stored, labelled
  HISTORICAL). Phase 4A strategies have no logic and are refused.
* ``expire``: record expiry of proposals whose expiry passed.

Proposals only: nothing here sizes, approves or submits a trade.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
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
from app.database.models import Asset, AssetStatus
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies import catalog
from app.strategies.base import StrategyDefinitionError
from app.strategies.engine import EvaluationRequest
from app.strategies.enums import EvaluationMode, TriggerKind
from app.strategies.inputs import compute_features, knowledge_time
from app.strategies.runtime import build_strategy_context


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _basis(value: str) -> KnowledgeBasis:
    return KnowledgeBasis(value.strip().upper())


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.strategies.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync")
    sub.add_parser("list")
    sub.add_parser("expire")
    for name in ("features", "evaluate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--strategy", required=True)
        cmd.add_argument("--symbol", required=True)
        cmd.add_argument("--timeframe", default="1Day")
        cmd.add_argument("--at", type=_timestamp, required=name == "evaluate")
        cmd.add_argument("--basis", type=_basis, default=KnowledgeBasis.RECORDED)
    args = parser.parse_args(argv)

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
        ctx = build_strategy_context(
            sessions, clock, provider=market.provider.provider_id, feed=market.provider.feed
        )
        out: Any
        if args.command == "sync":
            with session_scope(sessions) as session:
                report = catalog.sync_catalog(session, ctx.registry)
            out = report.__dict__
        elif args.command == "list":
            rows = []
            with session_scope(sessions) as session:
                for strategy in ctx.registry:
                    version = catalog.latest_version(session, strategy.strategy_id)
                    rows.append(
                        {
                            "strategy": strategy.strategy_id,
                            "implemented": strategy.implemented,
                            "enabled": catalog.is_enabled(session, strategy.strategy_id),
                            "version": None if version is None else version.version,
                            "logic_version": strategy.logic_version,
                        }
                    )
            out = rows
        elif args.command == "expire":
            out = {"expired": ctx.engine.expire_due()}
        else:
            strategy = ctx.registry.get(args.strategy)
            with session_scope(sessions) as session:
                asset = session.scalar(
                    select(Asset).where(
                        Asset.symbol == args.symbol.upper(), Asset.status == AssetStatus.ACTIVE
                    )
                )
                if asset is None:
                    raise StrategyDefinitionError(f"no active asset {args.symbol}")
                version = catalog.latest_version(session, strategy.strategy_id)
                if version is None:
                    raise StrategyDefinitionError("not registered; run sync first")
                stored = catalog.configuration(session, version).parameters
                params = strategy.parse_parameters(stored)
                asset_id, version_id = asset.id, version.id
            now = clock.now()
            cutoff = args.at or now
            if args.command == "features":
                resolved = ctx.registry.features.resolve_all(
                    strategy.required_features(params, args.timeframe)
                )
                with session_scope(sessions) as session:
                    inputs = compute_features(
                        session,
                        resolved,
                        asset_id=asset_id,
                        timeframe=args.timeframe,
                        data_cutoff=cutoff,
                        known_at=knowledge_time(args.basis, cutoff, now),
                        basis=args.basis,
                        series=ctx.engine.series,
                    )
                out = {
                    "strategy": strategy.strategy_id,
                    "symbol": args.symbol.upper(),
                    "timeframe": args.timeframe,
                    "data_cutoff": cutoff.isoformat(),
                    "basis": args.basis.value,
                    "last_period_event_at": None
                    if inputs.last_period_event_at is None
                    else inputs.last_period_event_at.isoformat(),
                    "features": inputs.snapshot_json(),
                }
            else:
                if not strategy.implemented:
                    raise StrategyDefinitionError(
                        f"{strategy.strategy_id} has no evaluation logic yet (Phase 4B)"
                    )
                result = ctx.engine.evaluate(
                    EvaluationRequest(
                        strategy_id=strategy.strategy_id,
                        strategy_version_id=version_id,
                        asset_id=asset_id,
                        timeframe=args.timeframe,
                        trigger=TriggerKind.MANUAL,
                        trigger_ref=f"cli:{cutoff.isoformat()}",
                        mode=EvaluationMode.HISTORICAL,
                        basis=args.basis,
                        data_cutoff=cutoff,
                    )
                )
                out = {
                    "outcome": result.outcome,
                    "evaluation_id": result.evaluation_id,
                    "status": result.status,
                    "proposals": result.proposal_ids,
                    "error": result.error,
                }
        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid configuration"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except AtlasError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
    except StrategyDefinitionError as exc:
        print(f"strategy_error: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
