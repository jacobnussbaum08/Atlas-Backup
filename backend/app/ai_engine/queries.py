"""Read models of the AI decision engine (Phase 5B) for the API and dashboard.

Every number shown comes from stored records. Scores are labelled heuristics; a
probability appears only where an approved, calibrated model produced it; costs are
computed from the configured prices and labelled as estimates.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai_engine.config import AISettings
from app.ai_engine.enums import DecisionOutcome, LLMTask
from app.ai_engine.llm import budget
from app.database.models import (
    AIDecisionRecord,
    Asset,
    DecisionConflictRecord,
    DecisionEvaluationRecord,
    DecisionEvidenceRecord,
    DecisionInputRecord,
    DecisionPolicyRecord,
    DecisionPolicyVersionRecord,
    DecisionRunRecord,
    DecisionScoringComponentRecord,
    LLMAnalysisRecord,
    LLMCostRecord,
    LLMRequestRecord,
    LLMTokenUsageRecord,
    NewsArticleRecord,
    StrategyProposalRecord,
)

DISCLAIMER = (
    "The coordinator ranks analytical opportunities. PRIORITIZE_FOR_REVIEW means a candidate "
    "for the future deterministic risk engine (Phase 6); it is not an approved trade, carries "
    "no position size and was sent nowhere. Heuristic and ranking scores are not "
    "probabilities of profit. No strategy, model or ranking has demonstrated profitability."
)
LLM_NOTE = (
    "LLM outputs are unverified estimates stored with the time they became available; they "
    "are low-weight inputs at most and never authorize, size or execute anything. Costs are "
    "computed from operator-configured prices: estimates, not invoices."
)


class PolicyVersionView(BaseModel):
    id: uuid.UUID
    code: str
    version: int
    logic_version: str
    parameters: dict[str, Any]
    parameters_sha256: str
    reason: str
    created_by: str
    created_at: datetime


class RunSummary(BaseModel):
    id: uuid.UUID
    policy_version_id: uuid.UUID
    policy_code: str
    policy_version: int
    mode: str
    basis: str
    trigger: str
    decision_at: datetime
    knowledge_at: datetime
    status: str
    outcome: str | None
    proposals_considered: int
    candidates_count: int
    prioritized_count: int
    outcome_counts: dict[str, int]
    notes: list[str]
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class ConflictView(BaseModel):
    id: int
    run_id: uuid.UUID
    type: str
    asset_id: uuid.UUID | None
    symbol: str | None
    decision_ids: list[uuid.UUID]
    proposal_ids: list[uuid.UUID]
    handling: str
    description: str
    data: dict[str, Any]


class DecisionSummary(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    outcome: DecisionOutcome
    rank: int | None
    asset_id: uuid.UUID | None
    symbol: str | None
    direction: str | None
    timeframes: list[str]
    strategies: list[str]
    proposal_count: int
    heuristic_score: float | None
    ranking_score: float | None
    component_coverage: float | None
    calibrated_probability: float | None
    expected_value_status: str | None
    reward_risk_net: float | None
    uncertainty_index: float | None
    reasons: list[dict[str, Any]]
    conflict_types: list[str]
    decision_at: datetime
    data_cutoff: datetime
    expires_at: datetime


class ComponentView(BaseModel):
    name: str
    kind: str
    status: str
    score: float | None
    raw_value: float | None
    weight: float
    contribution: float | None
    reason: str
    inputs: dict[str, Any]


class EvidenceView(BaseModel):
    id: int
    kind: str
    stance: str
    summary: str
    input_id: int | None
    data: dict[str, Any]


class ProposalBrief(BaseModel):
    id: uuid.UUID
    strategy_id: str
    direction: str
    timeframe: str
    signal_at: datetime
    data_cutoff: datetime
    reference_entry: Decimal
    invalidation_level: Decimal | None
    target_reference: Decimal | None
    signal_strength: Decimal
    strength_kind: str
    expires_at: datetime
    reasoning: str


class AnalysisView(BaseModel):
    id: uuid.UUID
    task: str
    subject_kind: str
    subject_ref: str
    article_id: uuid.UUID | None
    headline: str | None
    run_id: uuid.UUID | None
    symbols: list[str]
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    analysis: dict[str, Any]
    flags: list[str]
    usable: bool
    subject_time: datetime | None
    available_at: datetime


class DecisionDetail(DecisionSummary):
    policy_version_id: uuid.UUID
    proposal_ids: list[uuid.UUID]
    strategy_versions: list[dict[str, Any]]
    model_versions: list[dict[str, Any]]
    regime_snapshot: dict[str, Any]
    news_refs: list[dict[str, Any]]
    llm_refs: list[dict[str, Any]]
    feature_timestamps: dict[str, Any]
    scores: dict[str, Any]
    expected: dict[str, Any]
    uncertainty: dict[str, Any]
    conflicts: list[dict[str, Any]]
    explanation: str
    content_sha256: str
    created_at: datetime
    components: list[ComponentView]
    evidence: list[EvidenceView]
    proposals: list[ProposalBrief]
    llm_analyses: list[AnalysisView]


class RunDetail(RunSummary):
    inputs_sha256: str | None
    result_sha256: str | None
    summary: dict[str, Any]
    code_version: dict[str, Any]
    input_counts: dict[str, int]
    decisions: list[DecisionSummary]
    conflicts: list[ConflictView]
    explanations: list[AnalysisView]


class RequestView(BaseModel):
    id: uuid.UUID
    task: str
    subject_kind: str
    subject_count: int
    provider: str
    model: str
    prompt_version: str
    status: str
    attempts: int
    estimated_input_tokens: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    reserved_cost_usd: Decimal | None
    error: str | None
    requested_at: datetime
    finished_at: datetime | None


class EvaluationView(BaseModel):
    id: uuid.UUID
    policy_version_id: uuid.UUID | None
    known_at: datetime
    decision_start: datetime | None
    decision_end: datetime | None
    runs_evaluated: int
    spec: dict[str, Any]
    results: dict[str, Any]
    verdict: str
    notes: list[str]
    created_at: datetime


# --- helpers ---------------------------------------------------------------------------


def _run_summary(run: DecisionRunRecord, code: str, version: int) -> dict[str, Any]:
    return {
        "id": run.id,
        "policy_version_id": run.policy_version_id,
        "policy_code": code,
        "policy_version": version,
        "mode": run.mode.value,
        "basis": run.basis.value,
        "trigger": run.trigger,
        "decision_at": run.decision_at,
        "knowledge_at": run.knowledge_at,
        "status": run.status.value,
        "outcome": None if run.outcome is None else run.outcome.value,
        "proposals_considered": run.proposals_considered,
        "candidates_count": run.candidates_count,
        "prioritized_count": run.prioritized_count,
        "outcome_counts": dict((run.summary or {}).get("outcomes", {})),
        "notes": list(run.notes or []),
        "error": run.error,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _runs_select() -> Any:
    v, p = DecisionPolicyVersionRecord, DecisionPolicyRecord
    return (
        select(DecisionRunRecord, p.code, v.version)
        .join(v, v.id == DecisionRunRecord.policy_version_id)
        .join(p, p.id == v.policy_id)
    )


def _decision_summary(d: AIDecisionRecord) -> dict[str, Any]:
    ev = (d.expected or {}).get("expected_value") or {}
    return {
        "id": d.id,
        "run_id": d.run_id,
        "outcome": d.outcome,
        "rank": d.rank,
        "asset_id": d.asset_id,
        "symbol": d.symbol,
        "direction": None if d.direction is None else d.direction.value,
        "timeframes": list(d.timeframes),
        "strategies": [s["strategy_id"] for s in d.strategy_versions],
        "proposal_count": len(d.proposal_ids),
        "heuristic_score": d.scores.get("heuristic_score"),
        "ranking_score": d.scores.get("ranking_score"),
        "component_coverage": d.scores.get("component_coverage"),
        "calibrated_probability": d.scores.get("calibrated_probability"),
        "expected_value_status": ev.get("status"),
        "reward_risk_net": (d.expected or {}).get("reward_risk_net"),
        "uncertainty_index": (d.uncertainty or {}).get("index"),
        "reasons": list(d.reasons),
        "conflict_types": sorted({c.get("type", "") for c in d.conflicts}),
        "decision_at": d.decision_at,
        "data_cutoff": d.data_cutoff,
        "expires_at": d.expires_at,
    }


def _order_decisions() -> Any:
    d = AIDecisionRecord
    return (d.rank.is_(None), d.rank, d.outcome, d.symbol, d.direction)


def _analysis_views(session: Session, rows: list[LLMAnalysisRecord]) -> list[AnalysisView]:
    article_ids = {r.article_id for r in rows if r.article_id is not None}
    headlines = (
        dict(
            session.execute(
                select(NewsArticleRecord.id, NewsArticleRecord.headline).where(
                    NewsArticleRecord.id.in_(list(article_ids))
                )
            ).all()
        )
        if article_ids
        else {}
    )
    asset_ids = {a for r in rows for a in r.asset_ids}
    symbols = (
        dict(
            session.execute(
                select(Asset.id, Asset.symbol).where(Asset.id.in_(list(asset_ids)))
            ).all()
        )
        if asset_ids
        else {}
    )
    return [
        AnalysisView(
            id=r.id,
            task=r.task.value,
            subject_kind=r.subject_kind.value,
            subject_ref=r.subject_ref,
            article_id=r.article_id,
            headline=headlines.get(r.article_id) if r.article_id else None,
            run_id=r.run_id,
            symbols=sorted(symbols.get(a, str(a)) for a in r.asset_ids),
            provider=r.provider,
            model=r.model,
            prompt_version=r.prompt_version,
            schema_version=r.schema_version,
            analysis=r.analysis,
            flags=list(r.flags),
            usable=r.usable,
            subject_time=r.subject_time,
            available_at=r.available_at,
        )
        for r in rows
    ]


def _conflict_views(session: Session, rows: list[DecisionConflictRecord]) -> list[ConflictView]:
    ids = {r.asset_id for r in rows if r.asset_id is not None}
    symbols = (
        dict(session.execute(select(Asset.id, Asset.symbol).where(Asset.id.in_(list(ids)))).all())
        if ids
        else {}
    )
    return [
        ConflictView(
            id=r.id,
            run_id=r.run_id,
            type=r.type.value,
            asset_id=r.asset_id,
            symbol=symbols.get(r.asset_id) if r.asset_id else None,
            decision_ids=list(r.decision_ids),
            proposal_ids=list(r.proposal_ids),
            handling=r.handling,
            description=r.description,
            data=r.data,
        )
        for r in rows
    ]


# --- queries ---------------------------------------------------------------------------


def policies(session: Session) -> list[PolicyVersionView]:
    v, p = DecisionPolicyVersionRecord, DecisionPolicyRecord
    rows = session.execute(
        select(v, p.code).join(p, p.id == v.policy_id).order_by(p.code, v.version.desc())
    ).all()
    return [
        PolicyVersionView(
            id=r.id,
            code=code,
            version=r.version,
            logic_version=r.logic_version,
            parameters=r.parameters,
            parameters_sha256=r.parameters_sha256,
            reason=r.reason,
            created_by=r.created_by,
            created_at=r.created_at,
        )
        for r, code in rows
    ]


def runs(
    session: Session,
    *,
    status: str | None = None,
    outcome: str | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[RunSummary]:
    r = DecisionRunRecord
    stmt = _runs_select()
    if status is not None:
        stmt = stmt.where(r.status == status)
    if outcome is not None:
        stmt = stmt.where(r.outcome == outcome)
    if before is not None:
        stmt = stmt.where(r.decision_at < before)
    rows = session.execute(stmt.order_by(r.decision_at.desc(), r.started_at.desc()).limit(limit))
    return [RunSummary(**_run_summary(run, code, version)) for run, code, version in rows]


def run(session: Session, run_id: uuid.UUID) -> RunDetail | None:
    row = session.execute(_runs_select().where(DecisionRunRecord.id == run_id)).first()
    if row is None:
        return None
    record, code, version = row
    decisions = session.scalars(
        select(AIDecisionRecord)
        .where(AIDecisionRecord.run_id == run_id)
        .order_by(*_order_decisions())
    ).all()
    counts = dict(
        session.execute(
            select(DecisionInputRecord.kind, func.count())
            .where(DecisionInputRecord.run_id == run_id)
            .group_by(DecisionInputRecord.kind)
        ).all()
    )
    conflicts = list(
        session.scalars(
            select(DecisionConflictRecord)
            .where(DecisionConflictRecord.run_id == run_id)
            .order_by(DecisionConflictRecord.id)
        )
    )
    explanations = list(
        session.scalars(
            select(LLMAnalysisRecord)
            .where(
                LLMAnalysisRecord.run_id == run_id,
                LLMAnalysisRecord.task == LLMTask.CONFLICT_EXPLANATION,
            )
            .order_by(LLMAnalysisRecord.available_at.desc())
        )
    )
    return RunDetail(
        **_run_summary(record, code, version),
        inputs_sha256=record.inputs_sha256,
        result_sha256=record.result_sha256,
        summary=record.summary,
        code_version=record.code_version,
        input_counts={k.value: int(n) for k, n in counts.items()},
        decisions=[DecisionSummary(**_decision_summary(d)) for d in decisions],
        conflicts=_conflict_views(session, conflicts),
        explanations=_analysis_views(session, explanations),
    )


def decisions(
    session: Session,
    *,
    outcome: str | None = None,
    asset_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> list[DecisionSummary]:
    d = AIDecisionRecord
    stmt = select(d)
    if outcome is not None:
        stmt = stmt.where(d.outcome == outcome)
    if asset_id is not None:
        stmt = stmt.where(d.asset_id == asset_id)
    if run_id is not None:
        stmt = stmt.where(d.run_id == run_id)
    if before is not None:
        stmt = stmt.where(d.decision_at < before)
    rows = session.scalars(stmt.order_by(d.decision_at.desc(), *_order_decisions()).limit(limit))
    return [DecisionSummary(**_decision_summary(x)) for x in rows]


def rankings(
    session: Session, run_id: uuid.UUID | None
) -> tuple[RunSummary | None, list[DecisionSummary]]:
    """A run's ranked opportunities (default: the newest completed run)."""
    r = DecisionRunRecord
    stmt = _runs_select().where(r.status == "COMPLETED")
    stmt = (
        stmt.where(r.id == run_id)
        if run_id is not None
        else stmt.order_by(r.decision_at.desc(), r.started_at.desc())
    )
    row = session.execute(stmt.limit(1)).first()
    if row is None:
        return None, []
    record, code, version = row
    found = session.scalars(
        select(AIDecisionRecord)
        .where(AIDecisionRecord.run_id == record.id)
        .order_by(*_order_decisions())
    ).all()
    return RunSummary(**_run_summary(record, code, version)), [
        DecisionSummary(**_decision_summary(d)) for d in found
    ]


