"""LLM tasks over stored data (Phase 5B): news analysis, conflict explanations and
research summaries. Each stores validated outputs as immutable ``llm_analyses`` records
with the time they became available.

**News analysis** (asynchronous, spec 12.4): current revisions of recent articles that
Atlas mapped to at least one asset are analysed in batches. Before anything is sent:

* a revision whose ``content_sha256`` was already analysed under the same prompt, provider
  and model is not sent again: its analysis is reused (flagged ``reused_from``);
* a revision that already produced two INVALID_OUTPUT requests under this prompt is
  skipped (it would likely fail again and cost again);
* only the mapped symbols are offered; symbols the model adds are dropped and flagged.

An analysis with suspected prompt injection (pattern match before sending, or the model's
own report) is stored with ``usable = false`` and is never read by a decision.

**Conflict explanations** run after a decision run with recorded conflicts. Their input is
Atlas's own stored records; their output is reading material for a reviewer and is never
an input to any decision (the run is already final).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session, sessionmaker

from app.ai_engine.enums import LLMRequestStatus, LLMSubjectKind, LLMTask
from app.ai_engine.llm.prompts import CONFLICT_EXPLANATION, NEWS_ANALYSIS, RESEARCH_SUMMARY
from app.ai_engine.llm.safety import clean_text, injection_flags
from app.ai_engine.llm.schemas import ConflictExplanation, NewsAnalysisBatch, ResearchSummary
from app.ai_engine.llm.service import CallOutcome, LLMService, TaskCall
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.database.models import (
    AIDecisionRecord,
    Asset,
    DecisionConflictRecord,
    DecisionRunRecord,
    LLMAnalysisRecord,
    LLMRequestRecord,
    NewsArticleRecord,
    NewsArticleRevisionRecord,
    NewsAssetMappingRecord,
)
from app.database.sessions import session_scope
from app.news.enums import MappingStatus

logger = get_logger(__name__)

MAX_INVALID_ATTEMPTS = 2
BLOCKING_FLAG_PREFIXES = ("suspected_prompt_injection", "model_reported_embedded_instructions")


@dataclass(frozen=True)
class PendingArticle:
    revision_id: int
    article_id: uuid.UUID
    content_sha256: str
    headline: str
    text: str | None
    published_at: datetime
    source: str | None
    # Provider symbol -> asset id (mapped at publication, D-079).
    symbols: dict[str, uuid.UUID]


@dataclass
class CycleReport:
    selected: int = 0
    analysed: int = 0
    reused: int = 0
    cached: int = 0
    unusable: int = 0
    failed: int = 0
    skipped: int = 0
    statuses: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _analysis_exists(
    session: Session, *, subject_ref: str, content_sha256: str, provider: str, model: str
) -> bool:
    a = LLMAnalysisRecord
    return (
        session.scalar(
            select(func.count()).where(
                a.task == LLMTask.NEWS_ANALYSIS,
                a.subject_kind == LLMSubjectKind.NEWS_REVISION,
                a.subject_ref == subject_ref,
                a.content_sha256 == content_sha256,
                a.prompt_version == NEWS_ANALYSIS.version,
                a.provider == provider,
                a.model == model,
            )
        )
        or 0
    ) > 0


class NewsAnalyzer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        service: LLMService,
        clock: Clock,
    ) -> None:
        self._sessions = session_factory
        self.service = service
        self.clock = clock

    def pending(self, *, lookback: timedelta, limit: int) -> list[PendingArticle]:
        provider = self.service.provider
        if provider is None:
            return []
        now = self.clock.now()
        art, rev = NewsArticleRecord, NewsArticleRevisionRecord
        a = LLMAnalysisRecord
        analysed = (
            select(a.id)
            .where(
                a.task == LLMTask.NEWS_ANALYSIS,
                a.subject_ref == func.cast(rev.id, a.subject_ref.type),
                a.prompt_version == NEWS_ANALYSIS.version,
                a.provider == provider.name,
                a.model == provider.model,
            )
            .exists()
        )
        r = LLMRequestRecord
        invalid = (
            select(func.count())
            .where(
                r.task == LLMTask.NEWS_ANALYSIS,
                r.prompt_version == NEWS_ANALYSIS.version,
                r.status == LLMRequestStatus.INVALID_OUTPUT,
                r.subject_refs.op("@>")(
                    func.jsonb_build_array(func.cast(rev.id, a.subject_ref.type)).cast(JSONB)
                ),
            )
            .scalar_subquery()
        )
        with session_scope(self._sessions) as session:
            rows = (
                session.execute(
                    select(rev)
                    .join(art, and_(art.id == rev.article_id, art.current_revision == rev.revision))
                    .where(
                        rev.published_at >= now - lookback,
                        rev.available_at <= now,
                        func.cardinality(art.mapped_asset_ids) > 0,
                        ~analysed,
                        invalid < MAX_INVALID_ATTEMPTS,
                    )
                    .order_by(rev.published_at.desc(), rev.id.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            ids = [x.id for x in rows]
            mapped: dict[int, dict[str, uuid.UUID]] = {}
            if ids:
                for m in session.scalars(
                    select(NewsAssetMappingRecord).where(
                        NewsAssetMappingRecord.revision_id.in_(ids),
                        NewsAssetMappingRecord.status == MappingStatus.MAPPED,
                    )
                ):
                    if m.asset_id is not None:
                        mapped.setdefault(m.revision_id, {})[m.provider_symbol] = m.asset_id
            limit_chars = self.service.settings.llm_max_article_chars
            return [
                PendingArticle(
                    revision_id=x.id,
                    article_id=x.article_id,
                    content_sha256=x.content_sha256,
                    headline=clean_text(x.headline, 400) or "",
                    text=clean_text(x.content or x.summary, limit_chars),
                    published_at=x.published_at,
                    source=x.source_code,
                    symbols=dict(sorted(mapped.get(x.id, {}).items())),
                )
                for x in rows
                if mapped.get(x.id)
            ]

    def _reuse(self, article: PendingArticle) -> bool:
        """Store an existing analysis of identical content for this revision."""
        provider = self.service.provider
        assert provider is not None  # noqa: S101
        now = self.clock.now()
        with session_scope(self._sessions) as session:
            source = session.scalar(
                select(LLMAnalysisRecord)
                .where(
                    LLMAnalysisRecord.task == LLMTask.NEWS_ANALYSIS,
                    LLMAnalysisRecord.content_sha256 == article.content_sha256,
                    LLMAnalysisRecord.prompt_version == NEWS_ANALYSIS.version,
                    LLMAnalysisRecord.provider == provider.name,
                    LLMAnalysisRecord.model == provider.model,
                )
                .order_by(LLMAnalysisRecord.available_at)
                .limit(1)
            )
            if source is None:
                return False
            session.add(
                LLMAnalysisRecord(
                    id=uuid.uuid4(),
                    task=LLMTask.NEWS_ANALYSIS,
                    subject_kind=LLMSubjectKind.NEWS_REVISION,
                    subject_ref=str(article.revision_id),
                    article_id=article.article_id,
                    revision_id=article.revision_id,
                    asset_ids=list(source.asset_ids),
                    content_sha256=article.content_sha256,
                    request_id=source.request_id,
                    response_id=source.response_id,
                    provider=source.provider,
                    model=source.model,
                    prompt_version=source.prompt_version,
                    schema_version=source.schema_version,
                    analysis=source.analysis,
                    flags=[*source.flags, f"reused_from:{source.id}"],
                    usable=source.usable,
                    subject_time=article.published_at,
                    available_at=now,
                )
            )
        return True

    @staticmethod
    def build_call(batch: list[PendingArticle]) -> tuple[TaskCall, dict[str, PendingArticle]]:
        refs = {f"a{i + 1}": a for i, a in enumerate(batch)}
        data = {
            "articles": [
                {
                    "article_ref": ref,
                    "headline": a.headline,
                    "text": a.text,
                    "published_at": a.published_at.isoformat(),
                    "source": a.source,
                    "symbols": sorted(a.symbols),
                }
                for ref, a in refs.items()
            ]
        }

        def check(parsed: NewsAnalysisBatch) -> list[str]:
            got = [x.article_ref for x in parsed.analyses]
            errors = []
            if sorted(got) != sorted(refs):
                errors.append(f"expected one analysis per article_ref {sorted(refs)}, got {got}")
            return errors

        return (
            TaskCall(
                prompt=NEWS_ANALYSIS,
                subject_kind=LLMSubjectKind.NEWS_REVISION,
                subject_refs=[str(a.revision_id) for a in batch],
                data=data,
                intro=f"Analyse each of the {len(batch)} articles below.",
                check=check,
            ),
            refs,
        )

    def store(
        self, outcome: CallOutcome, refs: dict[str, PendingArticle], report: CycleReport
    ) -> None:
        provider = self.service.provider
        assert provider is not None  # noqa: S101
        assert isinstance(outcome.parsed, NewsAnalysisBatch)  # noqa: S101
        assert outcome.request_id is not None  # noqa: S101
        assert outcome.response_id is not None  # noqa: S101
        parsed = outcome.parsed
        now = self.clock.now()
        with session_scope(self._sessions) as session:
            for item in parsed.analyses:
                article = refs[item.article_ref]
                if _analysis_exists(
                    session,
                    subject_ref=str(article.revision_id),
                    content_sha256=article.content_sha256,
                    provider=provider.name,
                    model=provider.model,
                ):
                    continue
                flags = injection_flags(article.headline, article.text)
                if item.contains_instructions:
                    flags.append("model_reported_embedded_instructions")
                assets = []
                for entry in item.assets:
                    asset_id = article.symbols.get(entry.symbol.upper()) or article.symbols.get(
                        entry.symbol
                    )
                    if asset_id is None:
                        flags.append(f"out_of_scope_symbol_dropped:{entry.symbol[:32]}")
                        continue
                    assets.append({**entry.model_dump(mode="json"), "asset_id": str(asset_id)})
                relevant = sorted(
                    {uuid.UUID(e["asset_id"]) for e in assets if e["relevance"] != "NOT_RELEVANT"},
                    key=str,
                )
                usable = not any(f.startswith(BLOCKING_FLAG_PREFIXES) for f in flags)
                analysis = {
                    **item.model_dump(mode="json", exclude={"assets"}),
                    "assets": assets,
                    "label_status": "UNVERIFIED_LLM_ESTIMATE",
                }
                session.add(
                    LLMAnalysisRecord(
                        id=uuid.uuid4(),
                        task=LLMTask.NEWS_ANALYSIS,
                        subject_kind=LLMSubjectKind.NEWS_REVISION,
                        subject_ref=str(article.revision_id),
                        article_id=article.article_id,
                        revision_id=article.revision_id,
                        asset_ids=relevant,
                        content_sha256=article.content_sha256,
                        request_id=outcome.request_id,
                        response_id=outcome.response_id,
                        provider=provider.name,
                        model=provider.model,
                        prompt_version=NEWS_ANALYSIS.version,
                        schema_version=NEWS_ANALYSIS.schema_version,
                        analysis=analysis,
                        flags=flags,
                        usable=usable,
                        subject_time=article.published_at,
                        available_at=now,
                    )
                )
                report.analysed += 1
                if not usable:
                    report.unusable += 1

    async def run_cycle(
        self, *, lookback: timedelta, limit: int, actor: str = "ai-news-analysis"
    ) -> CycleReport:
        report = CycleReport()
        if self.service.provider is None:
            report.statuses["UNAVAILABLE"] = 1
            return report
        pending = self.pending(lookback=lookback, limit=limit)
        report.selected = len(pending)
        to_send: list[PendingArticle] = []
        for article in pending:
            if self._reuse(article):
                report.reused += 1
            else:
                to_send.append(article)
        size = self.service.settings.llm_batch_size
        for start in range(0, len(to_send), size):
            batch = to_send[start : start + size]
            call, refs = self.build_call(batch)
            outcome = await self.service.call(call, actor=actor)
            report.statuses[outcome.status] = report.statuses.get(outcome.status, 0) + 1
            if outcome.usable:
                if outcome.status == "CACHED":
                    report.cached += 1
                self.store(outcome, refs, report)
            elif outcome.status in ("UNAVAILABLE", "BUDGET_REFUSED", "UNPRICED"):
                report.skipped += len(to_send) - start
                break
            else:
                report.failed += len(batch)
        logger.info("ai.news_analysis_cycle", **report.to_json())
        return report


class ConflictExplainer:
    """Explains recorded conflicts of completed runs, after the fact (never an input)."""

    def __init__(
        self, session_factory: sessionmaker[Session], service: LLMService, clock: Clock
    ) -> None:
        self._sessions = session_factory
        self.service = service
        self.clock = clock

    def pending_runs(self, limit: int) -> list[uuid.UUID]:
        provider = self.service.provider
        if provider is None:
            return []
        a = LLMAnalysisRecord
        explained = (
            select(a.id)
            .where(
                a.task == LLMTask.CONFLICT_EXPLANATION,
                a.run_id == DecisionRunRecord.id,
                a.prompt_version == CONFLICT_EXPLANATION.version,
                a.provider == provider.name,
                a.model == provider.model,
            )
            .exists()
        )
        has_conflicts = (
            select(DecisionConflictRecord.id)
            .where(DecisionConflictRecord.run_id == DecisionRunRecord.id)
            .exists()
        )
        with session_scope(self._sessions) as session:
            return list(
                session.scalars(
                    select(DecisionRunRecord.id)
                    .where(DecisionRunRecord.status == "COMPLETED", has_conflicts, ~explained)
                    .order_by(DecisionRunRecord.decision_at.desc())
                    .limit(limit)
                )
            )

    def build_call(self, run_id: uuid.UUID) -> tuple[TaskCall, str]:
        with session_scope(self._sessions) as session:
            conflicts = list(
                session.scalars(
                    select(DecisionConflictRecord)
                    .where(DecisionConflictRecord.run_id == run_id)
                    .order_by(DecisionConflictRecord.id)
                )
            )
            decision_ids = {d for c in conflicts for d in c.decision_ids}
            decisions = (
                {
                    d.id: d
                    for d in session.scalars(
                        select(AIDecisionRecord).where(AIDecisionRecord.id.in_(list(decision_ids)))
                    )
                }
                if decision_ids
                else {}
            )
            refs = {f"c{c.id}": c for c in conflicts}
            evidence_refs = {f"d{i + 1}": d for i, d in enumerate(decisions.values())}
            by_id = {d.id: ref for ref, d in evidence_refs.items()}
            data = {
                "conflicts": [
                    {
                        "conflict_ref": ref,
                        "type": c.type.value,
                        "description": c.description,
                        "handling": c.handling,
                        "decisions": [by_id[d] for d in c.decision_ids if d in by_id],
                    }
                    for ref, c in refs.items()
                ],
                "decisions": [
                    {
                        "ref": ref,
                        "symbol": d.symbol,
                        "direction": None if d.direction is None else d.direction.value,
                        "outcome": d.outcome.value,
                        "heuristic_score": d.scores.get("heuristic_score"),
                        "ranking_score": d.scores.get("ranking_score"),
                        "strategies": [s["strategy_id"] for s in d.strategy_versions],
                        "reasons": [r.get("message") for r in d.reasons][:6],
                    }
                    for ref, d in evidence_refs.items()
                ],
                "note": "scores are heuristics, not probabilities",
            }
            content = str(run_id)

        allowed = set(refs) | set(evidence_refs)

        def check(parsed: ConflictExplanation) -> list[str]:
            errors = []
            for note in parsed.conflicts:
                if note.conflict_ref not in refs:
                    errors.append(f"unknown conflict_ref {note.conflict_ref}")
                bad = [r for r in note.evidence_refs if r not in allowed]
                if bad:
                    errors.append(f"unknown evidence refs {bad}")
            return errors

        return (
            TaskCall(
                prompt=CONFLICT_EXPLANATION,
                subject_kind=LLMSubjectKind.DECISION_RUN,
                subject_refs=[content],
                data=data,
                intro=f"Explain the {len(refs)} recorded conflict(s) below.",
                check=check,
            ),
            content,
        )

    async def explain(self, run_id: uuid.UUID, actor: str = "ai-explanations") -> CallOutcome:
        call, content = self.build_call(run_id)
        outcome = await self.service.call(call, actor=actor)
        provider = self.service.provider
        if outcome.usable and provider is not None:
            assert outcome.request_id is not None  # noqa: S101
            assert outcome.response_id is not None  # noqa: S101
            assert isinstance(outcome.parsed, ConflictExplanation)  # noqa: S101
            parsed = outcome.parsed
            with session_scope(self._sessions) as session:
                session.add(
                    LLMAnalysisRecord(
                        id=uuid.uuid4(),
                        task=LLMTask.CONFLICT_EXPLANATION,
                        subject_kind=LLMSubjectKind.DECISION_RUN,
                        subject_ref=content,
                        run_id=run_id,
                        content_sha256=self.service.keys(call)[1],
                        request_id=outcome.request_id,
                        response_id=outcome.response_id,
                        provider=provider.name,
                        model=provider.model,
                        prompt_version=CONFLICT_EXPLANATION.version,
                        schema_version=CONFLICT_EXPLANATION.schema_version,
                        analysis={
                            **parsed.model_dump(mode="json"),
                            "label_status": "EXPLANATION_ONLY_NOT_EVIDENCE",
                        },
                        flags=[],
                        usable=True,
                        subject_time=None,
                        available_at=self.clock.now(),
                    )
                )
        return outcome


async def research_summary(
    session_factory: sessionmaker[Session],
    service: LLMService,
    clock: Clock,
    asset_id: uuid.UUID,
    *,
    lookback: timedelta,
    actor: str = "ai-research",
) -> CallOutcome:
    """A structured research summary of one asset from its stored usable news analyses."""
    now = clock.now()
    with session_scope(session_factory) as session:
        symbol = session.scalar(select(Asset.symbol).where(Asset.id == asset_id)) or "UNKNOWN"
        rows = list(
            session.scalars(
                select(LLMAnalysisRecord)
                .where(
                    LLMAnalysisRecord.task == LLMTask.NEWS_ANALYSIS,
                    LLMAnalysisRecord.usable.is_(True),
                    LLMAnalysisRecord.asset_ids.contains([asset_id]),
                    LLMAnalysisRecord.available_at <= now,
                    LLMAnalysisRecord.subject_time >= now - lookback,
                )
                .order_by(LLMAnalysisRecord.subject_time.desc())
                .limit(20)
            )
        )
        items = {
            f"n{i + 1}": {
                "published_at": None if r.subject_time is None else r.subject_time.isoformat(),
                "event_type": r.analysis.get("event_type"),
                "summary": r.analysis.get("summary"),
                "catalysts": r.analysis.get("catalysts", []),
                "sentiment": [
                    a.get("sentiment")
                    for a in r.analysis.get("assets", [])
                    if a.get("asset_id") == str(asset_id)
                ],
            }
            for i, r in enumerate(rows)
        }
    if not items:
        return CallOutcome("UNAVAILABLE", reason="no usable stored news analysis for the asset")
    data = {"symbol": symbol, "analyses": [{"ref": k, **v} for k, v in items.items()]}

    def check(parsed: ResearchSummary) -> list[str]:
        bad = [r for r in parsed.cited_refs if r not in items]
        return [f"unknown refs {bad}"] if bad else []

    call = TaskCall(
        prompt=RESEARCH_SUMMARY,
        subject_kind=LLMSubjectKind.ASSET,
        subject_refs=[str(asset_id)],
        data=data,
        intro=f"Summarize the stored analyses of {symbol} below.",
        check=check,
    )
    outcome = await service.call(call, actor=actor)
    provider = service.provider
    if outcome.usable and provider is not None:
        assert outcome.request_id is not None  # noqa: S101
        assert outcome.response_id is not None  # noqa: S101
        assert isinstance(outcome.parsed, ResearchSummary)  # noqa: S101
        with session_scope(session_factory) as session:
            session.add(
                LLMAnalysisRecord(
                    id=uuid.uuid4(),
                    task=LLMTask.ASSET_RESEARCH_SUMMARY,
                    subject_kind=LLMSubjectKind.ASSET,
                    subject_ref=str(asset_id),
                    asset_ids=[asset_id],
                    content_sha256=service.keys(call)[1],
                    request_id=outcome.request_id,
                    response_id=outcome.response_id,
                    provider=provider.name,
                    model=provider.model,
                    prompt_version=RESEARCH_SUMMARY.version,
                    schema_version=RESEARCH_SUMMARY.schema_version,
                    analysis={
                        **outcome.parsed.model_dump(mode="json"),
                        "label_status": "UNVERIFIED_LLM_SUMMARY",
                    },
                    flags=[],
                    usable=True,
                    subject_time=now,
                    available_at=clock.now(),
                )
            )
    return outcome


__all__ = [
    "BLOCKING_FLAG_PREFIXES",
    "ConflictExplainer",
    "CycleReport",
    "NewsAnalyzer",
    "PendingArticle",
    "research_summary",
]
