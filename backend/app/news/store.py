"""Idempotent persistence of news observations (Phase 3C, D-075).

One call persists a batch inside the caller's transaction:

1. Per article, an advisory transaction lock (taken for the whole batch in sorted order,
   so the stream worker and a backfill job never race or deadlock on the same article).
2. Duplicate detection: an observation equal to a stored revision (same stored fields,
   same provider ``updated_at``, and the same body hash where both carry a body) changes
   nothing but the article's ``observation_count``/``last_seen_at``. Overlapping stream
   recovery and live delivery therefore never create duplicates.
3. Anything else is a new, append-only revision: ``PROVIDER_UPDATE`` (``updated_at``
   advanced), ``CONTENT_CHANGED`` (fields differ, same ``updated_at``) or
   ``LATE_EARLIER_VERSION`` (an older version arriving after a newer one; stored for the
   record, but the article's current state stays the newest version).
4. Each new revision is mapped to assets at its publication time, classified, and its
   event, categories and asset relationships (with relevance) are stored.

Timestamps (never interchanged): ``published_at``/``provider_updated_at`` are the
provider's; ``received_at`` is when Atlas got the observation; ``processed_at`` and
``available_at`` when the revision was stored (>= received); ``original_available_at`` is
the live receipt time for stream observations and NULL for REST retrievals.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.clocks import Clock
from app.database.models import (
    Asset,
    EventAssetRelationshipRecord,
    EventClassificationRecord,
    NewsArticleRecord,
    NewsArticleRevisionRecord,
    NewsAssetMappingRecord,
    NewsEventRecord,
    NewsSourceRecord,
)
from app.news.classification import CLASSIFIER_VERSION, Classification, classify
from app.news.enums import (
    ArticleOrigin,
    AssetRelationship,
    MappingStatus,
    RevisionReason,
)
from app.news.mapping import MAPPING_VERSION, SymbolMapper, SymbolMapping
from app.news.relevance import RELEVANCE_VERSION, static_relevance
from app.news.schemas import ProviderNewsArticle


@dataclass
class IngestResult:
    received: int = 0
    new_articles: int = 0
    new_revisions: int = 0
    duplicates: int = 0
    article_ids: list[uuid.UUID] = field(default_factory=list)
    revision_ids: list[int] = field(default_factory=list)
    latest_received_at: datetime | None = None

    def add(self, other: IngestResult) -> None:
        self.received += other.received
        self.new_articles += other.new_articles
        self.new_revisions += other.new_revisions
        self.duplicates += other.duplicates
        self.article_ids.extend(other.article_ids)
        self.revision_ids.extend(other.revision_ids)
        if other.latest_received_at is not None:
            self.latest_received_at = max(
                self.latest_received_at or other.latest_received_at, other.latest_received_at
            )


def _lock_key(provider: str, article_id: str) -> str:
    return f"news_article:{provider}:{article_id}"


class NewsStore:
    """Callers own the transaction; the store never commits."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Clock,
        namespace: str,
        normalize_symbol: Callable[[str], str],
        ambiguity_window: timedelta,
    ) -> None:
        self._session = session
        self._clock = clock
        self._mapper = SymbolMapper(
            session,
            namespace=namespace,
            normalize=normalize_symbol,
            ambiguity_window=ambiguity_window,
        )
        self._namespace = namespace

    # ------------------------------------------------------------ public
    def ingest(
        self,
        articles: Sequence[ProviderNewsArticle],
        *,
        job_id: uuid.UUID | None = None,
        stream_session_id: uuid.UUID | None = None,
    ) -> IngestResult:
        result = IngestResult()
        if not articles:
            return result
        keys = sorted({_lock_key(a.provider, a.provider_article_id) for a in articles})
        for key in keys:
            self._session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
        ordered = sorted(
            articles,
            key=lambda a: (
                a.provider,
                a.provider_article_id,
                a.provider_updated_at or a.published_at,
                a.received_at,
            ),
        )
        for article in ordered:
            result.add(self._ingest_one(article, job_id, stream_session_id))
        return result

    # ------------------------------------------------------------ one observation
    def _ingest_one(
        self,
        obs: ProviderNewsArticle,
        job_id: uuid.UUID | None,
        stream_session_id: uuid.UUID | None,
    ) -> IngestResult:
        result = IngestResult(received=1, latest_received_at=obs.received_at)
        source_id = self._source(obs)
        article = self._session.scalars(
            select(NewsArticleRecord).where(
                NewsArticleRecord.provider == obs.provider,
                NewsArticleRecord.provider_article_id == obs.provider_article_id,
            )
        ).one_or_none()
        now = max(self._clock.now(), obs.received_at)
        live_at = obs.received_at if obs.origin is ArticleOrigin.REALTIME_STREAM else None

        if article is None:
            article = NewsArticleRecord(
                id=uuid.uuid4(),
                provider=obs.provider,
                provider_article_id=obs.provider_article_id,
                source_id=source_id,
                published_at=obs.published_at,
                provider_updated_at=obs.provider_updated_at,
                first_received_at=obs.received_at,
                first_available_at=now,
                first_origin=obs.origin,
                first_live_received_at=live_at,
                current_revision=1,
                headline=obs.headline,
                summary=obs.summary,
                author=obs.author,
                url=obs.url,
                symbols=list(obs.symbols),
                content_sha256=obs.content_sha256,
                observation_count=1,
                last_seen_at=obs.received_at,
            )
            self._session.add(article)
            self._session.flush()
            revision = self._add_revision(
                article, obs, 1, RevisionReason.FIRST_OBSERVATION, now, job_id, stream_session_id
            )
            self._process(article, revision, obs, make_current=True)
            result.new_articles = 1
            result.new_revisions = 1
            result.article_ids.append(article.id)
            result.revision_ids.append(revision.id)
            return result

        # Known article: the lock serialises writers, so the revision list is complete.
        article.observation_count += 1
        article.last_seen_at = max(article.last_seen_at, obs.received_at)
        if live_at is not None and article.first_live_received_at is None:
            article.first_live_received_at = live_at
        revisions = self._session.execute(
            select(
                NewsArticleRevisionRecord.revision,
                NewsArticleRevisionRecord.content_sha256,
                NewsArticleRevisionRecord.provider_updated_at,
                NewsArticleRevisionRecord.body_sha256,
            ).where(NewsArticleRevisionRecord.article_id == article.id)
        ).all()
        content_sha = obs.content_sha256
        for _, sha, updated, body in revisions:
            if (
                sha == content_sha
                and updated == obs.provider_updated_at
                and (body is None or obs.body_sha256 is None or body == obs.body_sha256)
            ):
                result.duplicates = 1
                result.article_ids.append(article.id)
                return result

        latest_number = max(r[0] for r in revisions)
        current = self._session.scalars(
            select(NewsArticleRevisionRecord).where(
                NewsArticleRevisionRecord.article_id == article.id,
                NewsArticleRevisionRecord.revision == article.current_revision,
            )
        ).one()
        current_updated = current.provider_updated_at
        if obs.provider_updated_at is not None and current_updated is not None:
            if obs.provider_updated_at > current_updated:
                reason = RevisionReason.PROVIDER_UPDATE
            elif obs.provider_updated_at < current_updated:
                reason = RevisionReason.LATE_EARLIER_VERSION
            else:
                reason = RevisionReason.CONTENT_CHANGED
        else:
            reason = RevisionReason.CONTENT_CHANGED
        revision = self._add_revision(
            article, obs, latest_number + 1, reason, now, job_id, stream_session_id
        )
        make_current = reason is not RevisionReason.LATE_EARLIER_VERSION
        if make_current:
            article.current_revision = revision.revision
            article.published_at = obs.published_at
            article.provider_updated_at = obs.provider_updated_at
            article.headline = obs.headline
            article.summary = obs.summary
            article.author = obs.author
            article.url = obs.url
            article.symbols = list(obs.symbols)
            article.content_sha256 = content_sha
            article.source_id = source_id
        self._process(article, revision, obs, make_current=make_current)
        result.new_revisions = 1
        result.article_ids.append(article.id)
        result.revision_ids.append(revision.id)
        return result

    def _add_revision(
        self,
        article: NewsArticleRecord,
        obs: ProviderNewsArticle,
        number: int,
        reason: RevisionReason,
        now: datetime,
        job_id: uuid.UUID | None,
        stream_session_id: uuid.UUID | None,
    ) -> NewsArticleRevisionRecord:
        live = obs.origin is ArticleOrigin.REALTIME_STREAM
        revision = NewsArticleRevisionRecord(
            article_id=article.id,
            revision=number,
            reason=reason,
            origin=obs.origin,
            headline=obs.headline,
            summary=obs.summary,
            author=obs.author,
            source_code=obs.source,
            url=obs.url,
            symbols=list(obs.symbols),
            published_at=obs.published_at,
            provider_updated_at=obs.provider_updated_at,
            content=obs.content,
            has_body=obs.has_body,
            body_sha256=obs.body_sha256,
            content_sha256=obs.content_sha256,
            images=[image.model_dump() for image in obs.images],
            provider_categories=list(obs.provider_categories),
            provider_payload=obs.provider_payload,
            received_at=obs.received_at,
            processed_at=now,
            available_at=now,
            original_available_at=obs.received_at if live else None,
            ingestion_job_id=None if live else job_id,
            stream_session_id=stream_session_id if live else None,
        )
        self._session.add(revision)
        self._session.flush()
        return revision

    # ------------------------------------------------------------ mapping and events
    def _process(
        self,
        article: NewsArticleRecord,
        revision: NewsArticleRevisionRecord,
        obs: ProviderNewsArticle,
        *,
        make_current: bool,
    ) -> None:
        mappings = self._mapper.map(list(obs.symbols), obs.published_at)
        mapping_rows = self._store_mappings(article, revision, mappings)
        classification = classify(
            headline=obs.headline,
            summary=obs.summary,
            symbols=obs.symbols,
            provider_categories=obs.provider_categories,
        )
        self._store_event(
            article, revision, classification, mappings, mapping_rows, revision.available_at
        )
        if make_current:
            mapped = [m.asset_id for m in mappings if m.asset_id is not None]
            article.mapped_asset_ids = list(dict.fromkeys(mapped))
            article.primary_category = classification.primary
            article.scope = classification.scope(len(article.mapped_asset_ids))
            article.classifier_version = classification.classifier_version

    def _store_mappings(
        self,
        article: NewsArticleRecord,
        revision: NewsArticleRevisionRecord,
        mappings: list[SymbolMapping],
    ) -> dict[str, NewsAssetMappingRecord]:
        rows: dict[str, NewsAssetMappingRecord] = {}
        for m in mappings:
            row = NewsAssetMappingRecord(
                revision_id=revision.id,
                article_id=article.id,
                provider_symbol=m.provider_symbol,
                symbol_namespace=self._namespace,
                status=m.status,
                asset_id=m.asset_id,
                asset_identifier_id=m.asset_identifier_id,
                asset_status=m.asset_status,
                resolved_for=revision.published_at,
                mapping_version=MAPPING_VERSION,
                reason=m.reason,
                candidates=m.candidates,
            )
            self._session.add(row)
            rows[m.provider_symbol] = row
        self._session.flush()
        return rows

    def _store_event(
        self,
        article: NewsArticleRecord,
        revision: NewsArticleRevisionRecord,
        classification: Classification,
        mappings: list[SymbolMapping],
        mapping_rows: dict[str, NewsAssetMappingRecord],
        available_at: datetime,
    ) -> NewsEventRecord:
        mapped = [m for m in mappings if m.status is MappingStatus.MAPPED]
        distinct_assets = len({m.asset_id for m in mapped})
        event = NewsEventRecord(
            article_id=article.id,
            revision_id=revision.id,
            taxonomy_version=classification.taxonomy_version,
            classifier_version=classification.classifier_version,
            rules_sha256=classification.rules_sha256,
            primary_category=classification.primary,
            scope=classification.scope(distinct_assets),
            event_time=revision.published_at,
            available_at=available_at,
        )
        self._session.add(event)
        self._session.flush()
        for a in classification.assignments:
            self._session.add(
                EventClassificationRecord(
                    event_id=event.id,
                    category=a.category,
                    source=a.source,
                    method=a.method,
                    confidence=a.confidence,
                    verification=a.verification,
                    is_primary=a.is_primary,
                    score=a.score,
                    provider_label=a.provider_label,
                    evidence=a.evidence,
                )
            )
        seen: set[uuid.UUID] = set()
        for m in mapped:
            assert m.asset_id is not None  # noqa: S101  # MAPPED implies an asset
            if m.asset_id in seen:
                continue
            seen.add(m.asset_id)
            relevance = static_relevance(
                headline=revision.headline,
                asset_symbol=m.asset_symbol,
                asset_name=m.asset_name,
                tagged_symbols=len(revision.symbols),
                mapped_assets=distinct_assets,
                source=revision.source_code,
                has_summary=revision.summary is not None,
            )
            self._session.add(
                EventAssetRelationshipRecord(
                    event_id=event.id,
                    asset_id=m.asset_id,
                    mapping_id=mapping_rows[m.provider_symbol].id,
                    relationship=AssetRelationship.DIRECT_PROVIDER_SYMBOL,
                    relevance_version=RELEVANCE_VERSION,
                    static_relevance=relevance.score,
                    relevance_factors=relevance.factors,
                    event_time=revision.published_at,
                    available_at=available_at,
                )
            )
        self._session.flush()
        return event

    # ------------------------------------------------------------ reclassification
    def reclassify(self, revision_ids: Sequence[int]) -> int:
        """Classify stored revisions under the current classifier version, reusing their
        stored symbol mappings. The new events are available only from now: a backtest at an
        earlier time keeps seeing the version that existed then. Returns events created."""
        created = 0
        now = self._clock.now()
        for revision_id in sorted(revision_ids):
            revision = self._session.get(NewsArticleRevisionRecord, revision_id)
            if revision is None:
                continue
            article = self._session.get(NewsArticleRecord, revision.article_id)
            assert article is not None  # noqa: S101  # foreign key
            key = _lock_key(article.provider, article.provider_article_id)
            self._session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))
            exists_already = self._session.scalar(
                select(NewsEventRecord.id).where(
                    NewsEventRecord.revision_id == revision.id,
                    NewsEventRecord.classifier_version == CLASSIFIER_VERSION,
                )
            )
            if exists_already is not None:
                continue
            rows = self._session.execute(
                select(NewsAssetMappingRecord, Asset)
                .outerjoin(Asset, Asset.id == NewsAssetMappingRecord.asset_id)
                .where(
                    NewsAssetMappingRecord.revision_id == revision.id,
                    NewsAssetMappingRecord.mapping_version == MAPPING_VERSION,
                )
                .order_by(NewsAssetMappingRecord.id)
            ).all()
            mappings = [
                SymbolMapping(
                    provider_symbol=row.provider_symbol,
                    status=row.status,
                    reason=row.reason,
                    asset_id=row.asset_id,
                    asset_identifier_id=row.asset_identifier_id,
                    asset_symbol=asset.symbol if asset is not None else None,
                    asset_name=asset.name if asset is not None else None,
                    asset_status=row.asset_status,
                )
                for row, asset in rows
            ]
            classification = classify(
                headline=revision.headline,
                summary=revision.summary,
                symbols=revision.symbols,
                provider_categories=revision.provider_categories,
            )
            self._store_event(
                article,
                revision,
                classification,
                mappings,
                {row.provider_symbol: row for row, _ in rows},
                max(now, revision.available_at),
            )
            if revision.revision == article.current_revision:
                article.primary_category = classification.primary
                article.scope = classification.scope(len(article.mapped_asset_ids))
                article.classifier_version = classification.classifier_version
            created += 1
        return created

    # ------------------------------------------------------------ sources
    def _source(self, obs: ProviderNewsArticle) -> int | None:
        if obs.source is None:
            return None
        stmt = (
            insert(NewsSourceRecord)
            .values(
                provider=obs.provider,
                code=obs.source,
                display_name=obs.source,
                first_seen_at=obs.received_at,
                last_seen_at=obs.received_at,
            )
            .on_conflict_do_update(
                index_elements=["provider", "code"],
                set_={
                    "last_seen_at": func.greatest(NewsSourceRecord.last_seen_at, obs.received_at)
                },
            )
            .returning(NewsSourceRecord.id)
        )
        return int(self._session.execute(stmt).scalar_one())


__all__ = ["CLASSIFIER_VERSION", "IngestResult", "NewsStore"]
