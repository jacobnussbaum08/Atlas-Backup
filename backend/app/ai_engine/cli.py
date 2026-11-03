"""AI decision engine command line (Phase 5B): ``python -m app.ai_engine.cli``.

* ``decide``: a LIVE run now (RECORDED inputs; requests ML predictions of APPROVED/SHADOW
  models first). ``decide --at ISO [--basis RECONSTRUCTED]``: a HISTORICAL run.
* ``decide-range --start ISO --end ISO [--basis ...]``: HISTORICAL runs at every session's
  close plus the settle delay in a range (idempotent per policy version and time).
* ``replay --run ID``: re-evaluate a stored run from its stored inputs; compare hashes.
* ``evaluate [--start] [--end] [--cost-bps]``: past decisions against simple baselines on
  matured outcomes (stored in ``decision_evaluations``).
* ``analyze-news [--lookback-hours] [--limit]``: one asynchronous LLM news-analysis cycle.
* ``explain-conflicts [--limit]``: LLM explanations of recorded conflicts (reading material).
* ``research --symbol SYM``: an LLM research summary from stored analyses.
* ``usage``: LLM tokens, estimated costs and budget state.
* ``runs`` / ``policy``: recorded runs; the current policy's parameters.

``--policy FILE`` (or ``AI_DECISION_POLICY_FILE``) selects a policy JSON. Nothing here
sizes, approves or submits a trade.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select

from app.ai_engine import queries
from app.ai_engine.coordinator.baselines import evaluate_decisions
from app.ai_engine.coordinator.service import CoordinatorError
from app.ai_engine.enums import DecisionMode
from app.ai_engine.llm.providers import build_provider
from app.ai_engine.llm.service import LLMService
from app.ai_engine.llm.tasks import ConflictExplainer, NewsAnalyzer, research_summary
from app.ai_engine.runtime import AIContext, build_ai_context, load_policy
from app.asset_discovery.sessions import trading_day
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
from app.database.models import Asset
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.market_intelligence.enums import KnowledgeBasis
from app.ml_engine.runtime import build_ml_context
from app.strategies.config import get_strategy_settings

ACTOR = "operator:cli"


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.ai_engine.cli")
    parser.add_argument("--policy", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("decide")
    cmd.add_argument("--at", type=_timestamp)
    cmd.add_argument("--basis", type=KnowledgeBasis, default=KnowledgeBasis.RECORDED)
    cmd = sub.add_parser("decide-range")
    cmd.add_argument("--start", type=_timestamp, required=True)
    cmd.add_argument("--end", type=_timestamp, required=True)
    cmd.add_argument("--basis", type=KnowledgeBasis, default=KnowledgeBasis.RECONSTRUCTED)
    sub.add_parser("replay").add_argument("--run", type=uuid.UUID, required=True)
    cmd = sub.add_parser("evaluate")
    cmd.add_argument("--start", type=_timestamp)
    cmd.add_argument("--end", type=_timestamp)
    cmd.add_argument("--cost-bps", type=float, default=None)
    cmd.add_argument("--min-observations", type=int, default=30)
    cmd = sub.add_parser("analyze-news")
    cmd.add_argument("--lookback-hours", type=int, default=None)
    cmd.add_argument("--limit", type=int, default=None)
    sub.add_parser("explain-conflicts").add_argument("--limit", type=int, default=5)
    cmd = sub.add_parser("research")
    cmd.add_argument("--symbol", required=True)
    cmd.add_argument("--lookback-hours", type=int, default=168)
    sub.add_parser("usage")
    sub.add_parser("runs")
    sub.add_parser("policy")
    return parser


def _settle_times(ctx: AIContext, start: datetime, end: datetime, settle: float) -> list[datetime]:
    out = []
    day = start.astimezone(ctx.calendar.zone).date()
    last = end.astimezone(ctx.calendar.zone).date()
    while day <= last:
        session = trading_day(ctx.calendar, day)
        if session is not None:
            at = session.regular_close + timedelta(seconds=settle)
            if start <= at <= end:
                out.append(at)
        day += timedelta(days=1)
    return out


async def _llm(ctx: AIContext, command: str, args: argparse.Namespace) -> Any:
    async with httpx.AsyncClient() as client:
        service = LLMService(
            ctx.session_factory,
            ctx.settings,
            ctx.clock,
            build_provider(ctx.settings, client),
            worker_id="cli",
        )
        if not service.available:
            return {"status": "UNAVAILABLE", "reason": "AI_LLM_PROVIDER is none"}
        service.sweep_interrupted()
        if command == "analyze-news":
            report = await NewsAnalyzer(ctx.session_factory, service, ctx.clock).run_cycle(
                lookback=timedelta(
                    hours=args.lookback_hours or ctx.settings.news_analysis_lookback_hours
                ),
                limit=args.limit or ctx.settings.news_analysis_max_articles_per_cycle,
                actor=ACTOR,
            )
            return report.to_json()
        if command == "explain-conflicts":
            explainer = ConflictExplainer(ctx.session_factory, service, ctx.clock)
            results = []
            for run_id in explainer.pending_runs(args.limit):
                outcome = await explainer.explain(run_id, actor=ACTOR)
                results.append({"run_id": str(run_id), "status": outcome.status})
            return results
        with session_scope(ctx.session_factory) as session:
            asset_id = session.scalar(select(Asset.id).where(Asset.symbol == args.symbol.upper()))
        if asset_id is None:
            return {"error": f"unknown symbol {args.symbol}"}
        outcome = await research_summary(
            ctx.session_factory,
            service,
            ctx.clock,
            asset_id,
            lookback=timedelta(hours=args.lookback_hours),
            actor=ACTOR,
        )
        return {
            "status": outcome.status,
            "reason": outcome.reason,
            "summary": None if outcome.parsed is None else outcome.parsed.model_dump(mode="json"),
        }


def _run(argv: list[str] | None) -> int:
    args = _parser().parse_args(argv)
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
        inference = None
        if args.command == "decide" and args.at is None:
            market = build_market_data_context(sessions, clock)
            inference = build_ml_context(
                sessions, clock, provider=market.provider.provider_id, feed=market.provider.feed
            ).inference
        ctx = build_ai_context(sessions, clock, inference=inference, worker_id="cli")
        policy = load_policy(args.policy) if args.policy else ctx.policy
        out: Any
        if args.command == "decide":
            result = ctx.coordinator.run(
                policy,
                mode=DecisionMode.LIVE if args.at is None else DecisionMode.HISTORICAL,
                decision_at=args.at,
                basis=args.basis,
                trigger="cli",
                actor=ACTOR,
            )
            out = result.__dict__
        elif args.command == "decide-range":
            settle = get_strategy_settings().settle_seconds
            out = []
            for at in _settle_times(ctx, args.start, args.end, settle):
                result = ctx.coordinator.run(
                    policy,
                    mode=DecisionMode.HISTORICAL,
                    decision_at=at,
                    basis=args.basis,
                    trigger="cli-range",
                    actor=ACTOR,
                )
                out.append({"decision_at": at.isoformat(), **result.__dict__})
        elif args.command == "replay":
            out = ctx.coordinator.replay(args.run)
        elif args.command == "evaluate":
            with session_scope(sessions) as session:
                out = evaluate_decisions(
                    session,
                    calendar=ctx.calendar,
                    known_at=clock.now(),
                    start=args.start,
                    end=args.end,
                    cost_bps=policy.assumed_round_trip_cost_bps
                    if args.cost_bps is None
                    else args.cost_bps,
                    settle_seconds=get_strategy_settings().settle_seconds,
                    min_observations=args.min_observations,
                    actor=ACTOR,
                )
        elif args.command in ("analyze-news", "explain-conflicts", "research"):
            out = asyncio.run(_llm(ctx, args.command, args))
        elif args.command == "usage":
            with session_scope(sessions) as session:
                out = queries.usage(session, ctx.settings, clock.now())
        elif args.command == "runs":
            with session_scope(sessions) as session:
                out = [r.model_dump(mode="json") for r in queries.runs(session, limit=50)]
        else:
            out = {"sha256": policy.sha256(), "parameters": policy.parameters()}
        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ValidationError as exc:
        print(describe_validation_error(exc, "Invalid policy"), file=sys.stderr)
    except (InvalidSettingsError, UnknownSettingError) as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
    except AtlasError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
    except CoordinatorError as exc:
        print(f"ai_error: {exc}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
