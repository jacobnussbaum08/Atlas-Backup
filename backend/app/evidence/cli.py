"""Evidence program command line (Phase 11A): ``python -m app.evidence.cli``.

* ``criteria``: the default protocol criteria and their digest.
* ``register-protocol --rationale TEXT [--name N] [--criteria FILE.json] --actor operator:X``:
  fix a new protocol version (append-only; identical criteria are refused).
* ``protocols`` / ``protocol ID``.
* ``audit --symbols A,B,... --start D --end D [--benchmark SPY] [--selected-on D]``: audit
  the historical daily bars behind an evaluation and store the result.
* ``scorecard --protocol ID --run STRATEGY=RUN_ID ... [--portfolio-run ID] [--audit ID]
  [--account NAME] [--strategy S ...]``: build and store a readiness scorecard.
* ``scorecards`` / ``show ID`` / ``latest [--brief]``.

In the paper environment every write must name an active operator credential. Nothing here
submits an order, changes a risk limit, or enables, disables or promotes anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
from app.evidence import queries
from app.evidence import service as evidence
from app.evidence.data_audit import AuditScope
from app.evidence.protocol import DEFAULT_PROTOCOL_NAME, DEFAULT_RATIONALE, EvidenceCriteria

WRITES = {"register-protocol", "audit", "scorecard"}


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def _brief(card: dict[str, Any]) -> dict[str, Any]:
    result = card.get("result", {})
    return {
        "id": card["id"],
        "verdict": card["verdict"],
        "as_of": card["as_of"],
        "protocol": result.get("protocol"),
        "entries": card["entries"],
        "portfolio": {
            k: result.get("portfolio", {}).get(k)
            for k in ("classification", "oos_trades", "oos_source", "reproduction")
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.evidence.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("criteria")
    reg = sub.add_parser("register-protocol")
    reg.add_argument("--name", default=DEFAULT_PROTOCOL_NAME)
    reg.add_argument("--rationale", default=DEFAULT_RATIONALE)
    reg.add_argument("--criteria", type=Path, help="JSON criteria (default: the defaults)")
    sub.add_parser("protocols")
    sub.add_parser("protocol").add_argument("protocol_id", type=uuid.UUID)
    aud = sub.add_parser("audit")
    aud.add_argument("--symbols", required=True, help="comma-separated")
    aud.add_argument("--start", required=True, type=date.fromisoformat)
    aud.add_argument("--end", required=True, type=date.fromisoformat)
    aud.add_argument("--benchmark", default="SPY")
    aud.add_argument("--provider", default="alpaca")
    aud.add_argument("--feed", default="iex")
    aud.add_argument("--selected-on", type=date.fromisoformat)
    aud.add_argument("--brief", action="store_true")
    sc = sub.add_parser("scorecard")
    sc.add_argument("--protocol", required=True, type=uuid.UUID)
    sc.add_argument("--run", action="append", default=[], help="STRATEGY=RUN_ID (repeatable)")
    sc.add_argument("--portfolio-run", type=uuid.UUID)
    sc.add_argument("--audit", type=uuid.UUID)
    sc.add_argument("--account")
    sc.add_argument("--strategy", action="append", default=[], help="judge without a run")
    sc.add_argument("--brief", action="store_true")
    sub.add_parser("scorecards")
    sub.add_parser("show").add_argument("scorecard_id", type=uuid.UUID)
    sub.add_parser("latest").add_argument("--brief", action="store_true")
    for name in WRITES:
        sub.choices[name].add_argument("--actor", required=True, help="operator:<name>")
    return parser


def _runs(values: list[str]) -> dict[str, uuid.UUID]:
    out: dict[str, uuid.UUID] = {}
    for v in values:
        key, _, run = v.partition("=")
        if not key or not run:
            raise SystemExit(f"--run expects STRATEGY=RUN_ID, got {v!r}")
        out[key.strip()] = uuid.UUID(run.strip())
    return out


def _run(argv: list[str] | None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "criteria":
        c = EvidenceCriteria()
        _print({"criteria": c.as_json(), "criteria_sha256": c.sha256()})
        return 0
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
        with session_scope(sessions) as session:
            if args.command in WRITES:
                if not args.actor.startswith("operator:"):
                    raise evidence.EvidenceError("--actor must be operator:<name>")
                if settings.environment is Environment.PAPER and (
                    operators.active(session, args.actor.removeprefix("operator:")) is None
                ):
                    raise operators.OperatorCredentialError(
                        f"{args.actor} is not an active operator credential (paper environment)"
                    )
            if args.command == "register-protocol":
                criteria = (
                    EvidenceCriteria.model_validate_json(args.criteria.read_text("utf-8"))
                    if args.criteria
                    else EvidenceCriteria()
                )
                row = evidence.register_protocol(
                    session,
                    criteria,
                    name=args.name,
                    rationale=args.rationale,
                    actor=args.actor,
                    now=clock.now(),
                )
                _print(queries.protocol_view(row, full=True))
            elif args.command == "protocols":
                _print(queries.protocols(session))
            elif args.command == "protocol":
                _print(queries.protocol_detail(session, args.protocol_id))
            elif args.command == "audit":
                scope = AuditScope(
                    symbols=tuple(s.strip() for s in args.symbols.split(",") if s.strip()),
                    start=args.start,
                    end=args.end,
                    benchmark=args.benchmark or None,
                    provider=args.provider,
                    feed=args.feed,
                    known_at=clock.now(),
                    universe_selected_on=args.selected_on,
                )
                audit_row = evidence.run_audit(session, scope, actor=args.actor, now=clock.now())
                view = queries.audit_view(audit_row, full=not args.brief)
                _print(view)
            elif args.command == "scorecard":
                req = evidence.ScorecardRequest(
                    protocol_id=args.protocol,
                    strategy_runs=_runs(args.run),
                    portfolio_run_id=args.portfolio_run,
                    data_audit_id=args.audit,
                    account_name=args.account,
                    extra_strategies=tuple(args.strategy),
                )
                card = evidence.build_scorecard(session, req, actor=args.actor, now=clock.now())
                view = queries.scorecard_view(session, card, full=True)
                _print(_brief(view) if args.brief else view)
            elif args.command == "scorecards":
                _print(queries.scorecards(session))
            elif args.command == "show":
                _print(queries.scorecard_detail(session, args.scorecard_id))
            elif args.command == "latest":
                found = queries.latest_scorecard(session)
                _print(None if found is None else (_brief(found) if args.brief else found))
            else:
                return 2
        return 0
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid evidence criteria"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except AtlasError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