def decision(session: Session, decision_id: uuid.UUID) -> DecisionDetail | None:
    d = session.get(AIDecisionRecord, decision_id)
    if d is None:
        return None
    components = session.scalars(
        select(DecisionScoringComponentRecord)
        .where(DecisionScoringComponentRecord.decision_id == decision_id)
        .order_by(DecisionScoringComponentRecord.id)
    ).all()
    evidence = session.scalars(
        select(DecisionEvidenceRecord)
        .where(DecisionEvidenceRecord.decision_id == decision_id)
        .order_by(DecisionEvidenceRecord.id)
    ).all()
    proposals = (
        session.scalars(
            select(StrategyProposalRecord).where(
                StrategyProposalRecord.id.in_(list(d.proposal_ids))
            )
        ).all()
        if d.proposal_ids
        else []
    )
    llm_ids = [uuid.UUID(x["analysis_id"]) for x in d.llm_refs if x.get("analysis_id")]
    analyses = (
        list(session.scalars(select(LLMAnalysisRecord).where(LLMAnalysisRecord.id.in_(llm_ids))))
        if llm_ids
        else []
    )
    return DecisionDetail(
        **_decision_summary(d),
        policy_version_id=d.policy_version_id,
        proposal_ids=list(d.proposal_ids),
        strategy_versions=d.strategy_versions,
        model_versions=d.model_versions,
        regime_snapshot=d.regime_snapshot,
        news_refs=d.news_refs,
        llm_refs=d.llm_refs,
        feature_timestamps=d.feature_timestamps,
        scores=d.scores,
        expected=d.expected,
        uncertainty=d.uncertainty,
        conflicts=d.conflicts,
        explanation=d.explanation,
        content_sha256=d.content_sha256,
        created_at=d.created_at,
        components=[
            ComponentView(
                name=c.name,
                kind=c.kind.value,
                status=c.status.value,
                score=c.score,
                raw_value=c.raw_value,
                weight=c.weight,
                contribution=c.contribution,
                reason=c.reason,
                inputs=c.inputs,
            )
            for c in components
        ],
        evidence=[
            EvidenceView(
                id=e.id,
                kind=e.kind.value,
                stance=e.stance.value,
                summary=e.summary,
                input_id=e.input_id,
                data=e.data,
            )
            for e in evidence
        ],
        proposals=[
            ProposalBrief(
                id=p.id,
                strategy_id=p.strategy_id,
                direction=p.direction.value,
                timeframe=p.timeframe,
                signal_at=p.signal_at,
                data_cutoff=p.data_cutoff,
                reference_entry=p.reference_entry,
                invalidation_level=p.invalidation_level,
                target_reference=p.target_reference,
                signal_strength=p.signal_strength,
                strength_kind=p.strength_kind.value,
                expires_at=p.expires_at,
                reasoning=p.reasoning,
            )
            for p in sorted(proposals, key=lambda p: (p.strategy_id, p.timeframe, str(p.id)))
        ],
        llm_analyses=_analysis_views(session, analyses),
    )


