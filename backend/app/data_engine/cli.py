"""Market-data command line: ``python -m app.data_engine.cli <command>``.

Commands
  register-assets  Register symbols from the reference data file (idempotent).
  check-auth       Verify the provider credentials (market-data endpoint only).
  fetch            Create and run a historical bar ingestion job in the foreground.

Results are printed to stdout as JSON; logs go to stderr. Credentials are never
printed.
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
    get_market_data_settings,
    get_settings,
)
from app.core.exceptions import AtlasError
from app.core.logging import configure_logging
from app.data_engine.historical import HistoricalFetchRequest
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.runtime import MarketDataContext, build_market_data_context
from app.data_engine.timeframes import Timeframe
from app.database.sessions import build_engine, build_session_factory, session_scope


def _timestamp(text: str) -> datetime:
    """RFC 3339 or YYYY-MM-DD (UTC midnight). Naive timestamps are read as UTC."""
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _symbols(text: str | None, default: tuple[str, ...]) -> list[str]:
    symbols = [s.strip().upper() for s in text.split(",") if s.strip()] if text else list(default)
    if not symbols:
        raise SystemExit("no symbols given (use --symbols or MARKET_DATA_SYMBOLS)")
    return symbols


def _print(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _context() -> MarketDataContext:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        stream=sys.stderr,
        service=settings.service_name,
        environment=settings.environment.value,
    )
    engine = build_engine(settings)
    return build_market_data_context(build_session_factory(engine), SystemClock())


def cmd_register(args: argparse.Namespace) -> int:
    ctx = _context()
    symbols = _symbols(args.symbols, ctx.settings.market_data_symbols)
    reference = load_reference_assets()
    with session_scope(ctx.session_factory) as session:
        results = register_assets(session, symbols, ctx.provider.provider_id, reference)
    _print([vars(r) for r in results])
    return 0


def cmd_check_auth(_: argparse.Namespace) -> int:
    ctx = _context()
    result = asyncio.run(ctx.provider.check_authentication())
    _print(result.model_dump(mode="json"))
    return 0 if result.status.value == "VERIFIED" else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    ctx = _context()
    request = HistoricalFetchRequest(
        symbols=tuple(_symbols(args.symbols, ctx.settings.market_data_symbols)),
        timeframe=Timeframe.parse(args.timeframe),
        start=_timestamp(args.start),
        end=_timestamp(args.end),
        feed=args.feed,
    )
    job = ctx.service.create_job(request, requested_by="cli")
    result = asyncio.run(ctx.service.run_job(job.job_id))
    _print(result.model_dump(mode="json"))
    return 0 if result.status.value == "COMPLETED" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.data_engine.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    register = commands.add_parser("register-assets", help="register symbols from reference data")
    register.add_argument("--symbols", help="comma-separated (default: MARKET_DATA_SYMBOLS)")
    register.set_defaults(handler=cmd_register)

    check = commands.add_parser("check-auth", help="verify provider credentials")
    check.set_defaults(handler=cmd_check_auth)

    fetch = commands.add_parser("fetch", help="retrieve and store historical bars")
    fetch.add_argument("--symbols", help="comma-separated (default: MARKET_DATA_SYMBOLS)")
    fetch.add_argument("--timeframe", required=True, help="1Min, 5Min, 15Min, 1Hour, 1Day")
    fetch.add_argument("--start", required=True, help="RFC 3339 or YYYY-MM-DD (UTC)")
    fetch.add_argument("--end", required=True, help="RFC 3339 or YYYY-MM-DD (UTC), inclusive")
    fetch.add_argument("--feed", help="must equal the configured feed if given")
    fetch.set_defaults(handler=cmd_fetch)

    args = parser.parse_args(argv)
    try:
        get_market_data_settings()
        return int(args.handler(args))
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid input"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError, AtlasError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
