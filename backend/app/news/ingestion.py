"""Historical news retrieval jobs (Phase 3C).

A job retrieves one range (all news, or named symbols) through the provider's REST
endpoint, page by page, and persists each page in its own transaction through
:class:`NewsStore` (idempotent: re-running a range, or overlapping the live stream, adds
nothing that is already stored). Jobs are durable rows in ``news_ingestion_jobs`` with
counters, a heartbeat and a final status; a job whose process died (stale heartbeat) is
failed as interrupted by the next job creation, as D-064 does for market data.

Every revision written by a job is labelled ``HISTORICAL_REST``: its ``available_at`` is
when Atlas stored it and its ``original_available_at`` is NULL (D-075).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.exceptions import InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.errors import InvalidProviderSymbolError, MarketDataError
from app.database.models import (
    NewsArticleRevisionRecord,
    NewsEventRecord,
    NewsIngestionJobRecord,
)
from app.database.sessions import session_scope
from app.news.classification import CLASSIFIER_VERSION
from app.news.config import NewsSettings
from app.news.enums import ACTIVE_JOB_STATUSES, NewsJobKind, NewsJobStatus
from app.news.providers.base import NewsProvider
from app.news.schemas import NewsQuery
from app.news.store import IngestResult, NewsStore

logger = get_logger(__name__)

HEARTBEAT_INTERVAL = timedelta(seconds=30)
STALE_AFTER = timedelta(minutes=5)
INTERRUPTED_REASON = "interrupted: the process running this job stopped"
MAX_MALFORMED_SAMPLES = 20


class JobOwnershipLostError(RuntimeError):
    """The job row is no longer RUNNING (reclaimed or cancelled): stop without writing."""


class NewsIngestionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: NewsProvider,
        settings: NewsSettings,
        clock: Clock,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._settings = settings
        self._clock = clock

    @property
    def provider(self) -> NewsProvider:
        return self._provider

    def store(self, session: Session) -> NewsStore:
        return NewsStore(
            session,
            clock=self._clock,
            namespace=self._provider.capabilities().symbol_namespace,
            normalize_symbol=self._provider.normalize_symbol,
            ambiguity_window=timedelta(hours=self._settings.mapping_ambiguity_window_hours),
        )

    # ------------------------------------------------------------ jobs
    def create_job(
        self,
        *,
        kind: NewsJobKind,
        start: datetime,
        end: datetime,
        symbols: Sequence[str] = (),
        requested_by: str,
        stream_session_id: uuid.UUID | None = None,
        params: dict[str, Any] | None = None,
    ) -> NewsIngestionJobRecord:
        if end <= start:
            raise InvalidRequestError("end must be after start")
        if end - start > timedelta(days=self._settings.historical_max_range_days):
            raise InvalidRequestError(
                f"range exceeds NEWS_HISTORICAL_MAX_RANGE_DAYS "
                f"({self._settings.historical_max_range_days} days); split it"
            )
        if end > self._clock.now() + timedelta(minutes=1):
            raise InvalidRequestError("end is in the future")
        try:
            normalized = list(dict.fromkeys(self._provider.normalize_symbol(s) for s in symbols))
        except InvalidProviderSymbolError as exc:
            raise InvalidRequestError(exc.message) from None
        now = self._clock.now()
        with session_scope(self._sessions) as session:
            self._reclaim_stale(session, now)
            job = NewsIngestionJobRecord(
                id=uuid.uuid4(),
                provider=self._provider.provider_id,
                kind=kind,
                status=NewsJobStatus.PENDING,
                symbols=normalized,
                range_start=start,
                range_end=end,
                include_content=self._settings.store_content,
                requested_by=requested_by[:64],
                stream_session_id=stream_session_id,
                params=params or {},
                created_at=now,
                heartbeat_at=now,
            )
            session.add(job)
            session.flush()
            session.expunge(job)
        logger.info(
            "news.job_created",
            job_id=str(job.id),
            kind=kind.value,
            symbols=len(normalized),
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return job

    def _reclaim_stale(self, session: Session, now: datetime) -> int:
        result = session.execute(
            update(NewsIngestionJobRecord)
            .where(
                NewsIngestionJobRecord.status.in_(ACTIVE_JOB_STATUSES),
                NewsIngestionJobRecord.heartbeat_at < now - STALE_AFTER,
            )
            .values(status=NewsJobStatus.FAILED, error=INTERRUPTED_REASON, completed_at=now)
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            logger.warning("news.jobs_interrupted", count=count)
        return count

    def get_job(self, job_id: uuid.UUID) -> NewsIngestionJobRecord:
        with session_scope(self._sessions) as session:
            job = session.get(NewsIngestionJobRecord, job_id)
            if job is None:
                raise NotFoundError(f"news ingestion job {job_id} does not exist")
            session.expunge(job)
            return job

    async def run_job(self, job_id: uuid.UUID) -> NewsIngestionJobRecord:
        """Run a PENDING job to completion. Failures are recorded on the job, not raised."""
        await asyncio.to_thread(self._claim, job_id)
        job = self.get_job(job_id)
        query = NewsQuery(
            symbols=tuple(job.symbols),
            start=job.range_start,
            end=job.range_end,
            page_size=self._settings.historical_page_size,
            include_content=job.include_content,
        )
        heartbeat = asyncio.create_task(self._heartbeat(job_id), name=f"news-job-{job_id}")
        totals = IngestResult()
        pages = 0
        malformed: list[dict[str, Any]] = []
        malformed_count = 0
        status, error = NewsJobStatus.COMPLETED, None
        pages_iter = self._provider.fetch_news(query)
        try:
            async for page in pages_iter:
                pages += 1
                malformed_count += len(page.malformed)
                for item in page.malformed[: max(0, MAX_MALFORMED_SAMPLES - len(malformed))]:
                    malformed.append({"id": item.provider_article_id, "reason": item.reason})
                result = await asyncio.to_thread(
                    self._persist_page, job_id, page.articles, pages, totals, malformed_count
                )
                totals.add(result)
        except JobOwnershipLostError:
            logger.warning("news.job_ownership_lost", job_id=str(job_id))
            status, error = NewsJobStatus.CANCELLED, None
        except MarketDataError as exc:
            status, error = NewsJobStatus.FAILED, exc.message
        except Exception as exc:  # recorded on the job; a bug must not leave it RUNNING
            logger.exception("news.job_crashed", job_id=str(job_id))
            status, error = NewsJobStatus.FAILED, f"internal error: {type(exc).__name__}"
        finally:
            aclose = getattr(pages_iter, "aclose", None)
            if aclose is not None:  # close the HTTP client even when stopping early
                with contextlib.suppress(Exception):
                    await aclose()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        if status is not NewsJobStatus.CANCELLED:
            await asyncio.to_thread(
                self._finish, job_id, status, error, pages, totals, malformed_count, malformed
            )
        logger.info(
            "news.job_finished",
            job_id=str(job_id),
            status=status.value,
            pages=pages,
            received=totals.received,
            new_articles=totals.new_articles,
            new_revisions=totals.new_revisions,
            duplicates=totals.duplicates,
            malformed=malformed_count,
        )
        return self.get_job(job_id)

    def _claim(self, job_id: uuid.UUID) -> None:
        now = self._clock.now()
        with session_scope(self._sessions) as session:
            job = session.get(NewsIngestionJobRecord, job_id, with_for_update=True)
            if job is None:
                raise NotFoundError(f"news ingestion job {job_id} does not exist")
            if job.status is not NewsJobStatus.PENDING:
                raise InvalidRequestError(f"job {job_id} is {job.status.value}, not PENDING")
            job.status = NewsJobStatus.RUNNING
            job.started_at = now
            job.heartbeat_at = now

    def _persist_page(
        self,
        job_id: uuid.UUID,
        articles: Sequence[Any],
        pages: int,
        totals: IngestResult,
        malformed: int,
    ) -> IngestResult:
        with session_scope(self._sessions) as session:
            job = session.get(NewsIngestionJobRecord, job_id, with_for_update=True)
            if job is None or job.status is not NewsJobStatus.RUNNING:
                raise JobOwnershipLostError(str(job_id))
            result = self.store(session).ingest(articles, job_id=job_id)
            job.pages_retrieved = pages
            job.articles_received = totals.received + result.received
            job.articles_new = totals.new_articles + result.new_articles
            job.revisions_new = totals.new_revisions + result.new_revisions
            job.duplicates = totals.duplicates + result.duplicates
            job.malformed = malformed
            job.heartbeat_at = self._clock.now()
            return result

    def _finish(
        self,
        job_id: uuid.UUID,
        status: NewsJobStatus,
        error: str | None,
        pages: int,
        totals: IngestResult,
        malformed: int,
        samples: list[dict[str, Any]],
    ) -> None:
        now = self._clock.now()
        with session_scope(self._sessions) as session:
            job = session.get(NewsIngestionJobRecord, job_id, with_for_update=True)
            if job is None or job.status is not NewsJobStatus.RUNNING:
                return  # reclaimed meanwhile: never overwrite that outcome
            job.status = status
            job.error = redact_text(error)[:2000] if error else None
            job.pages_retrieved = pages
            job.articles_received = totals.received
            job.articles_new = totals.new_articles
            job.revisions_new = totals.new_revisions
            job.duplicates = totals.duplicates
            job.malformed = malformed
            if samples:
                job.params = {**job.params, "malformed_samples": samples}
            job.heartbeat_at = now
            job.completed_at = now

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL.total_seconds())
            try:
                await asyncio.to_thread(self._beat, job_id)
            except Exception:  # the next page write retries; the job stays observable
                logger.warning("news.job_heartbeat_failed", job_id=str(job_id))

    def _beat(self, job_id: uuid.UUID) -> None:
        with session_scope(self._sessions) as session:
            session.execute(
                update(NewsIngestionJobRecord)
                .where(
                    NewsIngestionJobRecord.id == job_id,
                    NewsIngestionJobRecord.status == NewsJobStatus.RUNNING,
                )
                .values(heartbeat_at=self._clock.now())
            )

    def reclassify_pending(self, *, batch_size: int = 500, limit: int | None = None) -> int:
        """Add events under the current classifier version for revisions that have none
        (after a rule change). Earlier events stay; new ones are available from now."""
        done = 0
        while limit is None or done < limit:
            size = batch_size if limit is None else min(batch_size, limit - done)
            with session_scope(self._sessions) as session:
                classified = select(NewsEventRecord.revision_id).where(
                    NewsEventRecord.classifier_version == CLASSIFIER_VERSION
                )
                ids = list(
                    session.scalars(
                        select(NewsArticleRevisionRecord.id)
                        .where(NewsArticleRevisionRecord.id.not_in(classified))
                        .order_by(NewsArticleRevisionRecord.id)
                        .limit(size)
                    )
                )
                if not ids:
                    break
                done += self.store(session).reclassify(ids)
        logger.info("news.reclassified", events=done, classifier_version=CLASSIFIER_VERSION)
        return done

    def list_jobs(
        self, *, limit: int = 50, kind: NewsJobKind | None = None
    ) -> list[NewsIngestionJobRecord]:
        with session_scope(self._sessions) as session:
            stmt = select(NewsIngestionJobRecord).order_by(NewsIngestionJobRecord.created_at.desc())
            if kind is not None:
                stmt = stmt.where(NewsIngestionJobRecord.kind == kind)
            jobs = list(session.scalars(stmt.limit(limit)))
            for job in jobs:
                session.expunge(job)
            return jobs


def run_job_blocking(service: NewsIngestionService, job_id: uuid.UUID) -> None:
    """Run a job on its own event loop (API background thread)."""
    try:
        asyncio.run(service.run_job(job_id))
    except Exception:
        logger.exception("news.background_job_failed", job_id=str(job_id))


__all__ = [
    "INTERRUPTED_REASON",
    "STALE_AFTER",
    "JobOwnershipLostError",
    "NewsIngestionService",
    "run_job_blocking",
]