def evidence(session: Session, decision_id: uuid.UUID) -> list[EvidenceView] | None:
    found = decision(session, decision_id)
    return None if found is None else found.evidence


def model_contributions(session: Session, decision_id: uuid.UUID) -> dict[str, Any] | None:
    found = decision(session, decision_id)
    if found is None:
        return None
    ml = next((c for c in found.components if c.name == "ml_support"), None)
    return {
        "decision_id": str(decision_id),
        "model_versions": found.model_versions,
        "ml_component": None if ml is None else ml.model_dump(),
        "calibrated_probability": found.scores.get("calibrated_probability"),
        "calibrated_probability_meaning": found.scores.get("calibrated_probability_meaning"),
        "expected_value": found.expected.get("expected_value"),
        "evidence": [e.model_dump() for e in found.evidence if e.kind == "ML_PREDICTION"],
    }


def conflicts(
    session: Session, *, run_id: uuid.UUID | None = None, limit: int = 100
) -> list[ConflictView]:
    stmt = select(DecisionConflictRecord)
    if run_id is not None:
        stmt = stmt.where(DecisionConflictRecord.run_id == run_id)
    rows = list(session.scalars(stmt.order_by(DecisionConflictRecord.id.desc()).limit(limit)))
    return _conflict_views(session, rows)


