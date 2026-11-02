"""Asset discovery jobs (Phase 3A): run, chain and schedule the five job kinds.

* ``DIRECTORY_DISCOVERY`` (``mode`` FULL or INCREMENTAL): provider directory -> pool.
* ``METADATA_REFRESH``: single-instrument lookups for the tracked universe (bounded).
* ``QUALIFICATION``: evaluations for the pool (with bounded history preparation).
* ``MEMBERSHIP_UPDATE`` (``layer``, ``source_job_id``): qualified universe or watchlist
  membership from a qualification or scoring job.
* ``WATCHLIST_SCORING``: ranks qualified assets.

A pipeline (``params.pipeline = true``) chains discovery -> qualification -> membership
(qualified) -> scoring -> membership (watchlist); each step is its own job with its own
status, and the chain stops at a step that fails.

Job execution never places, sizes or approves anything: the deepest effect of any job is a
membership event.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery import jobs, membership
from app.asset_discovery.config import AssetDiscoverySettings
from app.asset_discovery.discovery import DirectoryCounters, DiscoveryEngine
from app.asset_discovery.enums import (
    DiscoveryJobKind,
    DiscoveryJobStatus,
    DiscoveryMode,
    JobTrigger,
    MembershipAction,
    MetricStatus,
    QualificationOutcome,
    UniverseLayer,
)
from app.asset_discovery.qualification import QualificationService
from app.asset_discovery.rules import resolve_rule_set, resolve_scoring
from app.asset_discovery.scoring import ScoreInput, score_assets
from app.core.clocks import Clock
from app.core.config import MarketDataSettings
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.errors import (
    CredentialsNotConfiguredError,
    MarketDataError,
    ProviderAuthenticationError,
    ProviderEntitlementError,
    ProviderRequestError,
    ProviderResponseError,
    UnsupportedCapabilityError,
)
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.providers.base import MarketDataProvider
from app.database.models import (
    Asset,
    AssetMetricValueRecord,
    DiscoveryJobRecord,
    ProviderAssetRecord,
    QualificationEvaluationRecord,
    WatchlistScoreRecord,
)
from app.database.sessions import session_scope

logger = get_logger(__name__)

PIPELINE: tuple[tuple[DiscoveryJobKind, dict[str, Any]], ...] = (
    (DiscoveryJobKind.QUALIFICATION, {}),
    (DiscoveryJobKind.MEMBERSHIP_UPDATE, {"layer": UniverseLayer.QUALIFIED_UNIVERSE.value}),
    (DiscoveryJobKind.WATCHLIST_SCORING, {}),
    (DiscoveryJobKind.MEMBERSHIP_UPDATE, {"layer": UniverseLayer.ACTIVE_WATCHLIST.value}),
)
_PERMANENT = (
    CredentialsNotConfiguredError,
    ProviderAuthenticationError,
    ProviderEntitlementError,
    UnsupportedCapabilityError,
    ProviderRequestError,
    LookupError,
    ValueError,
)
HEARTBEAT_SECONDS = 30.0
_SOURCE_KEYS = {
    DiscoveryJobKind.QUALIFICATION: "qualification_job_id",
    DiscoveryJobKind.WATCHLIST_SCORING: "scoring_job_id",
}


def default_owner() -> str:
    return f"{socket.gethostname()[:32]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class AssetDiscoveryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: MarketDataProvider,
        market_settings: MarketDataSettings,
        settings: AssetDiscoverySettings,
        clock: Clock,
        *,
        ingestion: HistoricalIngestionService | None = None,
        owner: str | None = None,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._market = market_settings
        self.settings = settings
        self._clock = clock
        self.owner = owner or default_owner()
        self.engine = DiscoveryEngine(session_factory, provider, settings, clock)
        self.qualification = QualificationService(
            session_factory, provider, market_settings, settings, clock, ingestion
        )

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    @property
    def _lease(self) -> timedelta:
        return timedelta(seconds=self.settings.job_lease_seconds)

    # ------------------------------------------------------------ requests
    def request(
        self,
        kind: DiscoveryJobKind,
        *,
        trigger: JobTrigger,
        requested_by: str,
        params: dict[str, Any] | None = None,
        parent_job_id: uuid.UUID | None = None,
    ) -> jobs.CreatedJob:
        with session_scope(self._sessions) as session:
            created = jobs.create_job(
                session,
                kind=kind,
                provider=self.provider_id,
                trigger=trigger,
                requested_by=requested_by,
                params=params or {},
                now=self._clock.now(),
                max_attempts=self.settings.job_max_attempts,
                parent_job_id=parent_job_id,
            )
            session.expunge(created.job)
        return created

    # ------------------------------------------------------------ execution
    def recover(self) -> int:
        with session_scope(self._sessions) as session:
            recovered = jobs.recover_expired(session, now=self._clock.now())
        if recovered:
            logger.warning("discovery.jobs_recovered", count=len(recovered))
        return len(recovered)

    async def run_due(self, *, max_jobs: int = 20, job_id: uuid.UUID | None = None) -> int:
        """Claim and run due jobs (including pipeline successors) until none is due."""
        ran = 0
        while ran < max_jobs:
            with session_scope(self._sessions) as session:
                job = jobs.claim_next(
                    session,
                    owner=self.owner,
                    now=self._clock.now(),
                    lease=self._lease,
                    job_id=job_id if ran == 0 else None,
                )
                if job is None:
                    return ran
                session.expunge(job)
            await self._run(job)
            ran += 1
        return ran

    async def _run(self, job: DiscoveryJobRecord) -> None:
        logger.info(
            "discovery.job_started", job_id=str(job.id), kind=job.kind.value, attempt=job.attempt
        )
        beat = asyncio.create_task(self._heartbeat(job.id))
        try:
            status, counters, summary = await self._dispatch(job)
        except Exception as exc:
            beat.cancel()
            permanent = isinstance(exc, _PERMANENT) and not isinstance(exc, ProviderResponseError)
            code = exc.code if isinstance(exc, MarketDataError) else type(exc).__name__
            message = exc.message if isinstance(exc, MarketDataError) else redact_text(str(exc))
            if isinstance(exc, SQLAlchemyError):
                message, permanent = f"database error: {type(exc).__name__}", False
            logger.warning(
                "discovery.job_attempt_failed",
                job_id=str(job.id),
                kind=job.kind.value,
                code=code,
                permanent=permanent,
            )
            try:
                with session_scope(self._sessions) as session:
                    jobs.fail_attempt(
                        session,
                        job.id,
                        owner=self.owner,
                        now=self._clock.now(),
                        code=code,
                        message=message,
                        permanent=permanent,
                    )
            except SQLAlchemyError:
                logger.exception("discovery.job_failure_not_recorded", job_id=str(job.id))
            if not isinstance(
                exc,
                MarketDataError | SQLAlchemyError | jobs.JobStateError | LookupError | ValueError,
            ):
                raise
            return
        beat.cancel()
        with session_scope(self._sessions) as session:
            jobs.finish(
                session,
                job.id,
                owner=self.owner,
                now=self._clock.now(),
                status=status,
                counters=counters,
                summary=summary,
                error=None if status is DiscoveryJobStatus.COMPLETED else summary.get("problem"),
            )
        logger.info(
            "discovery.job_finished", job_id=str(job.id), kind=job.kind.value, status=status.value
        )
        if job.params.get("pipeline") and status is not DiscoveryJobStatus.FAILED:
            self._chain(job)

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                with session_scope(self._sessions) as session:
                    jobs.heartbeat(
                        session, job_id, owner=self.owner, now=self._clock.now(), lease=self._lease
                    )
            except (SQLAlchemyError, jobs.JobStateError):
                logger.warning("discovery.heartbeat_failed", job_id=str(job_id))

    def _chain(self, job: DiscoveryJobRecord) -> None:
        step = int(job.params.get("pipeline_step", -1)) + 1
        if step >= len(PIPELINE):
            return
        kind, extra = PIPELINE[step]
        # Each step finds its inputs by kind: the newest qualification and scoring job ids
        # travel down the chain.
        carried = {k: job.params[k] for k in _SOURCE_KEYS.values() if k in job.params}
        if job.kind in _SOURCE_KEYS:
            carried[_SOURCE_KEYS[job.kind]] = str(job.id)
        params = {**extra, **carried, "pipeline": True, "pipeline_step": step}
        self.request(
            kind,
            trigger=JobTrigger.PIPELINE,
            requested_by=job.requested_by,
            params=params,
            parent_job_id=job.id,
        )

    async def _dispatch(
        self, job: DiscoveryJobRecord
    ) -> tuple[DiscoveryJobStatus, dict[str, Any], dict[str, Any]]:
        if job.kind is DiscoveryJobKind.DIRECTORY_DISCOVERY:
            mode = DiscoveryMode(job.params.get("mode", DiscoveryMode.INCREMENTAL.value))
            counters = await self.engine.discover(job.id, mode)
            return self._directory_status(counters), counters.as_dict(), {"mode": mode.value}
        if job.kind is DiscoveryJobKind.METADATA_REFRESH:
            counters = await self._refresh(job)
            return self._directory_status(counters), counters.as_dict(), {}
        if job.kind is DiscoveryJobKind.QUALIFICATION:
            return await self._qualify(job)
        if job.kind is DiscoveryJobKind.WATCHLIST_SCORING:
            return self._score(job)
        if job.kind is DiscoveryJobKind.MEMBERSHIP_UPDATE:
            return self._update_membership(job)
        raise ValueError(f"unknown job kind {job.kind}")

    @staticmethod
    def _directory_status(counters: DirectoryCounters) -> DiscoveryJobStatus:
        if counters.malformed or counters.verification_deferred:
            return DiscoveryJobStatus.PARTIALLY_COMPLETED
        return DiscoveryJobStatus.COMPLETED

    # ------------------------------------------------------------ refresh
    def refresh_targets(self, limit: int) -> list[str]:
        """Watchlist, then qualified, then the least recently verified pool members."""
        with session_scope(self._sessions) as session:
            ordered: list[uuid.UUID] = []
            for layer in (UniverseLayer.ACTIVE_WATCHLIST, UniverseLayer.QUALIFIED_UNIVERSE):
                ordered += sorted(membership.current_members(session, layer))
            rows = session.execute(
                select(ProviderAssetRecord.asset_id, ProviderAssetRecord.provider_asset_id)
                .where(ProviderAssetRecord.provider == self.provider_id)
                .order_by(ProviderAssetRecord.last_seen_at)
            ).all()
            by_asset = {asset_id: pid for asset_id, pid in rows}
            pool = membership.current_members(session, UniverseLayer.DISCOVERY_POOL)
            ordered += [asset_id for asset_id, _ in rows if asset_id in pool]
        seen: list[str] = []
        for asset_id in ordered:
            pid = by_asset.get(asset_id)
            if pid is not None and pid not in seen:
                seen.append(pid)
            if len(seen) >= limit:
                break
        return seen

    async def _refresh(self, job: DiscoveryJobRecord) -> DirectoryCounters:
        limit = min(
            int(job.params.get("max_assets", self.settings.refresh_max_assets)),
            self.settings.refresh_max_assets,
        )
        requested = job.params.get("asset_ids")
        if requested:
            with session_scope(self._sessions) as session:
                ids = [uuid.UUID(a) for a in requested][:limit]
                targets = list(
                    session.scalars(
                        select(ProviderAssetRecord.provider_asset_id).where(
                            ProviderAssetRecord.provider == self.provider_id,
                            ProviderAssetRecord.asset_id.in_(ids),
                        )
                    )
                )
        else:
            targets = self.refresh_targets(limit)
        counters = DirectoryCounters()
        await self.engine.lookup(job.id, targets, counters)
        return counters

    # ------------------------------------------------------------ qualification
    async def _qualify(
        self, job: DiscoveryJobRecord
    ) -> tuple[DiscoveryJobStatus, dict[str, Any], dict[str, Any]]:
        as_of_param = job.params.get("as_of")
        live = as_of_param is None
        prep: dict[str, Any] = {"enabled": False}
        if live and job.params.get("prepare_history", True):
            with session_scope(self._sessions) as session:
                _, config = self.qualification.rule_set(session)
            prep = await self.qualification.prepare_history(job.id, config)
        as_of = (
            self._clock.now() if as_of_param is None else datetime.fromisoformat(str(as_of_param))
        )
        summary = self.qualification.evaluate_all(job.id, as_of, live=live)
        summary["history_preparation"] = prep
        failed_prep = [
            j
            for j in prep.get("ingestion_jobs", [])
            if j.get("status") not in ("COMPLETED", None) or "error" in j
        ]
        status = (
            DiscoveryJobStatus.PARTIALLY_COMPLETED if failed_prep else DiscoveryJobStatus.COMPLETED
        )
        if failed_prep:
            summary["problem"] = f"{len(failed_prep)} history ingestion job(s) did not complete"
        return status, {"evaluated": summary["candidates"], **summary["outcomes"]}, summary

    def _source(
        self, session: Session, job: DiscoveryJobRecord, kind: DiscoveryJobKind
    ) -> DiscoveryJobRecord:
        source_id = job.params.get(_SOURCE_KEYS[kind])
        source = session.get(DiscoveryJobRecord, uuid.UUID(source_id)) if source_id else None
        if source is None:
            source = jobs.last_finished(session, kind, self.provider_id, successful_only=True)
        if source is None or source.kind is not kind:
            raise LookupError(f"no completed {kind.value.lower()} job to use")
        if source.status not in (
            DiscoveryJobStatus.COMPLETED,
            DiscoveryJobStatus.PARTIALLY_COMPLETED,
        ):
            raise LookupError(f"source job {source.id} is {source.status.value}")
        return source

    # ------------------------------------------------------------ scoring
    def _score(
        self, job: DiscoveryJobRecord
    ) -> tuple[DiscoveryJobStatus, dict[str, Any], dict[str, Any]]:
        with session_scope(self._sessions) as session:
            source = self._source(session, job, DiscoveryJobKind.QUALIFICATION)
            if not source.summary.get("live", True):
                raise ValueError("watchlist scoring uses live qualification jobs only")
            version, config = resolve_scoring(session, self.settings.scoring_version)
            _, rules = resolve_rule_set(session, self.settings.rule_set_code(self._provider.feed))
            evaluations = list(
                session.scalars(
                    select(QualificationEvaluationRecord).where(
                        QualificationEvaluationRecord.job_id == source.id,
                        QualificationEvaluationRecord.outcome == QualificationOutcome.QUALIFIED,
                    )
                )
            )
            ids = [e.asset_id for e in evaluations]
            values: dict[uuid.UUID, dict[str, float | None]] = {a: {} for a in ids}
            for row in session.scalars(
                select(AssetMetricValueRecord).where(
                    AssetMetricValueRecord.job_id == source.id,
                    AssetMetricValueRecord.asset_id.in_(ids),
                )
            ):
                values[row.asset_id][row.metric] = (
                    float(row.value)
                    if row.status is MetricStatus.OK and row.value is not None
                    else None
                )
            symbols = dict(
                session.execute(select(Asset.id, Asset.symbol).where(Asset.id.in_(ids))).all()
            )
            members = membership.current_members(session, UniverseLayer.ACTIVE_WATCHLIST)
            as_of = evaluations[0].as_of if evaluations else source.created_at
            evaluation_ids = {e.asset_id: e.id for e in evaluations}
            version_id = version.id
        inputs = [
            ScoreInput(
                asset_id=a,
                symbol=symbols.get(a, "?"),
                median_dollar_volume=values[a].get("median_dollar_volume"),
                relative_volume=values[a].get("relative_volume"),
                realized_volatility=values[a].get("realized_volatility"),
                return_20d=values[a].get("return_20d"),
                relative_strength_20d=values[a].get("relative_strength_20d"),
                median_spread_bps=values[a].get("median_spread_bps"),
                history_coverage=values[a].get("history_coverage"),
            )
            for a in ids
        ]
        scores = score_assets(
            inputs,
            config,
            min_history_coverage=float(rules.min_history_coverage),
            feed=self._provider.feed,
            current_members={a: True for a in members},
        )
        with session_scope(self._sessions) as session:
            for s in scores:
                session.add(
                    WatchlistScoreRecord(
                        job_id=job.id,
                        asset_id=s.asset_id,
                        scoring_version_id=version_id,
                        evaluation_id=evaluation_ids[s.asset_id],
                        as_of=as_of,
                        overall_score=s.overall,
                        rank=s.rank,
                        weight_coverage=s.weight_coverage,
                        factor_scores={f.factor: f.as_dict() for f in s.factors},
                        explanation=s.explanation,
                        selected=s.selected,
                    )
                )
        selected = sum(1 for s in scores if s.selected)
        summary = {
            "source_job_id": str(source.id),
            "scoring_version_id": str(version_id),
            "as_of": as_of.isoformat(),
            "qualified": len(ids),
            "ranked": sum(1 for s in scores if s.rank is not None),
            "selected": selected,
            "note": "analytical priority only; not a trading signal",
        }
        return DiscoveryJobStatus.COMPLETED, {"scored": len(scores), "selected": selected}, summary

    # ------------------------------------------------------------ membership
    def _update_membership(
        self, job: DiscoveryJobRecord
    ) -> tuple[DiscoveryJobStatus, dict[str, Any], dict[str, Any]]:
        layer = UniverseLayer(job.params.get("layer", UniverseLayer.QUALIFIED_UNIVERSE.value))
        source_kind = (
            DiscoveryJobKind.QUALIFICATION
            if layer is UniverseLayer.QUALIFIED_UNIVERSE
            else DiscoveryJobKind.WATCHLIST_SCORING
        )
        if layer is UniverseLayer.DISCOVERY_POOL:
            raise ValueError("discovery pool membership is maintained by directory discovery")
        with session_scope(self._sessions) as session:
            source = self._source(session, job, source_kind)
            source_id = source.id
            if source_kind is DiscoveryJobKind.QUALIFICATION and not source.summary.get(
                "live", True
            ):
                raise ValueError("membership is only changed from live qualification jobs")
        decisions = self._membership_decisions(layer, source_id)
        counts: Counter[str] = Counter()
        chunk = 500
        for offset in range(0, len(decisions), chunk):
            part = decisions[offset : offset + chunk]
            for attempt in range(3):
                try:
                    with session_scope(self._sessions) as session:
                        local = self._apply_decisions(session, job.id, layer, part)
                    counts.update(local)
                    break
                except IntegrityError:
                    # A concurrent writer changed one of these assets; re-read and retry.
                    counts["concurrent_retries"] += 1
                    if attempt == 2:
                        raise
        summary = {"layer": layer.value, "source_job_id": str(source_id), **counts}
        return DiscoveryJobStatus.COMPLETED, dict(counts), summary

    def _membership_decisions(
        self, layer: UniverseLayer, source_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        with session_scope(self._sessions) as session:
            if layer is UniverseLayer.QUALIFIED_UNIVERSE:
                rows = session.scalars(
                    select(QualificationEvaluationRecord).where(
                        QualificationEvaluationRecord.job_id == source_id
                    )
                )
                return [
                    {
                        "asset_id": e.asset_id,
                        "member": e.outcome is QualificationOutcome.QUALIFIED,
                        "effective_at": e.as_of,
                        "evaluation_id": e.id,
                        "score_id": None,
                        "reason_code": e.outcome.value,
                        "reason": _qualification_reason(e),
                    }
                    for e in rows
                ]
            scores = list(
                session.scalars(
                    select(WatchlistScoreRecord).where(WatchlistScoreRecord.job_id == source_id)
                )
            )
            scored = {s.asset_id for s in scores}
            decisions: list[dict[str, Any]] = [
                {
                    "asset_id": s.asset_id,
                    "member": s.selected,
                    "effective_at": s.as_of,
                    "evaluation_id": s.evaluation_id,
                    "score_id": s.id,
                    "reason_code": "SELECTED"
                    if s.selected
                    else ("NOT_RANKED" if s.rank is None else "RANKED_OUT"),
                    "reason": (
                        f"rank {s.rank}, score {s.overall_score}" if s.rank else "not ranked"
                    )
                    + ("; selected for analytical attention" if s.selected else "; not selected"),
                }
                for s in scores
            ]
            as_of = scores[0].as_of if scores else None
            for asset_id in membership.current_members(session, UniverseLayer.ACTIVE_WATCHLIST):
                if asset_id not in scored and as_of is not None:
                    decisions.append(
                        {
                            "asset_id": asset_id,
                            "member": False,
                            "effective_at": as_of,
                            "evaluation_id": None,
                            "score_id": None,
                            "reason_code": "NOT_QUALIFIED",
                            "reason": "no longer in the qualified universe",
                        }
                    )
            return decisions

    def _apply_decisions(
        self,
        session: Session,
        job_id: uuid.UUID,
        layer: UniverseLayer,
        decisions: list[dict[str, Any]],
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        ids = [d["asset_id"] for d in decisions]
        states = membership.current_states(session, layer, ids)
        pool = membership.current_states(session, UniverseLayer.DISCOVERY_POOL, ids)
        qualified = (
            membership.current_states(session, UniverseLayer.QUALIFIED_UNIVERSE, ids)
            if layer is UniverseLayer.ACTIVE_WATCHLIST
            else {}
        )
        now = self._clock.now()
        for d in decisions:
            state = states.get(d["asset_id"])
            member = d["member"]
            # Re-validate at write time: only pool members can qualify, only qualified
            # assets can be watched (discovery may have deactivated one meanwhile).
            if member and not (pool.get(d["asset_id"]) and pool[d["asset_id"]].member):
                counts["skipped_not_in_pool"] += 1
                continue
            if (
                member
                and layer is UniverseLayer.ACTIVE_WATCHLIST
                and not (qualified.get(d["asset_id"]) and qualified[d["asset_id"]].member)
            ):
                counts["skipped_not_qualified"] += 1
                continue
            if state is not None and d["effective_at"] < state.since:
                counts["skipped_stale_evidence"] += 1
                continue
            event = membership.transition(
                session,
                asset_id=d["asset_id"],
                layer=layer,
                action=MembershipAction.ADDED if member else MembershipAction.REMOVED,
                effective_at=d["effective_at"],
                recorded_at=max(now, d["effective_at"]),
                reason_code=d["reason_code"],
                reason=d["reason"],
                job_id=job_id,
                evaluation_id=d["evaluation_id"],
                score_id=d["score_id"],
                previous=state,
            )
            if event is None:
                counts["unchanged"] += 1
            else:
                counts["added" if member else "removed"] += 1
        return counts

    # ------------------------------------------------------------ scheduling
    def schedule_due(self) -> list[DiscoveryJobRecord]:
        """Create the periodic jobs whose interval has elapsed (idempotent)."""
        now = self._clock.now()
        created: list[DiscoveryJobRecord] = []
        with session_scope(self._sessions) as session:
            any_catalog = session.scalar(
                select(ProviderAssetRecord.id)
                .where(ProviderAssetRecord.provider == self.provider_id)
                .limit(1)
            )

            def last(kind: DiscoveryJobKind, mode: str | None = None) -> datetime | None:
                stmt = (
                    select(DiscoveryJobRecord.completed_at)
                    .where(
                        DiscoveryJobRecord.kind == kind,
                        DiscoveryJobRecord.provider == self.provider_id,
                        DiscoveryJobRecord.status.in_(
                            [DiscoveryJobStatus.COMPLETED, DiscoveryJobStatus.PARTIALLY_COMPLETED]
                        ),
                    )
                    .order_by(DiscoveryJobRecord.completed_at.desc())
                    .limit(1)
                )
                if mode is not None:
                    stmt = stmt.where(DiscoveryJobRecord.params["mode"].astext == mode)
                return session.scalar(stmt)

            plan: list[tuple[DiscoveryJobKind, dict[str, Any]]] = []
            full, incremental = (
                last(DiscoveryJobKind.DIRECTORY_DISCOVERY, "FULL"),
                last(DiscoveryJobKind.DIRECTORY_DISCOVERY, "INCREMENTAL"),
            )
            s = self.settings
            if (
                not any_catalog
                or full is None
                or now - full >= timedelta(hours=s.full_interval_hours)
            ):
                plan.append(
                    (DiscoveryJobKind.DIRECTORY_DISCOVERY, {"mode": "FULL", "pipeline": True})
                )
            elif now - max(full, incremental or full) >= timedelta(
                hours=s.incremental_interval_hours
            ):
                plan.append(
                    (
                        DiscoveryJobKind.DIRECTORY_DISCOVERY,
                        {"mode": "INCREMENTAL", "pipeline": True},
                    )
                )
            else:
                qualified = last(DiscoveryJobKind.QUALIFICATION)
                if qualified is None or now - qualified >= timedelta(
                    hours=s.qualification_interval_hours
                ):
                    plan.append(
                        (DiscoveryJobKind.QUALIFICATION, {"pipeline": True, "pipeline_step": 0})
                    )
            refreshed = last(DiscoveryJobKind.METADATA_REFRESH)
            if any_catalog and (
                refreshed is None or now - refreshed >= timedelta(hours=s.refresh_interval_hours)
            ):
                plan.append((DiscoveryJobKind.METADATA_REFRESH, {}))
        for kind, params in plan:
            result = self.request(
                kind, trigger=JobTrigger.SCHEDULE, requested_by="scheduler", params=params
            )
            if result.created:
                created.append(result.job)
        return created


def _qualification_reason(evaluation: QualificationEvaluationRecord) -> str:
    if evaluation.outcome is QualificationOutcome.QUALIFIED:
        return "all mandatory rules passed"
    failed = [
        r for r in evaluation.rule_results if r.get("mandatory") and r.get("status") == "FAIL"
    ]
    if failed:
        return "; ".join(f"{r['rule']}: {r['message']}" for r in failed)[:2000]
    missing = [
        r
        for r in evaluation.rule_results
        if r.get("mandatory") and r.get("status") == "UNAVAILABLE"
    ]
    return "insufficient data: " + "; ".join(f"{r['rule']}: {r['message']}" for r in missing)[:2000]


__all__ = ["PIPELINE", "AssetDiscoveryService", "default_owner"]
