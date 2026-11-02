"""Asset discovery command line: ``python -m app.asset_discovery.cli <command>``.

Commands
  discover   Read the provider directory (FULL or INCREMENTAL) and, unless --no-pipeline,
             run qualification, scoring and the membership updates after it.
  qualify    Re-evaluate the pool (no directory download); --as-of evaluates a past time
             from the data Atlas had then, without changing membership.
  refresh    Re-read reference data for the tracked universe or given asset IDs.
  status     Print discovery counts and the newest job of each kind.
  members    Reconstruct a layer as Atlas knew it at a given time (point in time).

Jobs run in the foreground. Results are printed to stdout as JSON; logs go to stderr.
Exit code 0 only when every job run finished COMPLETED.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from app.asset_discovery import membership, queries
from app.asset_discovery.enums import (
    DiscoveryJobKind,
    DiscoveryJobStatus,
    JobTrigger,
    UniverseLayer,
)
from app.asset_discovery.runtime import build_asset_discovery_context
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
from app.database.models import Asset, DiscoveryJobRecord
from app.database.sessions import build_engine, build_session_factory, session_scope


def _timestamp(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _job(job: DiscoveryJobRecord) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "kind": job.kind.value,
        "status": job.status.value,
        "params": job.params,
        "attempt": job.attempt,
        "created_at": job.created_at.isoformat(),
        "completed_at": None if job.completed_at is None else job.completed_at.isoformat(),
        "counters": job.counters,
        "summary": job.summary,
        "error": job.error,
    }


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.asset_discovery.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("--mode", choices=["FULL", "INCREMENTAL"], default="FULL")
    discover.add_argument("--no-pipeline", action="store_true")
    qualify = sub.add_parser("qualify")
    qualify.add_argument("--no-history", action="store_true", help="skip history preparation")
    qualify.add_argument("--no-pipeline", action="store_true", help="evaluate only")
    qualify.add_argument("--as-of", type=_timestamp, help="historical evaluation time (UTC)")
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--asset-ids", default="", help="comma-separated Atlas asset IDs")
    refresh.add_argument("--max", type=int)
    sub.add_parser("status")
    members = sub.add_parser("members")
    members.add_argument("--layer", choices=[layer.value for layer in UniverseLayer], required=True)
    members.add_argument("--at", type=_timestamp, required=True)
    members.add_argument("--known-at", type=_timestamp)
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
        ctx = build_asset_discovery_context(build_market_data_context(sessions, clock), clock)
        provider = ctx.provider.provider_id
        if args.command == "status":
            with session_scope(sessions) as session:
                data = queries.summary(session, provider, clock.now())
                data["last_jobs"] = {k: v and _job(v) for k, v in data["last_jobs"].items()}
                success = data["last_successful_discovery"]
                data["last_successful_discovery"] = success and _job(success)
            print(json.dumps(data, indent=2, default=str))
            return 0
        if args.command == "members":
            with session_scope(sessions) as session:
                found = membership.members_at(
                    session, UniverseLayer(args.layer), args.at, known_at=args.known_at
                )
                symbols = (
                    dict(
                        session.execute(
                            select(Asset.id, Asset.symbol).where(Asset.id.in_(list(found)))
                        ).all()
                    )
                    if found
                    else {}
                )
            print(
                json.dumps(
                    {
                        "layer": args.layer,
                        "at": args.at.isoformat(),
                        "known_at": (args.known_at or args.at).isoformat(),
                        "count": len(found),
                        "members": sorted(
                            (
                                {
                                    "asset_id": str(a),
                                    "symbol": symbols.get(a),
                                    "since": s.since.isoformat(),
                                }
                                for a, s in found.items()
                            ),
                            key=lambda m: m["symbol"] or "",
                        ),
                    },
                    indent=2,
                )
            )
            return 0

        if args.command == "discover":
            kind = DiscoveryJobKind.DIRECTORY_DISCOVERY
            params: dict[str, Any] = {"mode": args.mode, "pipeline": not args.no_pipeline}
        elif args.command == "qualify":
            kind = DiscoveryJobKind.QUALIFICATION
            params = {"prepare_history": not args.no_history}
            if args.as_of is not None:
                params["as_of"] = args.as_of.isoformat()  # historical: never chained
            elif not args.no_pipeline:
                params |= {"pipeline": True, "pipeline_step": 0}
        else:
            kind = DiscoveryJobKind.METADATA_REFRESH
            params = {}
            if args.asset_ids:
                params["asset_ids"] = [str(uuid.UUID(a)) for a in args.asset_ids.split(",")]
            if args.max:
                params["max_assets"] = args.max
        created = ctx.service.request(
            kind, trigger=JobTrigger.CLI, requested_by="cli", params=params
        )
        if not created.created:
            print(
                json.dumps(
                    {"note": "an equivalent job is already active", "job": _job(created.job)},
                    indent=2,
                    default=str,
                )
            )
            return 1
        ctx.service.recover()
        asyncio.run(ctx.service.run_due(job_id=created.job.id))
        with session_scope(sessions) as session:
            first = session.get(DiscoveryJobRecord, created.job.id)
            chain = [] if first is None else [first]
            while chain:
                child = session.scalars(
                    select(DiscoveryJobRecord).where(
                        DiscoveryJobRecord.parent_job_id == chain[-1].id
                    )
                ).first()
                if child is None:
                    break
                chain.append(child)
            result = [_job(j) for j in chain]
        print(json.dumps(result, indent=2, default=str))
        ok = all(j["status"] == DiscoveryJobStatus.COMPLETED.value for j in result)
        return 0 if ok else 1
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
