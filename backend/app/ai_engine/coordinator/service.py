"""The decision coordinator service (Phase 5B): run, persist, replay.

A run:

1. registers the policy version (content-addressed, D-114);
2. LIVE only: asks the Phase 5A inference engine for point-in-time predictions of every
   APPROVED model (and SHADOW models, recorded as context only) for the active proposals;
3. fixes the decision time and claims the run: a partial unique index allows one
   non-failed run per (policy version, mode, basis, decision time), so a repeated trigger
   returns the existing run instead of deciding twice;
4. gathers the point-in-time inputs (one REPEATABLE READ snapshot), evaluates them
   (pure), and writes the inputs, decisions, components, evidence and conflicts and the
   run's outcome in one transaction. A failure marks the run FAILED (message redacted).

The coordinator never calls an LLM: it reads LLM analyses that were already stored and
available, so a slow or unavailable provider can never block or alter a decision.
:meth:`Coordinator.replay` re-evaluates a stored run from its ``decision_inputs`` rows and
the stored policy parameters alone and compares the content hashes.

Nothing here sizes, reserves, approves or submits anything.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.ai_engine.coordinator import gather as reads
from app.ai_engine.coordinator.bundle import InputBundle
from app.ai_engine.coordinator.evaluate import Evaluation, evaluate
from app.ai_engine.coordinator.policy import LOGIC_VERSION, DecisionPolicy
from app.ai_engine.enums import DecisionMode, DecisionRunStatus, InputKind
from app.asset_discovery.sessions import ExchangeCalendar
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.database.models import (
    AIDecisionRecord,
    DecisionConflictRecord,
    DecisionEvidenceRecord,
    DecisionInputRecord,
    DecisionPolicyRecord,
    DecisionPolicyVersionRecord,
    DecisionRunRecord,
    DecisionScoringComponentRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import sha256_json
from app.market_intelligence.enums import KnowledgeBasis
from app.ml_engine.enums import PredictionMode
from app.ml_engine.versioning import code_version

logger = get_logger(__name__)


class CoordinatorError(RuntimeError):
    """A run could not be performed (bad arguments, missing run)."""


def ensure_policy_version(
    session: Session, policy: DecisionPolicy, *, actor: str, reason: str
) -> DecisionPolicyVersionRecord:
    """The stored version with these exact parameters, created if new (serialized)."""
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"policy:{policy.code}", 0)))
    )
    identity = session.scalar(
        select(DecisionPolicyRecord).where(DecisionPolicyRecord.code == policy.code)
    )
    if identity is None:
        identity = DecisionPolicyRecord(
            id=uuid.uuid4(), code=policy.code, description=policy.description
        )
        session.add(identity)
        session.flush()
    sha = policy.sha256()
    existing = session.scalar(
        select(DecisionPolicyVersionRecord).where(
            DecisionPolicyVersionRecord.policy_id == identity.id,
            DecisionPolicyVersionRecord.parameters_sha256 == sha,
        )
    )
    if existing is not None:
        return existing
    newest = session.scalar(
        select(func.max(DecisionPolicyVersionRecord.version)).where(
            DecisionPolicyVersionRecord.policy_id == identity.id
        )
    )
    record = DecisionPolicyVersionRecord(
        id=uuid.uuid4(),
        policy_id=identity.id,
        version=(newest or 0) + 1,
        parameters=policy.parameters(),
        parameters_sha256=sha,
        logic_version=LOGIC_VERSION,
        reason=reason,
        created_by=actor,
    )
    session.add(record)
    session.flush()
    return record


@dataclass(frozen=True)
class RunResult:
    run_id: uuid.UUID
    status: DecisionRunStatus
    created: bool
    outcome: str | None
    decisions: int
    prioritized: int


def _store_inputs(
    session: Session, run_id: uuid.UUID, bundle: InputBundle
) -> dict[tuple[str, str], int]:
    records = []
    for item in bundle.items():
        payload = item.payload()
        records.append(
            DecisionInputRecord(
                run_id=run_id,
                kind=item.kind,
                ref=item.ref,
                asset_id=item.item_asset_id,
                as_of=item.item_as_of,
                available_at=item.item_available_at,
                payload=payload,
                payload_sha256=sha256_json(payload),
            )
        )
    session.add_all(records)
    session.flush()
    return {(r.kind.value, r.ref): r.id for r in records}


def _store_evaluation(
    session: Session,
    run_id: uuid.UUID,
    policy_version_id: uuid.UUID,
    result: Evaluation,
    inputs: dict[tuple[str, str], int],
) -> dict[tuple[str, str], uuid.UUID]:
    ids: dict[tuple[str, str], uuid.UUID] = {}
    for d in result.decisions:
        record = AIDecisionRecord(
            id=uuid.uuid4(),
            run_id=run_id,
            outcome=d.outcome,
            rank=d.rank,
            asset_id=d.asset_id,
            symbol=d.symbol,
            direction=d.direction,
            timeframes=d.timeframes,
            proposal_ids=d.proposal_ids,
            strategy_versions=d.strategy_versions,
            model_versions=d.model_versions,
            regime_snapshot=d.regime_snapshot,
            news_refs=d.news_refs,
            llm_refs=d.llm_refs,
            feature_timestamps=d.feature_timestamps,
            policy_version_id=policy_version_id,
            scores=d.scores,
            expected=d.expected,
            uncertainty=d.uncertainty,
            conflicts=d.conflicts,
            reasons=d.reasons,
            explanation=d.explanation,
            decision_at=d.decision_at,
            data_cutoff=d.data_cutoff,
            expires_at=d.expires_at,
            content_sha256=d.sha256(),
        )
        session.add(record)
        key = (str(d.asset_id), d.direction.value if d.direction else "")
        ids[key] = record.id
        for c in d.components:
            session.add(
                DecisionScoringComponentRecord(
                    decision_id=record.id,
                    name=c["name"],
                    kind=c["kind"],
                    status=c["status"],
                    raw_value=c["raw_value"],
                    score=c["score"],
                    weight=c["weight"],
                    contribution=c["contribution"],
                    reason=c["reason"],
                    inputs=c["inputs"],
                )
            )
        for e in d.evidence:
            session.add(
                DecisionEvidenceRecord(
                    decision_id=record.id,
                    input_id=None if e["ref"] is None else inputs.get((e["kind"], e["ref"])),
                    kind=e["kind"],
                    stance=e["stance"],
                    summary=e["summary"],
                    data=e["data"],
                )
            )
    session.flush()
    for conflict in result.conflicts:
        session.add(
            DecisionConflictRecord(
                run_id=run_id,
                type=conflict.type,
                asset_id=conflict.asset_id,
                decision_ids=[
                    ids[(str(a), d.value)] for a, d in conflict.keys if (str(a), d.value) in ids
                ],
                proposal_ids=conflict.proposal_ids,
                handling=conflict.handling,
                description=conflict.description,
                data=conflict.data,
            )
        )
    return ids


class Coordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        *,
        inference: Any | None = None,
        calendar: ExchangeCalendar | None = None,
        worker_id: str = "coordinator",
    ) -> None:
        self._sessions = session_factory
        self.clock = clock
        self.inference = inference
        self.calendar = calendar
        self.worker_id = worker_id

    # --- ML ---------------------------------------------------------------------------

    def _request_predictions(self, actor: str) -> int:
        """LIVE runs: point-in-time predictions of APPROVED (used) and SHADOW (context)
        models for the currently active proposals. Each is recorded by the ML engine."""
        if self.inference is None or self.calendar is None:
            return 0
        now = self.clock.now()
        with session_scope(self._sessions) as session:
            active = reads.proposals(session, now, now, KnowledgeBasis.RECORDED)
            approved = reads.models_in_status(session, now, "APPROVED")
            shadow = reads.models_in_status(session, now, "SHADOW")
            session.expunge_all()
        targets = sorted(
            {(p.asset_id, p.direction, p.timeframe, p.strategy_id) for p in active},
            key=lambda t: (str(t[0]), t[1].value, t[2], t[3]),
        )
        made = 0
        for models, mode in ((approved, PredictionMode.APPROVED), (shadow, PredictionMode.SHADOW)):
            for model in models:
                for asset_id, direction, timeframe, strategy_id in targets:
                    try:
                        self.inference.predict_at(
                            model.id,
                            asset_id=asset_id,
                            timeframe=timeframe,
                            direction=direction,
                            decision_at=self.clock.now(),
                            calendar=self.calendar,
                            strategy_id=strategy_id,
                            mode=mode,
                            actor=actor,
                        )
                        made += 1
                    except Exception as exc:  # recorded per prediction; never blocks a run
                        logger.warning(
                            "ai.prediction_failed",
                            model=str(model.id),
                            error_type=type(exc).__name__,
                        )
        return made

    # --- Runs -------------------------------------------------------------------------

    def run(
        self,
        policy: DecisionPolicy,
        *,
        mode: DecisionMode = DecisionMode.LIVE,
        decision_at: datetime | None = None,
        basis: KnowledgeBasis = KnowledgeBasis.RECORDED,
        trigger: str = "manual",
        actor: str = "coordinator",
        policy_reason: str = "registered by a decision run",
    ) -> RunResult:
        if mode is DecisionMode.LIVE:
            if decision_at is not None or basis is not KnowledgeBasis.RECORDED:
                raise CoordinatorError("LIVE runs decide now, on RECORDED information")
            self._request_predictions(actor)
        elif decision_at is None:
            raise CoordinatorError("HISTORICAL runs need a decision time")
        now = self.clock.now()
        at = now if decision_at is None else decision_at
        if at > now:
            raise CoordinatorError("the decision time is in the future")
        knowledge_at = at if basis is KnowledgeBasis.RECORDED else now
        with session_scope(self._sessions) as session:
            version = ensure_policy_version(session, policy, actor=actor, reason=policy_reason)
            version_id = version.id
        run_id = uuid.uuid4()
        try:
            with session_scope(self._sessions) as session:
                session.add(
                    DecisionRunRecord(
                        id=run_id,
                        policy_version_id=version_id,
                        mode=mode,
                        basis=basis,
                        trigger=trigger[:64],
                        decision_at=at,
                        knowledge_at=knowledge_at,
                        status=DecisionRunStatus.RUNNING,
                        code_version={**code_version(), "coordinator": LOGIC_VERSION},
                        worker_id=self.worker_id,
                        created_by=actor,
                        started_at=now,
                    )
                )
        except IntegrityError:
            with session_scope(self._sessions) as session:
                existing = session.scalar(
                    select(DecisionRunRecord).where(
                        DecisionRunRecord.policy_version_id == version_id,
                        DecisionRunRecord.mode == mode,
                        DecisionRunRecord.basis == basis,
                        DecisionRunRecord.decision_at == at,
                        DecisionRunRecord.status != DecisionRunStatus.FAILED,
                    )
                )
                if existing is None:  # pragma: no cover - the index guarantees one
                    raise
                return RunResult(
                    existing.id,
                    existing.status,
                    False,
                    None if existing.outcome is None else existing.outcome.value,
                    existing.candidates_count,
                    existing.prioritized_count,
                )
        try:
            with session_scope(self._sessions) as session:
                session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                bundle = reads.gather(
                    session,
                    decision_at=at,
                    knowledge_at=knowledge_at,
                    basis=basis,
                    mode=mode,
                    policy=policy,
                )
            result = evaluate(bundle, policy)
            with session_scope(self._sessions) as session:
                inputs = _store_inputs(session, run_id, bundle)
                _store_evaluation(session, run_id, version_id, result, inputs)
                prioritized = int(result.summary["prioritized"])
                session.execute(
                    update(DecisionRunRecord)
                    .where(
                        DecisionRunRecord.id == run_id,
                        DecisionRunRecord.status == DecisionRunStatus.RUNNING,
                    )
                    .values(
                        status=DecisionRunStatus.COMPLETED,
                        outcome=result.outcome,
                        proposals_considered=result.proposals_considered,
                        candidates_count=int(result.summary["candidates"]),
                        prioritized_count=prioritized,
                        inputs_sha256=bundle.sha256(),
                        result_sha256=result.result_sha256(),
                        summary=result.summary,
                        notes=list(bundle.notes),
                        finished_at=self.clock.now(),
                    )
                )
        except Exception as exc:
            message = redact_text(f"{type(exc).__name__}: {exc}")[:2000]
            with session_scope(self._sessions) as session:
                session.execute(
                    update(DecisionRunRecord)
                    .where(
                        DecisionRunRecord.id == run_id,
                        DecisionRunRecord.status == DecisionRunStatus.RUNNING,
                    )
                    .values(
                        status=DecisionRunStatus.FAILED,
                        error=message,
                        finished_at=self.clock.now(),
                    )
                )
            logger.warning(
                "ai.decision_run_failed", run_id=str(run_id), error_type=type(exc).__name__
            )
            raise
        logger.info(
            "ai.decision_run_completed",
            run_id=str(run_id),
            outcome=result.outcome.value,
            candidates=result.summary["candidates"],
            prioritized=prioritized,
        )
        return RunResult(
            run_id,
            DecisionRunStatus.COMPLETED,
            True,
            result.outcome.value,
            len(result.decisions),
            prioritized,
        )

    def replay(self, run_id: uuid.UUID) -> dict[str, Any]:
        """Re-evaluate a stored run from its stored inputs and policy parameters only."""
        with session_scope(self._sessions) as session:
            run = session.get(DecisionRunRecord, run_id)
            if run is None:
                raise CoordinatorError(f"decision run {run_id} does not exist")
            if run.status is not DecisionRunStatus.COMPLETED:
                return {"run_id": str(run_id), "replayable": False, "reason": run.status.value}
            version = session.get(DecisionPolicyVersionRecord, run.policy_version_id)
            assert version is not None  # noqa: S101  # foreign key
            rows = [
                (r.kind, r.payload, r.payload_sha256)
                for r in session.scalars(
                    select(DecisionInputRecord)
                    .where(DecisionInputRecord.run_id == run_id)
                    .order_by(DecisionInputRecord.id)
                )
            ]
            stored = {
                (str(d.asset_id), d.direction.value if d.direction else ""): d.content_sha256
                for d in session.scalars(
                    select(AIDecisionRecord).where(AIDecisionRecord.run_id == run_id)
                )
            }
            info = (run.decision_at, run.knowledge_at, run.basis, run.mode, list(run.notes))
            inputs_sha, result_sha = run.inputs_sha256, run.result_sha256
            params, logic = version.parameters, version.logic_version
        tampered = [kind.value for kind, payload, sha in rows if sha256_json(payload) != sha]
        policy = DecisionPolicy.model_validate(params)
        bundle = InputBundle.from_rows(
            decision_at=info[0],
            knowledge_at=info[1],
            basis=info[2],
            mode=info[3],
            rows=[(InputKind(kind), payload) for kind, payload, _ in rows],
            notes=info[4],
        )
        result = evaluate(bundle, policy)
        recomputed = {
            (str(d.asset_id), d.direction.value if d.direction else ""): d.sha256()
            for d in result.decisions
        }
        differing = sorted(
            f"{k[0]}:{k[1]}"
            for k in set(stored) | set(recomputed)
            if stored.get(k) != recomputed.get(k)
        )
        return {
            "run_id": str(run_id),
            "replayable": True,
            "logic_version_stored": logic,
            "logic_version_current": LOGIC_VERSION,
            "inputs_identical": bundle.sha256() == inputs_sha and not tampered,
            "result_identical": result.result_sha256() == result_sha and not differing,
            "decisions_compared": len(recomputed),
            "differing_decisions": differing,
            "tampered_inputs": tampered,
            "note": "replayed from stored inputs and stored LLM outputs; no provider was "
            "called and no data was re-read",
        }


__all__ = ["Coordinator", "CoordinatorError", "RunResult", "ensure_policy_version"]
