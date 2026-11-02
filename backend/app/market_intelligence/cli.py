"""Market intelligence command line: ``python -m app.market_intelligence.cli <command>``.

Commands
  live         Compute market intelligence now (RECORDED basis), after re-retrieving the
               history window of the benchmark and the qualified universe.
  reconstruct  Recompute the last --sessions past decision times (RECONSTRUCTED basis).
  market       Print the newest recorded broad-market classifications (--at: as known then).

Runs execute in the foreground. Results are printed to stdout as JSON; logs go to stderr.
Exit code 0 only when the run finished COMPLETED.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
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
from app.data_engine.runtime import build_market_data_context
from app.database.models import IntelligenceRunRecord
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.market_intelligence import queries
from app.market_intelligence.enums import RunKind, RunStatus
from app.market_intelligence.runtime import build_intelligence_context


def _timestamp(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _run_json(run: IntelligenceRunRecord) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "kind": run.kind.value,
        "status": run.status.value,
        "basis": run.basis.value,
        "as_of_start": run.as_of_start.isoformat(),
        "as_of_end": run.as_of_end.isoformat(),
        "summary": run.summary,
        "error": run.error,
    }


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.market_intelligence.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("live")
    reconstruct = sub.add_parser("reconstruct")
    reconstruct.add_argument("--sessions", type=int, default=60)
    market = sub.add_parser("market")
    market.add_argument("--at", type=_timestamp)
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
        ctx = build_intelligence_context(build_market_data_context(sessions, clock), clock)
        if args.command == "market":
            at = args.at or clock.now()
            with session_scope(sessions) as session:
                run = queries.latest_live_run(
                    session, ctx.provider.provider_id, ctx.provider.feed, at
                )
                rows = [] if run is None else queries.market_rows(session, run.id)
                data = {
                    "at": at.isoformat(),
                    "run": None if run is None else _run_json(run),
                    "classifications": [
                        {
                            "timeframe": r.timeframe,
                            "dimension": r.dimension.value,
                            "label": r.label,
                            "confidence": str(r.confidence),
                            "probabilities": r.probabilities,
                            "quality": r.quality.value,
                        }
                        for r in rows
                    ],
                }
            print(json.dumps(data, indent=2, default=str))
            return 0
        if args.command == "live":
            run = asyncio.run(ctx.service.run(RunKind.LIVE, requested_by="cli"))
        else:
            run = asyncio.run(
                ctx.service.run(
                    RunKind.RECONSTRUCTION,
                    requested_by="cli",
                    params={"sessions": args.sessions},
                )
            )
        print(json.dumps(_run_json(run), indent=2, default=str))
        return 0 if run.status is RunStatus.COMPLETED else 1
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