def analyses(
    session: Session,
    *,
    task: str | None = None,
    usable: bool | None = None,
    asset_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[AnalysisView]:
    a = LLMAnalysisRecord
    stmt = select(a)
    if task is not None:
        stmt = stmt.where(a.task == task)
    if usable is not None:
        stmt = stmt.where(a.usable.is_(usable))
    if asset_id is not None:
        stmt = stmt.where(a.asset_ids.contains([asset_id]))
    rows = list(session.scalars(stmt.order_by(a.available_at.desc(), a.id).limit(limit)))
    return _analysis_views(session, rows)


def analysis(session: Session, analysis_id: uuid.UUID) -> AnalysisView | None:
    row = session.get(LLMAnalysisRecord, analysis_id)
    return None if row is None else _analysis_views(session, [row])[0]


def requests(session: Session, *, status: str | None = None, limit: int = 50) -> list[RequestView]:
    r = LLMRequestRecord
    tokens = (
        select(
            LLMTokenUsageRecord.request_id,
            func.sum(LLMTokenUsageRecord.input_tokens).label("input_tokens"),
            func.sum(LLMTokenUsageRecord.output_tokens).label("output_tokens"),
        )
        .group_by(LLMTokenUsageRecord.request_id)
        .subquery()
    )
    costs = (
        select(LLMCostRecord.request_id, func.sum(LLMCostRecord.cost_usd).label("cost"))
        .group_by(LLMCostRecord.request_id)
        .subquery()
    )
    stmt = (
        select(r, tokens.c.input_tokens, tokens.c.output_tokens, costs.c.cost)
        .outerjoin(tokens, tokens.c.request_id == r.id)
        .outerjoin(costs, costs.c.request_id == r.id)
    )
    if status is not None:
        stmt = stmt.where(r.status == status)
    rows = session.execute(stmt.order_by(r.requested_at.desc()).limit(limit)).all()
    return [
        RequestView(
            id=x.id,
            task=x.task.value,
            subject_kind=x.subject_kind.value,
            subject_count=len(x.subject_refs),
            provider=x.provider,
            model=x.model,
            prompt_version=x.prompt_version,
            status=x.status.value,
            attempts=x.attempts,
            estimated_input_tokens=x.estimated_input_tokens,
            input_tokens=None if inp is None else int(inp),
            output_tokens=None if out is None else int(out),
            cost_usd=cost,
            reserved_cost_usd=x.reserved_cost_usd,
            error=x.error,
            requested_at=x.requested_at,
            finished_at=x.finished_at,
        )
        for x, inp, out, cost in rows
    ]


def usage(session: Session, settings: AISettings, now: datetime, days: int = 30) -> dict[str, Any]:
    start = budget.day_start(now) - timedelta(days=days - 1)
    u, c, r = LLMTokenUsageRecord, LLMCostRecord, LLMRequestRecord
    day = func.date_trunc("day", u.recorded_at)
    by_day = [
        {
            "day": d.date().isoformat(),
            "input_tokens": int(i or 0),
            "output_tokens": int(o or 0),
            "responses": int(n),
        }
        for d, i, o, n in session.execute(
            select(day, func.sum(u.input_tokens), func.sum(u.output_tokens), func.count())
            .where(u.recorded_at >= start)
            .group_by(day)
            .order_by(day)
        )
    ]
    cost_day = func.date_trunc("day", c.recorded_at)
    costs_by_day = {
        d.date().isoformat(): str(total)
        for d, total in session.execute(
            select(cost_day, func.sum(c.cost_usd)).where(c.recorded_at >= start).group_by(cost_day)
        )
    }
    for item in by_day:
        item["cost_usd"] = costs_by_day.get(item["day"], "0")
    by_task = [
        {
            "task": t.value,
            "model": m,
            "input_tokens": int(i or 0),
            "output_tokens": int(o or 0),
            "responses": int(n),
        }
        for t, m, i, o, n in session.execute(
            select(
                u.task, u.model, func.sum(u.input_tokens), func.sum(u.output_tokens), func.count()
            )
            .where(u.recorded_at >= start)
            .group_by(u.task, u.model)
            .order_by(u.task, u.model)
        )
    ]
    task_costs = {
        (t.value, m): str(total)
        for t, m, total in session.execute(
            select(c.task, c.model, func.sum(c.cost_usd))
            .where(c.recorded_at >= start)
            .group_by(c.task, c.model)
        )
    }
    for item in by_task:
        item["cost_usd"] = task_costs.get((str(item["task"]), str(item["model"])), "0")
    statuses = {
        s.value: int(n)
        for s, n in session.execute(
            select(r.status, func.count()).where(r.requested_at >= start).group_by(r.status)
        )
    }
    return {
        "window_days": days,
        "configuration": {
            "provider": settings.llm_provider.value,
            "model": settings.llm_model or None,
            "priced": settings.priced,
            "input_usd_per_mtok": None
            if settings.llm_input_usd_per_mtok is None
            else str(settings.llm_input_usd_per_mtok),
            "output_usd_per_mtok": None
            if settings.llm_output_usd_per_mtok is None
            else str(settings.llm_output_usd_per_mtok),
            "pricing_source": settings.llm_pricing_source or None,
            "max_concurrency": settings.llm_max_concurrency,
            "max_retries": settings.llm_max_retries,
            "timeout_seconds": settings.llm_timeout_seconds,
            "batch_size": settings.llm_batch_size,
            "max_input_tokens": settings.llm_max_input_tokens,
            "max_output_tokens": settings.llm_max_output_tokens,
            "news_analysis_enabled": settings.news_analysis_enabled,
            "conflict_explanations_enabled": settings.conflict_explanations_enabled,
        },
        "budget": budget.state(session, settings, now).to_json(),
        "by_day": by_day,
        "by_task": by_task,
        "request_statuses": statuses,
        "note": LLM_NOTE,
    }


def evaluations(session: Session, limit: int = 20) -> list[EvaluationView]:
    rows = session.scalars(
        select(DecisionEvaluationRecord)
        .order_by(DecisionEvaluationRecord.created_at.desc())
        .limit(limit)
    )
    return [
        EvaluationView(
            id=e.id,
            policy_version_id=e.policy_version_id,
            known_at=e.known_at,
            decision_start=e.decision_start,
            decision_end=e.decision_end,
            runs_evaluated=e.runs_evaluated,
            spec=e.spec,
            results=e.results,
            verdict=e.verdict.value,
            notes=list(e.notes),
            created_at=e.created_at,
        )
        for e in rows
    ]


def overview(session: Session, settings: AISettings) -> dict[str, Any]:
    r, d, a = DecisionRunRecord, AIDecisionRecord, LLMAnalysisRecord
    run_counts = {
        s.value: int(n)
        for s, n in session.execute(select(r.status, func.count()).group_by(r.status))
    }
    outcome_counts = {
        o.value: int(n)
        for o, n in session.execute(select(d.outcome, func.count()).group_by(d.outcome))
    }
    analysis_counts = {
        f"{t.value}:{'usable' if u else 'unusable'}": int(n)
        for t, u, n in session.execute(
            select(a.task, a.usable, func.count()).group_by(a.task, a.usable)
        )
    }
    latest = runs(session, status="COMPLETED", limit=1)
    return {
        "runs": run_counts,
        "decisions": outcome_counts,
        "llm_analyses": analysis_counts,
        "latest_run": None if not latest else latest[0].model_dump(mode="json"),
        "llm": {
            "provider": settings.llm_provider.value,
            "model": settings.llm_model or None,
            "configured": settings.llm_provider.value != "none",
            "priced": settings.priced,
        },
        "disclaimer": DISCLAIMER,
        "llm_note": LLM_NOTE,
    }


__all__ = [
    "DISCLAIMER",
    "LLM_NOTE",
    "AnalysisView",
    "ComponentView",
    "ConflictView",
    "DecisionDetail",
    "DecisionSummary",
    "EvaluationView",
    "EvidenceView",
    "PolicyVersionView",
    "RequestView",
    "RunDetail",
    "RunSummary",
    "analyses",
    "analysis",
    "conflicts",
    "decision",
    "decisions",
    "evaluations",
    "evidence",
    "model_contributions",
    "overview",
    "policies",
    "rankings",
    "requests",
    "run",
    "runs",
    "usage",
]
