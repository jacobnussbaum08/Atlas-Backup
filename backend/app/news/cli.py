"""News command line: ``python -m app.news.cli <command>``.

Commands
  backfill  Retrieve historical news for --start/--end (or the last --days) and optional
            --symbols (comma-separated provider symbols; default: all news).
  auth      Verify the configured credentials against the news endpoint.
  reclassify  Classify stored revisions that have no event under the current classifier
            version (after a rule change). Earlier events are kept; the new ones are
            available from the time they are computed.

Retrievals run in the foreground as durable ``news_ingestion_jobs``; the job is printed to
stdout as JSON and logs go to stderr. Exit code 0 only when the job COMPLETED. Every article
stored this way is labelled as retrieved later (``HISTORICAL_REST``); its availability is
the retrieval time, never its publication time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
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
from app.database.models import NewsIngestionJobRecord
from app.database.sessions import build_engine, build_session_factory
from app.news.enums import NewsJobKind, NewsJobStatus
from app.news.runtime import build_news_context


def _timestamp(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def job_json(job: NewsIngestionJobRecord) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "kind": job.kind.value,
        "status": job.status.value,
        "symbols": job.symbols,
        "range_start": job.range_start.isoformat(),
        "range_end": job.range_end.isoformat(),
        "pages_retrieved": job.pages_retrieved,
        "articles_received": job.articles_received,
        "articles_new": job.articles_new,
        "revisions_new": job.revisions_new,
        "duplicates": job.duplicates,
        "malformed": job.malformed,
        "error": job.error,
    }


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.news.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--symbols", default="", help="comma-separated; empty = all news")
    backfill.add_argument("--start", type=_timestamp)
    backfill.add_argument("--end", type=_timestamp)
    backfill.add_argument("--days", type=float, help="instead of --start: the last N days")
    sub.add_parser("auth")
    reclassify = sub.add_parser("reclassify")
    reclassify.add_argument("--limit", type=int, help="at most this many revisions")
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
        ctx = build_news_context(build_session_factory(engine), clock)
        if args.command == "auth":
            result = asyncio.run(ctx.provider.check_authentication())
            print(json.dumps(result.model_dump(mode="json"), indent=2))
            return 0 if result.status == "VERIFIED" else 1
        if args.command == "reclassify":
            created = ctx.service.reclassify_pending(limit=args.limit)
            print(json.dumps({"events_created": created}))
            return 0
        end = args.end or clock.now()
        if args.start is not None:
            start = args.start
        elif args.days is not None:
            start = end - timedelta(days=args.days)
        else:
            parser.error("backfill needs --start or --days")
        symbols = [s for s in args.symbols.split(",") if s.strip()]
        job = ctx.service.create_job(
            kind=NewsJobKind.HISTORICAL_BACKFILL,
            start=start,
            end=end,
            symbols=symbols,
            requested_by="cli",
        )
        finished = asyncio.run(ctx.service.run_job(job.id))
        print(json.dumps(job_json(finished), indent=2))
        return 0 if finished.status is NewsJobStatus.COMPLETED else 1
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
        print(f"Error: {exc.message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
