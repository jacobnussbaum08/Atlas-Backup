"""The strategy evaluation engine (Phase 4A, spec 4 "event processing" and 11).

Flow for one :class:`EvaluationRequest` (one strategy version, asset, timeframe, trigger):

1. **Claim** (own transaction, advisory lock on the idempotency key). The key is the hash
   of (version, asset, timeframe, mode, basis, trigger, trigger reference): the same
   update never produces two results. An existing completed/skipped evaluation makes the
   request a duplicate; a RUNNING one with a live lease is in progress elsewhere; a
   RUNNING one whose lease expired belonged to a stopped worker and is failed
   (INTERRUPTED) first. After ``max_attempts`` failures the update is given up.
2. **Inputs**: point-in-time features (``app.strategies.inputs``), stored as a
   content-addressed snapshot.
3. **Strategy**: called only if every required feature is VALID (else SKIPPED). It gets
   an :class:`~app.strategies.base.EvaluationContext` and nothing else.
4. **Validation** of the drafts (``app.strategies.proposals``).
5. **Finalize** (one transaction, row lock, per-asset advisory lock): only if the
   evaluation is still RUNNING and owned by this worker (a timed-out or reclaimed
   evaluation commits nothing), store the proposals (one per strategy, asset, timeframe,
   direction, mode, basis and evaluation event: a repeat is DUPLICATE), supersede older
   active proposals of the same strategy/asset/direction (LIVE only), record conflicts
   with overlapping opposite-direction proposals and set the terminal status and signal
   state. Proposals and results are never updated afterwards.

Phase 4B additions: before any input is computed, the asset's point-in-time eligibility
(``app.strategies.eligibility``) is loaded; an unsupported or untradable asset, or one no
configured direction is eligible for, is SKIPPED as INELIGIBLE_ASSET. Every evaluation
records a :class:`~app.strategies.enums.SignalState`, the checks the strategy evaluated
and, without a proposal, the reason.

Any failure is recorded with its stage and the evaluation becomes FAILED; nothing escapes
to unrelated evaluations. :meth:`StrategyEngine.evaluate_many` runs requests concurrently
(bounded by ``max_concurrency``, each limited to ``evaluation_timeout_seconds``).

Nothing here sizes a position, approves a trade or talks to a broker.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.sessions import CALENDARS, ExchangeCalendar
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.database.models import (
    Asset,
    StrategyEvaluationErrorRecord,
    StrategyEvaluationRecord,
    StrategyEvaluationSnapshotRecord,
    StrategyProposalConflictRecord,
    StrategyProposalEventRecord,
    StrategyProposalRecord,
    StrategyVersionRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import FeatureStatus, ResolvedFeature, sha256_json
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies import catalog
from app.strategies.base import (
    AssetView,
    Eligibility,
    EvaluationContext,
    Strategy,
    StrategyOutcome,
    StrategyParameters,
)
from app.strategies.config import StrategySettings
from app.strategies.eligibility import load_eligibility
from app.strategies.enums import (
    ConflictKind,
    ErrorStage,
    EvaluationMode,
    EvaluationStatus,
    ProposalEventKind,
    SignalState,
    TriggerKind,
)
from app.strategies.inputs import BarSeries, FeatureInputs, compute_features, store_snapshot
from app.strategies.periods import expires_at
from app.strategies.proposals import ProposalSpec, ProposalValidationError, validate_drafts
from app.strategies.registry import StrategyRegistry

logger = get_logger(__name__)

CALENDAR = "XNYS"  # every supported US venue follows the NYSE schedule (D-059)
MAX_ERROR_MESSAGE = 2000
MAX_CHECKS = 64

# The order in which a blocked evaluation's feature problems name its state.
_DATA_STATES = (
    (FeatureStatus.INSUFFICIENT_DATA, SignalState.INSUFFICIENT_DATA),
    (FeatureStatus.MISSING_INPUTS, SignalState.MISSING_DATA),
    (FeatureStatus.UNAVAILABLE, SignalState.UNAVAILABLE_DATA),
    (FeatureStatus.UNDEFINED, SignalState.INVALID_DATA),
)


def data_state(statuses: Iterable[FeatureStatus]) -> SignalState:
    """The signal state of an evaluation blocked by these required-feature statuses."""
    found = set(statuses)
    for status, state in _DATA_STATES:
        if status in found:
            return state
    return SignalState.INVALID_DATA


def ineligibility(strategy: Strategy, params: StrategyParameters, asset: AssetView) -> str | None:
    """Why the asset cannot be evaluated at all, or None (Phase 4B)."""
    if asset.asset_class not in strategy.supported_asset_classes:
        return f"{asset.asset_class.value} is not supported by {strategy.strategy_id}"
    if asset.eligibility is None:
        return None
    blocked = [asset.eligibility.direction_reason(d) for d in params.directions]
    if all(reason is not None for reason in blocked):
        return "; ".join(dict.fromkeys(r for r in blocked if r is not None))
    return None


class EvaluationStageError(Exception):
    """A failure in one stage of :func:`decide` (the strategy, or proposal validation)."""

    def __init__(self, stage: ErrorStage, error: BaseException) -> None:
        super().__init__(str(error))
        self.stage, self.error = stage, error


@dataclass(frozen=True)
class Decision:
    """What one evaluation decided from its point-in-time inputs (nothing persisted)."""

    status: EvaluationStatus
    state: SignalState
    specs: list[ProposalSpec]
    skip_reasons: list[dict[str, Any]]
    notes: list[str]
    reason: str | None
    checks: list[dict[str, Any]]


def decide(
    strategy: Strategy,
    params: StrategyParameters,
    resolved: Sequence[ResolvedFeature],
    asset: AssetView,
    inputs: FeatureInputs,
    *,
    timeframe: str,
    mode: EvaluationMode,
    basis: KnowledgeBasis,
    data_cutoff: datetime,
    trigger: TriggerKind,
    trigger_ref: str,
    registry: StrategyRegistry,
    calendar: ExchangeCalendar,
    snapshot_ids: Sequence[uuid.UUID | None],
) -> Decision:
    """The pure part of an evaluation, shared by the live engine and the Phase 8A backtester:
    required features must be VALID, the strategy is called with an
    :class:`EvaluationContext` only, and its drafts are validated into proposals.

    Raises :class:`EvaluationStageError` (STRATEGY or VALIDATION) when a stage fails."""
    invalid = [
        {"feature": f.key, "status": v.status.value, "reason": v.reason}
        for f in resolved
        if not f.optional and not (v := inputs.values[f.key]).valid
    ]
    if invalid:
        state = data_state(
            inputs.values[f.key].status
            for f in resolved
            if not f.optional and not inputs.values[f.key].valid
        )
        return Decision(
            EvaluationStatus.SKIPPED,
            state,
            [],
            invalid,
            [],
            "; ".join(f"{i['feature']}: {i['status']}" for i in invalid)[:2000],
            [],
        )
    ctx = EvaluationContext(
        asset=asset,
        timeframe=timeframe,
        mode=mode,
        basis=basis,
        data_cutoff=data_cutoff,
        features=inputs.values,
        trigger=trigger,
        registry=registry.features,
    )
    try:
        outcome = strategy.evaluate(ctx, params)
        if not isinstance(outcome, StrategyOutcome):
            raise TypeError("evaluate() must return a StrategyOutcome")
    except Exception as exc:
        raise EvaluationStageError(ErrorStage.STRATEGY, exc) from exc
    checks = [c.to_json() for c in outcome.checks][:MAX_CHECKS]
    if outcome.invalid_data is not None:
        return Decision(
            EvaluationStatus.SKIPPED,
            SignalState.INVALID_DATA,
            [],
            [],
            list(outcome.notes),
            outcome.invalid_data,
            checks,
        )
    try:
        expiry = expires_at(calendar, timeframe, data_cutoff, params.signal_ttl_periods)
        specs, notes = validate_drafts(
            outcome.proposals,
            ctx,
            params,
            expires_at=expiry,
            snapshot_ids=[s for s in snapshot_ids if s is not None],
            evaluation_event=f"{trigger.value}:{trigger_ref}",
        )
    except (ProposalValidationError, ValueError) as exc:
        raise EvaluationStageError(ErrorStage.VALIDATION, exc) from exc
    return Decision(
        EvaluationStatus.PROPOSED if specs else EvaluationStatus.NO_PROPOSAL,
        SignalState.SIGNAL if specs else SignalState.NO_SIGNAL,
        specs,
        [],
        [*outcome.notes, *notes],
        None if specs else (outcome.reason or "; ".join(notes) or None),
        checks,
    )


@dataclass(frozen=True)
class MarketUpdate:
    """A validated update that may make strategies re-evaluate an asset.

    ``timeframe`` is None for updates that concern every timeframe (news)."""

    asset_id: uuid.UUID
    trigger: TriggerKind
    ref: str  # identifies the update: the completed period, or the news event
    timeframe: str | None = None


@dataclass(frozen=True)
class EvaluationRequest:
    strategy_id: str
    strategy_version_id: uuid.UUID
    asset_id: uuid.UUID
    timeframe: str
    trigger: TriggerKind
    trigger_ref: str
    mode: EvaluationMode = EvaluationMode.LIVE
    basis: KnowledgeBasis = KnowledgeBasis.RECORDED
    # HISTORICAL: the past decision time. LIVE: None (the cutoff is the claim time).
    data_cutoff: datetime | None = None

    @property
    def idempotency_key(self) -> str:
        return sha256_json(
            [
                str(self.strategy_version_id),
                str(self.asset_id),
                self.timeframe,
                self.mode.value,
                self.basis.value,
                self.trigger.value,
                self.trigger_ref,
            ]
        )


@dataclass(frozen=True)
class Claim:
    evaluation_id: uuid.UUID
    request: EvaluationRequest
    attempt: int
    data_cutoff: datetime
    known_at: datetime


@dataclass
class EvaluationResult:
    request: EvaluationRequest
    outcome: str  # EVALUATED, DUPLICATE, IN_PROGRESS, GAVE_UP, DISCARDED
    evaluation_id: uuid.UUID | None = None
    status: EvaluationStatus | None = None
    proposal_ids: list[uuid.UUID] = field(default_factory=list)
    error: str | None = None
    signal_state: SignalState | None = None


class _Failure(Exception):
    def __init__(self, stage: ErrorStage, error: BaseException, snapshot: uuid.UUID | None):
        super().__init__(str(error))
        self.stage, self.error, self.snapshot = stage, error, snapshot


class StrategyEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        strategies: StrategyRegistry,
        settings: StrategySettings,
        clock: Clock,
        *,
        provider: str,
        feed: str,
        worker_id: str | None = None,
    ) -> None:
        self._sessions = session_factory
        self.strategies = strategies
        self.settings = settings
        self.clock = clock
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.calendar = CALENDARS[CALENDAR]
        self.series = BarSeries(
            provider=provider,
            feed=feed,
            adjustment=settings.price_adjustment,
            calendar=self.calendar,
            settle=timedelta(seconds=settings.settle_seconds),
        )

    # --- Planning ---------------------------------------------------------------------

    def active_versions(
        self, session: Session
    ) -> list[tuple[Strategy, StrategyVersionRecord, StrategyParameters]]:
        """Implemented, enabled strategies whose current version matches the code."""
        active = []
        for strategy in self.strategies:
            if not strategy.implemented or not catalog.is_enabled(session, strategy.strategy_id):
                continue
            version = catalog.latest_version(session, strategy.strategy_id)
            if version is None or version.logic_version != strategy.logic_version:
                logger.warning("strategy.version_mismatch", strategy=strategy.strategy_id)
                continue
            params = strategy.parse_parameters(catalog.configuration(session, version).parameters)
            active.append((strategy, version, params))
        return active

    def plan(self, updates: Iterable[MarketUpdate]) -> list[EvaluationRequest]:
        updates = list(updates)
        if not updates:
            return []
        with session_scope(self._sessions) as session:
            active = self.active_versions(session)
            if not active:
                return []
            ids = list({u.asset_id for u in updates})
            rows = session.execute(select(Asset.id, Asset.asset_class).where(Asset.id.in_(ids)))
            classes = {row.id: row.asset_class for row in rows}
        requests: list[EvaluationRequest] = []
        for update_ in updates:
            asset_class = classes.get(update_.asset_id)
            for strategy, version, params in active:
                if update_.trigger not in strategy.schedule.triggers:
                    continue
                if asset_class not in strategy.supported_asset_classes:
                    continue
                for timeframe in params.timeframes:
                    if update_.timeframe is not None and update_.timeframe != timeframe:
                        continue
                    requests.append(
                        EvaluationRequest(
                            strategy_id=strategy.strategy_id,
                            strategy_version_id=version.id,
                            asset_id=update_.asset_id,
                            timeframe=timeframe,
                            trigger=update_.trigger,
                            trigger_ref=update_.ref,
                        )
                    )
        return requests

    # --- Claiming ---------------------------------------------------------------------

    def claim(self, request: EvaluationRequest) -> Claim | EvaluationResult:
        now = self.clock.now()
        if request.mode is EvaluationMode.LIVE:
            if request.data_cutoff is not None or request.basis is not KnowledgeBasis.RECORDED:
                raise ValueError("LIVE evaluations use the claim time and RECORDED inputs")
            cutoff = now
        else:
            if request.data_cutoff is None or request.data_cutoff > now:
                raise ValueError("HISTORICAL evaluations need a past data cutoff")
            cutoff = request.data_cutoff
        known_at = cutoff if request.basis is KnowledgeBasis.RECORDED else now
        key = request.idempotency_key
        e = StrategyEvaluationRecord
        with session_scope(self._sessions) as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"evaluation:{key}"}
            )
            live = session.scalar(
                select(e).where(e.idempotency_key == key, e.status != EvaluationStatus.FAILED)
            )
            if live is not None:
                if live.status is not EvaluationStatus.RUNNING:
                    return EvaluationResult(request, "DUPLICATE", live.id, live.status)
                if live.lease_expires_at > now:
                    return EvaluationResult(request, "IN_PROGRESS", live.id, live.status)
                self._mark_failed(
                    session,
                    live,
                    ErrorStage.INTERRUPTED,
                    "LeaseExpired",
                    f"lease expired; worker {live.worker_id} stopped before finishing",
                    now,
                )
            failures = session.scalar(
                select(func.count()).where(
                    e.idempotency_key == key, e.status == EvaluationStatus.FAILED
                )
            )
            attempts = int(failures or 0)
            if attempts >= self.settings.max_attempts:
                return EvaluationResult(request, "GAVE_UP", None, EvaluationStatus.FAILED)
            record = e(
                id=uuid.uuid4(),
                strategy_id=request.strategy_id,
                strategy_version_id=request.strategy_version_id,
                asset_id=request.asset_id,
                timeframe=request.timeframe,
                mode=request.mode,
                basis=request.basis,
                trigger=request.trigger,
                trigger_ref=request.trigger_ref,
                idempotency_key=key,
                attempt=attempts + 1,
                status=EvaluationStatus.RUNNING,
                data_cutoff=cutoff,
                known_at=known_at,
                worker_id=self.worker_id,
                started_at=now,
                lease_expires_at=now + timedelta(seconds=self.settings.lease_seconds),
            )
            session.add(record)
            session.flush()
            return Claim(record.id, request, record.attempt, cutoff, known_at)

    # --- Evaluation -------------------------------------------------------------------

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        """Claim and run one request synchronously (CLI, tests)."""
        claimed = self.claim(request)
        if isinstance(claimed, EvaluationResult):
            return claimed
        return self.run(claimed)

    def run(self, claim: Claim) -> EvaluationResult:
        request = claim.request
        snapshot: uuid.UUID | None = None
        try:
            strategy, params, resolved, asset = self._prepare(claim)
            if (ineligible := self._ineligible(strategy, params, asset)) is not None:
                return self._finalize(
                    claim,
                    EvaluationStatus.SKIPPED,
                    [],
                    None,
                    [],
                    [],
                    state=SignalState.INELIGIBLE_ASSET,
                    reason=ineligible,
                    eligibility=asset.eligibility,
                )
            try:
                inputs = self._inputs(claim, resolved)
                snapshot = self._store_snapshot(inputs)
            except Exception as exc:
                raise _Failure(ErrorStage.INPUTS, exc, snapshot) from exc
            try:
                decision = decide(
                    strategy,
                    params,
                    resolved,
                    asset,
                    inputs,
                    timeframe=request.timeframe,
                    mode=request.mode,
                    basis=request.basis,
                    data_cutoff=claim.data_cutoff,
                    trigger=request.trigger,
                    trigger_ref=request.trigger_ref,
                    registry=self.strategies,
                    calendar=self.calendar,
                    snapshot_ids=[snapshot],
                )
            except EvaluationStageError as exc:
                raise _Failure(exc.stage, exc.error, snapshot) from exc.error
            return self._finalize(
                claim,
                decision.status,
                decision.specs,
                snapshot,
                decision.skip_reasons,
                decision.notes,
                state=decision.state,
                reason=decision.reason,
                checks=decision.checks,
                eligibility=asset.eligibility,
            )
        except _Failure as failure:
            return self._fail(claim, failure.stage, failure.error, failure.snapshot)
        except Exception as exc:  # preparation or persistence
            return self._fail(claim, ErrorStage.PERSISTENCE, exc, snapshot)

    async def evaluate_many(self, requests: Sequence[EvaluationRequest]) -> list[EvaluationResult]:
        """Evaluate concurrently; one failure or timeout never affects the others."""
        limit = asyncio.Semaphore(self.settings.max_concurrency)
        timeout = self.settings.evaluation_timeout_seconds

        async def one(request: EvaluationRequest) -> EvaluationResult:
            async with limit:
                try:
                    claimed = await asyncio.to_thread(self.claim, request)
                except Exception as exc:
                    logger.exception("strategy.claim_failed", strategy=request.strategy_id)
                    return EvaluationResult(request, "DISCARDED", error=type(exc).__name__)
                if isinstance(claimed, EvaluationResult):
                    return claimed
                try:
                    return await asyncio.wait_for(asyncio.to_thread(self.run, claimed), timeout)
                except TimeoutError:
                    error = TimeoutError(f"evaluation exceeded {timeout:g} s")
                    return await asyncio.to_thread(
                        self._fail, claimed, ErrorStage.TIMEOUT, error, None
                    )

        return list(await asyncio.gather(*(one(r) for r in requests)))

    # --- Helpers ----------------------------------------------------------------------

    def _prepare(
        self, claim: Claim
    ) -> tuple[Strategy, StrategyParameters, list[ResolvedFeature], AssetView]:
        request = claim.request
        strategy = self.strategies.get(request.strategy_id)
        with session_scope(self._sessions) as session:
            version = session.get(StrategyVersionRecord, request.strategy_version_id)
            if version is None or version.strategy_id != strategy.strategy_id:
                raise ValueError("unknown strategy version")
            if version.logic_version != strategy.logic_version:
                raise ValueError(
                    f"version {version.version} has logic {version.logic_version}, the code "
                    f"has {strategy.logic_version}"
                )
            config = catalog.configuration(session, version)
            asset = session.get(Asset, request.asset_id)
            if asset is None:
                raise ValueError("unknown asset")
            eligibility = load_eligibility(
                session, asset.id, provider=self.series.provider, known_at=claim.known_at
            )
            view = AssetView(
                asset.id, asset.symbol, asset.asset_class, asset.exchange_code, eligibility
            )
            raw = dict(config.parameters)
        params = strategy.parse_parameters(raw)
        if request.timeframe not in params.timeframes:
            raise ValueError(f"{request.timeframe} is not configured for this version")
        resolved = self.strategies.features.resolve_all(
            strategy.required_features(params, request.timeframe)
        )
        return strategy, params, resolved, view

    def _inputs(self, claim: Claim, resolved: Sequence[ResolvedFeature]) -> FeatureInputs:
        with session_scope(self._sessions) as session:
            return compute_features(
                session,
                resolved,
                asset_id=claim.request.asset_id,
                timeframe=claim.request.timeframe,
                data_cutoff=claim.data_cutoff,
                known_at=claim.known_at,
                basis=claim.request.basis,
                series=self.series,
            )

    def _store_snapshot(self, inputs: FeatureInputs) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            return store_snapshot(session, inputs)

    def _owned(self, session: Session, claim: Claim) -> StrategyEvaluationRecord | None:
        record = session.get(StrategyEvaluationRecord, claim.evaluation_id, with_for_update=True)
        if (
            record is None
            or record.status is not EvaluationStatus.RUNNING
            or record.worker_id != self.worker_id
        ):
            return None
        return record

    @staticmethod
    def _ineligible(strategy: Strategy, params: StrategyParameters, asset: AssetView) -> str | None:
        """Why the asset cannot be evaluated at all, or None (Phase 4B)."""
        return ineligibility(strategy, params, asset)

    def _finalize(
        self,
        claim: Claim,
        status: EvaluationStatus,
        specs: Sequence[ProposalSpec],
        snapshot: uuid.UUID | None,
        skip_reasons: list[dict[str, Any]],
        notes: Sequence[str],
        *,
        state: SignalState,
        reason: str | None = None,
        checks: Sequence[dict[str, Any]] = (),
        eligibility: Eligibility | None = None,
    ) -> EvaluationResult:
        request = claim.request
        now = self.clock.now()
        notes = list(notes)
        with session_scope(self._sessions) as session:
            # Serialises proposal writes per asset, so duplicate and conflict checks see
            # every proposal committed before this one.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"strategy_proposals:{request.asset_id}"},
            )
            record = self._owned(session, claim)
            if record is None:
                return EvaluationResult(request, "DISCARDED", claim.evaluation_id)
            if snapshot is not None:
                session.add(
                    StrategyEvaluationSnapshotRecord(evaluation_id=record.id, snapshot_id=snapshot)
                )
            recorded: list[ProposalSpec] = []
            for spec in specs:
                duplicate = self._duplicate(session, record, spec)
                if duplicate is not None:
                    notes.append(
                        f"{spec.direction} not recorded: proposal {duplicate} already exists "
                        f"for event {spec.evaluation_event}"
                    )
                    continue
                session.add(self._proposal(record, spec))
                recorded.append(spec)
            session.flush()
            if request.mode is EvaluationMode.LIVE:
                for spec in recorded:
                    self._supersede(session, record, spec, now)
            conflicts = sum(self._record_conflicts(session, record, s, now) for s in recorded)
            if specs and not recorded:
                status, state = EvaluationStatus.NO_PROPOSAL, SignalState.DUPLICATE
                reason = "the same proposal was already recorded for this evaluation event"
            record.status = status
            record.signal_state = state
            record.signal_reason = None if reason is None else redact_text(reason)[:2000]
            record.checks = list(checks)
            record.eligibility = None if eligibility is None else eligibility.to_json()
            record.finished_at = now
            record.proposals_count = len(recorded)
            record.skip_reasons = skip_reasons
            record.notes = [str(n)[:500] for n in notes][:50]
        logger.info(
            "strategy.evaluated",
            strategy=request.strategy_id,
            status=status.value,
            signal_state=state.value,
            proposals=len(recorded),
            conflicts=conflicts,
        )
        return EvaluationResult(
            request,
            "EVALUATED",
            claim.evaluation_id,
            status,
            [s.id for s in recorded],
            signal_state=state,
        )

    @staticmethod
    def _duplicate(
        session: Session, record: StrategyEvaluationRecord, spec: ProposalSpec
    ) -> uuid.UUID | None:
        p = StrategyProposalRecord
        return session.scalar(
            select(p.id).where(
                p.strategy_id == record.strategy_id,
                p.asset_id == record.asset_id,
                p.timeframe == spec.timeframe,
                p.direction == spec.direction,
                p.mode == record.mode,
                p.basis == record.basis,
                p.evaluation_event == spec.evaluation_event,
            )
        )

    @staticmethod
    def _record_conflicts(
        session: Session, record: StrategyEvaluationRecord, spec: ProposalSpec, now: datetime
    ) -> int:
        """Record every proposal for the same asset, mode and basis in the opposite
        direction whose active period overlaps this one's. Nothing is resolved: the
        Phase 5 coordinator decides between competing proposals."""
        p, ev = StrategyProposalRecord, StrategyProposalEventRecord
        superseded = exists().where(
            ev.proposal_id == p.id,
            ev.kind == ProposalEventKind.SUPERSEDED,
            ev.occurred_at <= spec.signal_at,
        )
        others = session.execute(
            select(p.id, p.signal_at, p.expires_at).where(
                p.asset_id == record.asset_id,
                p.direction != spec.direction,
                p.mode == record.mode,
                p.basis == record.basis,
                p.id != spec.id,
                p.signal_at < spec.expires_at,
                p.expires_at > spec.signal_at,
                ~superseded,
            )
        ).all()
        if not others:
            return 0
        rows = []
        for other in others:
            first, second = sorted((spec.id, other.id))
            rows.append(
                {
                    "proposal_a_id": first,
                    "proposal_b_id": second,
                    "asset_id": record.asset_id,
                    "kind": ConflictKind.OPPOSING_DIRECTION,
                    "mode": record.mode,
                    "basis": record.basis,
                    "overlap_start": max(spec.signal_at, other.signal_at),
                    "overlap_end": min(spec.expires_at, other.expires_at),
                    "detected_by": spec.id,
                    "detected_at": now,
                }
            )
        c = StrategyProposalConflictRecord
        inserted = session.execute(
            pg_insert(c)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["proposal_a_id", "proposal_b_id"])
            .returning(c.id)
        )
        return len(inserted.all())

    @staticmethod
    def _proposal(record: StrategyEvaluationRecord, spec: ProposalSpec) -> StrategyProposalRecord:
        return StrategyProposalRecord(
            id=spec.id,
            evaluation_id=record.id,
            strategy_id=record.strategy_id,
            strategy_version_id=record.strategy_version_id,
            asset_id=record.asset_id,
            market=spec.market,
            direction=spec.direction,
            timeframe=spec.timeframe,
            mode=record.mode,
            basis=record.basis,
            signal_at=spec.signal_at,
            data_cutoff=spec.data_cutoff,
            feature_snapshot_ids=spec.feature_snapshot_ids,
            reference_entry=spec.reference_entry,
            invalidation_level=spec.invalidation_level,
            target_reference=spec.target_reference,
            levels=spec.levels,
            evaluation_event=spec.evaluation_event,
            holding_horizon_periods=spec.holding_horizon_periods,
            signal_strength=spec.signal_strength,
            strength_kind=spec.strength_kind,
            evidence=spec.evidence,
            reasoning=spec.reasoning,
            expires_at=spec.expires_at,
        )

    @staticmethod
    def _supersede(
        session: Session, record: StrategyEvaluationRecord, spec: ProposalSpec, now: datetime
    ) -> None:
        p, ev = StrategyProposalRecord, StrategyProposalEventRecord
        older = session.scalars(
            select(p.id).where(
                p.strategy_id == record.strategy_id,
                p.asset_id == record.asset_id,
                p.timeframe == spec.timeframe,
                p.direction == spec.direction,
                p.mode == EvaluationMode.LIVE,
                p.id != spec.id,
                p.signal_at <= spec.signal_at,
                p.expires_at > spec.signal_at,
                ~exists().where(ev.proposal_id == p.id),
            )
        ).all()
        for proposal_id in older:
            session.execute(
                insert(ev).values(
                    proposal_id=proposal_id,
                    kind=ProposalEventKind.SUPERSEDED,
                    occurred_at=spec.signal_at,
                    recorded_at=now,
                    superseded_by=spec.id,
                )
            )

    @staticmethod
    def _mark_failed(
        session: Session,
        record: StrategyEvaluationRecord,
        stage: ErrorStage,
        error_type: str,
        message: str,
        now: datetime,
    ) -> None:
        record.status = EvaluationStatus.FAILED
        record.signal_state = SignalState.FAILED
        record.signal_reason = f"{stage.value}: {error_type}"
        record.finished_at = now
        session.add(
            StrategyEvaluationErrorRecord(
                evaluation_id=record.id,
                strategy_id=record.strategy_id,
                stage=stage,
                error_type=error_type[:128],
                message=redact_text(message)[:MAX_ERROR_MESSAGE],
                occurred_at=now,
            )
        )
        session.flush()

    def _fail(
        self,
        claim: Claim,
        stage: ErrorStage,
        error: BaseException,
        snapshot: uuid.UUID | None,
    ) -> EvaluationResult:
        request = claim.request
        message = str(error) or type(error).__name__
        logger.warning(
            "strategy.evaluation_failed",
            strategy=request.strategy_id,
            stage=stage.value,
            error_type=type(error).__name__,
        )
        try:
            with session_scope(self._sessions) as session:
                record = self._owned(session, claim)
                if record is None:
                    return EvaluationResult(request, "DISCARDED", claim.evaluation_id)
                if snapshot is not None:
                    session.add(
                        StrategyEvaluationSnapshotRecord(
                            evaluation_id=record.id, snapshot_id=snapshot
                        )
                    )
                self._mark_failed(
                    session, record, stage, type(error).__name__, message, self.clock.now()
                )
        except Exception:
            # The lease will expire and the next claim records the interruption.
            logger.exception("strategy.failure_not_recorded", strategy=request.strategy_id)
        return EvaluationResult(
            request,
            "EVALUATED",
            claim.evaluation_id,
            EvaluationStatus.FAILED,
            error=redact_text(message)[:MAX_ERROR_MESSAGE],
            signal_state=SignalState.FAILED,
        )

    # --- Expiry -----------------------------------------------------------------------

    def expire_due(self) -> int:
        """Record EXPIRED for proposals whose expiry passed and that have no lifecycle
        event yet (idempotent). The status itself is derived from ``expires_at``; the
        event records when Atlas noticed."""
        now = self.clock.now()
        p, ev = StrategyProposalRecord, StrategyProposalEventRecord
        with session_scope(self._sessions) as session:
            due = session.execute(
                select(p.id, p.expires_at).where(
                    p.expires_at <= now, ~exists().where(ev.proposal_id == p.id)
                )
            ).all()
            if not due:
                return 0
            result = session.execute(
                pg_insert(ev)
                .values(
                    [
                        {
                            "proposal_id": row.id,
                            "kind": ProposalEventKind.EXPIRED,
                            "occurred_at": row.expires_at,
                            "recorded_at": now,
                        }
                        for row in due
                    ]
                )
                .on_conflict_do_nothing(index_elements=["proposal_id", "kind"])
                .returning(ev.id)
            )
            count = len(result.all())
        if count:
            logger.info("strategy.proposals_expired", count=count)
        return count


def worker_identity() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


__all__ = [
    "Claim",
    "Decision",
    "EvaluationRequest",
    "EvaluationResult",
    "EvaluationStageError",
    "MarketUpdate",
    "StrategyEngine",
    "decide",
    "ineligibility",
    "worker_identity",
]
