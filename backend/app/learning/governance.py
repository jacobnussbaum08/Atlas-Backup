"""Champion / challenger lifecycle for strategies and models (Phase 8B, spec 19.2).

**Champion:** the version currently approved to act. For a strategy, Atlas trades the latest
stored strategy version, so the champion is that version; the governed champion is the
candidate in APPROVED state (an incumbent never validated under 8B is shown as such). For a
model, the champion is the Phase 5A version in APPROVED status.

**Challenger:** a candidate under evaluation. A strategy challenger is a parameter set held
here, *not* a strategy version: creating one changes nothing that trades. A model
challenger is a registered 5A model version.

Lifecycle (append-only ``learning_candidate_events``, one successor per event)::

    CANDIDATE -> VALIDATING -> SHADOW -> APPROVED -> RETIRED
         \\            \\          \\
          +------------+----------+--> REJECTED          RETIRED -> APPROVED: rollback only

Gates (every one is checked in the transaction that changes the status):

* Every transition is an explicit request by a named operator with a reason. The monitor
  (``ActorKind.SYSTEM``) may never move a candidate; nothing here runs on a schedule.
* VALIDATING -> SHADOW needs out-of-sample evidence. Strategy: a COMPLETED validation run of
  the same strategy whose verdict is CANDIDATE_FOR_REVIEW and whose frozen final parameters
  are the candidate's, plus a first-access holdout evaluation of that run. Model: the 5A
  verdict PASSED and an 8B model comparison showing incremental value after costs.
* SHADOW -> APPROVED needs shadow evidence gathered **after** the candidate entered SHADOW
  (forward data nobody could have selected on): a shadow comparison with at least
  ``min_shadow_sessions`` sessions (strategies) or ``min_shadow_predictions`` recorded shadow
  predictions (models). When a champion exists, the approval must name it
  (``replaces``): the champion is retired in the same transaction, never implicitly.
* Approving a strategy candidate creates a **new strategy version** with its parameters
  (spec 19.4: a new configuration is a new version; history is kept). Approving a model
  moves the 5A status (champion RETIRED, challenger APPROVED).
* Rollback re-approves a RETIRED candidate that was APPROVED before (a new strategy version
  with its parameters; for models the 5A ``reinstate``), retiring the current champion.

Every decision writes a ``learning_approvals`` row with the evidence it relied on, and an
audit-log entry. No hard risk limit is read or written here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.config import ResolvedConfig, sha256_of
from app.backtesting.data import CALENDAR, sessions_between
from app.backtesting.service import BacktestService
from app.core.clocks import Clock
from app.core.exceptions import AtlasError, NotFoundError
from app.database.models import ActorType, MLModelVersionRecord, StrategyVersionRecord
from app.database.models.learning import (
    LearningApprovalRecord,
    LearningCandidateEventRecord,
    LearningCandidateRecord,
    ValidationComparisonRecord,
    ValidationExperimentRecord,
    ValidationHoldoutRecord,
    ValidationRunRecord,
)
from app.database.models.ml import MLPredictionRecord
from app.database.repositories import AuditLogRepository
from app.database.sessions import session_scope
from app.learning import evaluation as ev
from app.learning.enums import (
    ActorKind,
    CandidateStatus,
    Decision,
    RunStatus,
    SubjectKind,
    Verdict,
)
from app.learning.walkforward import WalkForwardRunner, derive
from app.ml_engine.enums import LifecycleStatus, PredictionMode, ValidationStatus
from app.ml_engine.registry import lifecycle as ml
from app.strategies import catalog
from app.strategies.registry import StrategyRegistry

C = CandidateStatus
TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    C.CANDIDATE: frozenset({C.VALIDATING, C.REJECTED}),
    C.VALIDATING: frozenset({C.SHADOW, C.REJECTED}),
    C.SHADOW: frozenset({C.APPROVED, C.REJECTED}),
    C.APPROVED: frozenset({C.RETIRED}),
    C.REJECTED: frozenset(),
    C.RETIRED: frozenset(),  # APPROVED again only through rollback()
}


class GovernanceError(AtlasError):
    """The lifecycle change is not allowed (the API answers 409)."""

    code = "learning_governance_conflict"
    http_status = 409


@dataclass(frozen=True)
class Policy:
    min_shadow_sessions: int = 20
    min_shadow_predictions: int = 100
    require_holdout: bool = True


@dataclass(frozen=True)
class Champion:
    subject_kind: SubjectKind
    subject_key: str
    governed: bool  # an APPROVED candidate exists
    candidate_id: uuid.UUID | None
    strategy_version_id: uuid.UUID | None = None
    strategy_version: int | None = None
    parameters_sha256: str | None = None
    model_version_id: uuid.UUID | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "subject_kind": self.subject_kind.value,
            "subject_key": self.subject_key,
            "governed": self.governed,
            "label": "APPROVED under 8B"
            if self.governed
            else (
                "NONE APPROVED"
                if self.subject_kind is SubjectKind.MODEL and self.model_version_id is None
                else "INCUMBENT (not validated under 8B)"
            ),
            "candidate_id": None if self.candidate_id is None else str(self.candidate_id),
            "strategy_version_id": None
            if self.strategy_version_id is None
            else str(self.strategy_version_id),
            "strategy_version": self.strategy_version,
            "parameters_sha256": self.parameters_sha256,
            "model_version_id": None
            if self.model_version_id is None
            else str(self.model_version_id),
        }


def _lock(session: Session, kind: SubjectKind, key: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"learning:{kind.value}:{key}"}
    )


def latest_event(session: Session, candidate_id: uuid.UUID) -> LearningCandidateEventRecord | None:
    e = LearningCandidateEventRecord
    return session.scalar(
        select(e).where(e.candidate_id == candidate_id).order_by(e.id.desc()).limit(1)
    )


def status_of(session: Session, candidate_id: uuid.UUID) -> CandidateStatus:
    event = latest_event(session, candidate_id)
    if event is None:
        raise NotFoundError(f"candidate {candidate_id} has no status")
    return event.to_status


def candidates_of(session: Session, kind: SubjectKind, key: str) -> list[LearningCandidateRecord]:
    c = LearningCandidateRecord
    return list(
        session.scalars(
            select(c).where(c.subject_kind == kind, c.subject_key == key).order_by(c.created_at)
        )
    )


def approved_candidate(
    session: Session, kind: SubjectKind, key: str
) -> LearningCandidateRecord | None:
    for cand in candidates_of(session, kind, key):
        if status_of(session, cand.id) is C.APPROVED:
            return cand
    return None


def was_approved(session: Session, candidate_id: uuid.UUID) -> bool:
    e = LearningCandidateEventRecord
    return (
        session.scalar(
            select(e.id).where(e.candidate_id == candidate_id, e.to_status == C.APPROVED).limit(1)
        )
        is not None
    )


def champion(session: Session, kind: SubjectKind, key: str) -> Champion:
    governed = approved_candidate(session, kind, key)
    if kind is SubjectKind.STRATEGY:
        version = catalog.latest_version(session, key)
        params_sha = None
        if version is not None:
            params_sha = sha256_of(dict(catalog.configuration(session, version).parameters))
        return Champion(
            kind,
            key,
            governed is not None,
            None if governed is None else governed.id,
            None if version is None else version.id,
            None if version is None else version.version,
            params_sha,
        )
    approved = ml.approved_versions(session, key)
    return Champion(
        kind,
        key,
        governed is not None,
        None if governed is None else governed.id,
        model_version_id=approved[0] if approved else None,
    )


class GovernanceService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        registry: StrategyRegistry,
        backtests: BacktestService,
        policy: Policy | None = None,
    ) -> None:
        self._sessions = session_factory
        self.clock = clock
        self.registry = registry
        self.backtests = backtests
        self.runner = WalkForwardRunner(backtests)
        self.policy = policy or Policy()

    # ------------------------------------------------------------------ events
    def _event(
        self,
        session: Session,
        cand: LearningCandidateRecord,
        to: CandidateStatus,
        *,
        reason: str,
        actor: str,
        actor_kind: ActorKind = ActorKind.USER,
        evidence: dict[str, Any] | None = None,
        rollback: bool = False,
    ) -> LearningCandidateEventRecord:
        if not reason.strip():
            raise GovernanceError("a reason is required")
        if actor_kind is not ActorKind.USER:
            raise GovernanceError("only an operator can change a candidate's lifecycle status")
        latest = latest_event(session, cand.id)
        previous = None if latest is None else latest.to_status
        if previous is not None:
            allowed = TRANSITIONS[previous] | (
                {C.APPROVED} if rollback and previous is C.RETIRED else set()
            )
            if to not in allowed:
                raise GovernanceError(
                    f"{previous.value} -> {to.value} is not an allowed transition"
                )
        event = LearningCandidateEventRecord(
            candidate_id=cand.id,
            previous_event_id=None if latest is None else latest.id,
            from_status=previous,
            to_status=to,
            reason=reason.strip(),
            actor=actor,
            actor_kind=actor_kind,
            evidence=evidence or {},
        )
        session.add(event)
        session.flush()
        AuditLogRepository(session).append(
            actor_type=ActorType.USER,
            actor_id=actor,
            action="learning.candidate_status_changed",
            entity_type="learning_candidate",
            entity_id=str(cand.id),
            reason=reason.strip(),
            previous_state={"status": None if previous is None else previous.value},
            new_state={"status": to.value},
            details={"subject_kind": cand.subject_kind.value, "subject_key": cand.subject_key},
        )
        return event

    def _get(self, session: Session, candidate_id: uuid.UUID) -> LearningCandidateRecord:
        cand = session.get(LearningCandidateRecord, candidate_id)
        if cand is None:
            raise NotFoundError(f"candidate {candidate_id} does not exist")
        _lock(session, cand.subject_kind, cand.subject_key)
        return cand

    # ------------------------------------------------------------------ create
    def create_strategy_candidate(
        self,
        strategy_id: str,
        parameters: dict[str, Any],
        *,
        actor: str,
        reason: str,
        source_run_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        if strategy_id not in self.registry:
            raise GovernanceError(f"unknown strategy {strategy_id}")
        strategy = self.registry.get(strategy_id)
        with session_scope(self._sessions) as session:
            _lock(session, SubjectKind.STRATEGY, strategy_id)
            live = catalog.latest_version(session, strategy_id)
            if live is None:
                raise GovernanceError(f"{strategy_id} has no stored version; sync the catalog")
            base = dict(catalog.configuration(session, live).parameters)
            try:
                params = catalog.parameters_json(strategy.parse_parameters({**base, **parameters}))
            except ValueError as exc:
                raise GovernanceError(f"invalid parameters: {exc}") from None
            if (
                source_run_id is not None
                and session.get(ValidationRunRecord, source_run_id) is None
            ):
                raise NotFoundError(f"validation run {source_run_id} does not exist")
            cand = LearningCandidateRecord(
                subject_kind=SubjectKind.STRATEGY,
                subject_key=strategy_id,
                strategy_id=strategy_id,
                parameters=params,
                parameters_sha256=sha256_of(params),
                base_strategy_version_id=live.id,
                source_run_id=source_run_id,
                champion_at_creation=champion(session, SubjectKind.STRATEGY, strategy_id).as_json(),
                notes=reason.strip(),
                created_by=actor,
            )
            session.add(cand)
            session.flush()
            self._event(session, cand, C.CANDIDATE, reason=reason, actor=actor)
            return cand.id

    def create_model_candidate(
        self, model_version_id: uuid.UUID, *, actor: str, reason: str
    ) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            model = session.get(MLModelVersionRecord, model_version_id)
            if model is None:
                raise NotFoundError(f"model version {model_version_id} does not exist")
            _lock(session, SubjectKind.MODEL, model.model_name)
            if ml.current_status(session, model.id) is not LifecycleStatus.CANDIDATE:
                raise GovernanceError("only a 5A CANDIDATE model version can become a challenger")
            cand = LearningCandidateRecord(
                subject_kind=SubjectKind.MODEL,
                subject_key=model.model_name,
                model_version_id=model.id,
                champion_at_creation=champion(
                    session, SubjectKind.MODEL, model.model_name
                ).as_json(),
                notes=reason.strip(),
                created_by=actor,
            )
            session.add(cand)
            session.flush()
            self._event(session, cand, C.CANDIDATE, reason=reason, actor=actor)
            return cand.id

    def adopt_incumbent(self, strategy_id: str, *, actor: str, reason: str) -> uuid.UUID:
        """Record the live strategy version as the governed champion without changing it
        (so it can be rolled back to later). Allowed only while no candidate is APPROVED."""
        with session_scope(self._sessions) as session:
            _lock(session, SubjectKind.STRATEGY, strategy_id)
            if approved_candidate(session, SubjectKind.STRATEGY, strategy_id) is not None:
                raise GovernanceError(f"{strategy_id} already has an approved champion")
            live = catalog.latest_version(session, strategy_id)
            if live is None:
                raise GovernanceError(f"{strategy_id} has no stored version")
            params = dict(catalog.configuration(session, live).parameters)
            cand = LearningCandidateRecord(
                subject_kind=SubjectKind.STRATEGY,
                subject_key=strategy_id,
                strategy_id=strategy_id,
                parameters=params,
                parameters_sha256=sha256_of(params),
                base_strategy_version_id=live.id,
                champion_at_creation=champion(session, SubjectKind.STRATEGY, strategy_id).as_json(),
                notes=f"incumbent adopted: {reason.strip()}",
                created_by=actor,
            )
            session.add(cand)
            session.flush()
            self._event(session, cand, C.CANDIDATE, reason=reason, actor=actor)
            event = self._adopt_event(session, cand, live, reason, actor)
            session.add(
                LearningApprovalRecord(
                    candidate_id=cand.id,
                    event_id=event.id,
                    decision=Decision.APPROVE,
                    approver=actor,
                    reason=reason.strip(),
                    evidence={
                        "incumbent": True,
                        "note": "the live version was recorded as champion; nothing changed",
                    },
                    replaced=None,
                    strategy_version_id=live.id,
                )
            )
            return cand.id

    def _adopt_event(
        self,
        session: Session,
        cand: LearningCandidateRecord,
        live: StrategyVersionRecord,
        reason: str,
        actor: str,
    ) -> LearningCandidateEventRecord:
        # CANDIDATE -> APPROVED is not an ordinary transition; adoption records the existing
        # live configuration and changes nothing, so it bypasses the evidence gates by
        # construction (parameters equal the live version's).
        latest = latest_event(session, cand.id)
        assert latest is not None  # noqa: S101
        event = LearningCandidateEventRecord(
            candidate_id=cand.id,
            previous_event_id=latest.id,
            from_status=latest.to_status,
            to_status=C.APPROVED,
            reason=f"incumbent adopted without change: {reason.strip()}",
            actor=actor,
            actor_kind=ActorKind.USER,
            evidence={"incumbent_strategy_version": live.version},
        )
        session.add(event)
        session.flush()
        return event

    # ------------------------------------------------------------------ transitions
    def start_validation(self, candidate_id: uuid.UUID, *, actor: str, reason: str) -> None:
        with session_scope(self._sessions) as session:
            cand = self._get(session, candidate_id)
            self._event(session, cand, C.VALIDATING, reason=reason, actor=actor)

    def strategy_evidence(
        self, session: Session, cand: LearningCandidateRecord, run_id: uuid.UUID
    ) -> dict[str, Any]:
        run = session.get(ValidationRunRecord, run_id)
        if run is None:
            raise NotFoundError(f"validation run {run_id} does not exist")
        experiment = session.get(ValidationExperimentRecord, run.experiment_id)
        assert experiment is not None  # noqa: S101
        problems = []
        if run.status is not RunStatus.COMPLETED or run.summary is None:
            problems.append(f"run is {run.status.value}")
        elif cand.strategy_id not in experiment.strategy_ids or len(experiment.strategy_ids) != 1:
            problems.append("the run did not validate this strategy alone")
        else:
            final = run.summary.get("final_parameters_sha256", {}).get(cand.strategy_id)
            if final != cand.parameters_sha256:
                problems.append("the run's frozen final parameters are not the candidate's")
            if run.verdict is not Verdict.CANDIDATE_FOR_REVIEW:
                problems.append(
                    f"the run's verdict is {None if run.verdict is None else run.verdict.value}"
                )
        holdout = session.scalars(
            select(ValidationHoldoutRecord)
            .where(ValidationHoldoutRecord.run_id == run.id)
            .order_by(ValidationHoldoutRecord.created_at)
        ).first()
        if self.policy.require_holdout:
            if holdout is None:
                problems.append("the run's holdout has not been evaluated")
            elif holdout.access_number != 1:
                problems.append(
                    f"the holdout evaluation was access {holdout.access_number}: not untouched"
                )
        if problems:
            raise GovernanceError("insufficient validation evidence: " + "; ".join(problems))
        return {
            "validation_run_id": str(run.id),
            "verdict": None if run.verdict is None else run.verdict.value,
            "result_sha256": run.result_sha256,
            "holdout_evaluation_id": None if holdout is None else str(holdout.id),
            "holdout_net_return": None if holdout is None else holdout.result.get("net_return"),
        }

    def model_evidence(self, session: Session, cand: LearningCandidateRecord) -> dict[str, Any]:
        model = session.get(MLModelVersionRecord, cand.model_version_id)
        assert model is not None  # noqa: S101
        problems = []
        if model.validation_status is not ValidationStatus.PASSED:
            problems.append(f"5A verdict {model.validation_status.value}")
        comparison = None
        for row in session.scalars(
            select(ValidationComparisonRecord)
            .where(ValidationComparisonRecord.kind == SubjectKind.MODEL)
            .order_by(ValidationComparisonRecord.created_at.desc())
        ):
            for entry in row.result.get("models", []):
                if entry.get("training_run_id") == str(model.training_run_id):
                    comparison = (row, entry)
                    break
            if comparison:
                break
        if comparison is None:
            problems.append("no 8B model comparison covers this model's training run")
        elif not comparison[1].get("incremental_value"):
            problems.append(
                "the 8B comparison shows no incremental out-of-sample value after costs"
            )
        if problems:
            raise GovernanceError("insufficient model evidence: " + "; ".join(problems))
        assert comparison is not None  # noqa: S101
        return {"comparison_id": str(comparison[0].id), "entry": comparison[1]}

    def to_shadow(
        self, candidate_id: uuid.UUID, *, actor: str, reason: str, run_id: uuid.UUID | None = None
    ) -> None:
        with session_scope(self._sessions) as session:
            cand = self._get(session, candidate_id)
            if status_of(session, cand.id) is not C.VALIDATING:
                raise GovernanceError("only a VALIDATING candidate can enter SHADOW")
            if cand.subject_kind is SubjectKind.STRATEGY:
                rid = run_id or cand.source_run_id
                if rid is None:
                    raise GovernanceError("name the validation run that supports this candidate")
                evidence = self.strategy_evidence(session, cand, rid)
            else:
                evidence = self.model_evidence(session, cand)
                assert cand.model_version_id is not None  # noqa: S101
                status = ml.current_status(session, cand.model_version_id)
                if status is LifecycleStatus.CANDIDATE:
                    ml.transition(
                        session,
                        cand.model_version_id,
                        LifecycleStatus.VALIDATED,
                        reason=reason,
                        actor=actor,
                    )
                    status = LifecycleStatus.VALIDATED
                if status is LifecycleStatus.VALIDATED:
                    ml.transition(
                        session,
                        cand.model_version_id,
                        LifecycleStatus.SHADOW,
                        reason=reason,
                        actor=actor,
                    )
                elif status is not LifecycleStatus.SHADOW:
                    raise GovernanceError(f"the 5A model status is {status.value}")
            self._event(session, cand, C.SHADOW, reason=reason, actor=actor, evidence=evidence)

    def shadow_started(self, session: Session, candidate_id: uuid.UUID) -> datetime:
        e = LearningCandidateEventRecord
        at = session.scalar(
            select(func.max(e.created_at)).where(
                e.candidate_id == candidate_id, e.to_status == C.SHADOW
            )
        )
        if at is None:
            raise GovernanceError("the candidate never entered SHADOW")
        return at

    def evaluate_shadow(self, candidate_id: uuid.UUID, *, actor: str) -> uuid.UUID:
        """Strategies: replay the challenger and the champion side by side over the sessions
        that completed after the candidate entered SHADOW (data that did not exist when it
        was selected), with the source run's universe, risk and costs. Models: count the
        shadow predictions recorded since then. Stored as a comparison record."""
        now = self.clock.now()
        with session_scope(self._sessions) as session:
            cand = self._get(session, candidate_id)
            if status_of(session, cand.id) is not C.SHADOW:
                raise GovernanceError("only a SHADOW candidate has a shadow evaluation")
            started = self.shadow_started(session, cand.id)
            if cand.subject_kind is SubjectKind.MODEL:
                p = MLPredictionRecord
                n = int(
                    session.scalar(
                        select(func.count(p.id)).where(
                            p.model_version_id == cand.model_version_id,
                            p.mode == PredictionMode.SHADOW,
                            p.created_at >= started,
                        )
                    )
                    or 0
                )
                result: dict[str, Any] = {
                    "shadow_started": started.isoformat(),
                    "shadow_predictions": n,
                    "sufficient": n >= self.policy.min_shadow_predictions,
                    "minimum": self.policy.min_shadow_predictions,
                }
            else:
                result = self._strategy_shadow(session, cand, started, now)
            row = ValidationComparisonRecord(
                kind=cand.subject_kind,
                title=f"shadow: {cand.subject_key} challenger vs champion",
                subjects={"candidate_id": str(cand.id), "shadow": True},
                result=result,
                result_sha256=sha256_of(result),
                created_by=actor,
            )
            session.add(row)
            session.flush()
            return row.id

    def _strategy_shadow(
        self, session: Session, cand: LearningCandidateRecord, started: datetime, now: datetime
    ) -> dict[str, Any]:
        if cand.source_run_id is None:
            raise GovernanceError(
                "the candidate has no source validation run to take the setup from"
            )
        run = session.get(ValidationRunRecord, cand.source_run_id)
        assert run is not None  # noqa: S101
        base = ResolvedConfig.model_validate(run.base_config)
        settle = timedelta(seconds=base.data.settle_seconds)
        days = [
            s
            for s in sessions_between(started.date(), now.date())
            if s.regular_open > started and s.regular_close + settle <= now
        ]
        out: dict[str, Any] = {
            "shadow_started": started.isoformat(),
            "sessions": len(days),
            "minimum_sessions": self.policy.min_shadow_sessions,
            "calendar": CALENDAR.code if hasattr(CALENDAR, "code") else "XNYS",
        }
        if len(days) < 2:
            return {**out, "sufficient": False, "note": "no complete session since SHADOW began"}
        fresh = base.model_copy(
            update={"data": base.data.model_copy(update={"data_known_at": now})}
        )
        first, last = days[0].day, days[-1].day
        if cand.strategy_id is None or cand.parameters is None:
            raise GovernanceError("a strategy candidate needs a strategy and parameters")
        live = catalog.latest_version(session, cand.strategy_id)
        champ_params = (
            None if live is None else dict(catalog.configuration(session, live).parameters)
        )
        evaluations = {}
        for label, params in (("challenger", cand.parameters), ("champion", champ_params)):
            if params is None:
                continue
            cfg = derive(
                fresh,
                start=first,
                end=last,
                parameters={cand.strategy_id: params},
                label=f"shadow {label}",
            )
            result, dataset = self.runner.replay(session, cfg)
            window = ev.evaluate_window(result, base.initial_capital, first, last, dataset=dataset)
            evaluations[label] = window.as_json()
        return {
            **out,
            "first": first.isoformat(),
            "last": last.isoformat(),
            "sufficient": len(days) >= self.policy.min_shadow_sessions,
            **evaluations,
            "note": "shadow = forward sessions after selection; simulated, never traded",
        }

    def approve(
        self,
        candidate_id: uuid.UUID,
        *,
        actor: str,
        reason: str,
        replaces: uuid.UUID | None = None,
    ) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            cand = self._get(session, candidate_id)
            if status_of(session, cand.id) is not C.SHADOW:
                raise GovernanceError("only a SHADOW candidate can be approved")
            started = self.shadow_started(session, cand.id)
            shadow = [
                r
                for r in session.scalars(
                    select(ValidationComparisonRecord)
                    .where(ValidationComparisonRecord.kind == cand.subject_kind)
                    .order_by(ValidationComparisonRecord.created_at.desc())
                )
                if r.subjects.get("candidate_id") == str(cand.id)
                and r.subjects.get("shadow")
                and r.created_at >= started
            ]
            if not shadow or not shadow[0].result.get("sufficient"):
                raise GovernanceError(
                    "approval needs a sufficient shadow evaluation recorded after SHADOW began"
                )
            current = approved_candidate(session, cand.subject_kind, cand.subject_key)
            replaced = self._replace(session, cand, current, replaces, actor, reason)
            event = self._event(
                session,
                cand,
                C.APPROVED,
                reason=reason,
                actor=actor,
                evidence={"shadow_comparison_id": str(shadow[0].id)},
            )
            version_id, model_id = self._activate(session, cand, actor, reason, "approval")
            approval = LearningApprovalRecord(
                candidate_id=cand.id,
                event_id=event.id,
                decision=Decision.APPROVE,
                approver=actor,
                reason=reason.strip(),
                evidence={
                    "shadow_comparison_id": str(shadow[0].id),
                    "shadow": shadow[0].result,
                    "transitions": [
                        {"to": e.to_status.value, "evidence": e.evidence}
                        for e in session.scalars(
                            select(LearningCandidateEventRecord)
                            .where(LearningCandidateEventRecord.candidate_id == cand.id)
                            .order_by(LearningCandidateEventRecord.id)
                        )
                    ],
                },
                replaced=replaced,
                strategy_version_id=version_id,
                model_version_id=model_id,
            )
            session.add(approval)
            session.flush()
            return approval.id

    def _replace(
        self,
        session: Session,
        cand: LearningCandidateRecord,
        current: LearningCandidateRecord | None,
        replaces: uuid.UUID | None,
        actor: str,
        reason: str,
    ) -> dict[str, Any] | None:
        model_champion = None
        if cand.subject_kind is SubjectKind.MODEL:
            approved = ml.approved_versions(session, cand.subject_key)
            model_champion = approved[0] if approved else None
        if current is None and model_champion is None:
            if replaces is not None:
                raise GovernanceError("there is no approved champion to replace")
            return None
        expected = current.id if current is not None else model_champion
        if replaces != expected:
            raise GovernanceError(
                f"an approved champion exists ({expected}); name it in 'replaces' to retire "
                "it in the same decision: a champion is never replaced implicitly"
            )
        if current is not None:
            self._event(
                session,
                current,
                C.RETIRED,
                reason=f"replaced by candidate {cand.id}: {reason}",
                actor=actor,
            )
        if model_champion is not None:
            ml.transition(
                session,
                model_champion,
                LifecycleStatus.RETIRED,
                reason=f"replaced: {reason}",
                actor=actor,
            )
        return {
            "candidate_id": None if current is None else str(current.id),
            "model_version_id": None if model_champion is None else str(model_champion),
        }

    def _activate(
        self, session: Session, cand: LearningCandidateRecord, actor: str, reason: str, how: str
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """Make the candidate the acting version: a new strategy version with its
        parameters, or the 5A APPROVED status. Returns (strategy_version_id, model_id)."""
        if cand.subject_kind is SubjectKind.STRATEGY:
            if cand.strategy_id is None or cand.parameters is None:
                raise GovernanceError("a strategy candidate needs a strategy and parameters")
            strategy = self.registry.get(cand.strategy_id)
            version, _created = catalog.create_version(
                session,
                strategy,
                cand.parameters,
                reason=f"8B {how} of candidate {cand.id}: {reason}",
                actor=actor,
            )
            return version.id, None
        assert cand.model_version_id is not None  # noqa: S101
        if how == "rollback":
            ml.reinstate(session, cand.model_version_id, reason=reason, actor=actor)
        else:
            ml.transition(
                session, cand.model_version_id, LifecycleStatus.APPROVED, reason=reason, actor=actor
            )
        return None, cand.model_version_id

    def reject(self, candidate_id: uuid.UUID, *, actor: str, reason: str) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            cand = self._get(session, candidate_id)
            event = self._event(session, cand, C.REJECTED, reason=reason, actor=actor)
            if cand.subject_kind is SubjectKind.MODEL and cand.model_version_id is not None:
                status = ml.current_status(session, cand.model_version_id)
                if LifecycleStatus.REJECTED in ml.TRANSITIONS[status]:
                    ml.transition(
                        session,
                        cand.model_version_id,
                        LifecycleStatus.REJECTED,
                        reason=reason,
                        actor=actor,
                    )
            row = LearningApprovalRecord(
                candidate_id=cand.id,
                event_id=event.id,
                decision=Decision.REJECT,
                approver=actor,
                reason=reason.strip(),
                evidence={},
                replaced=None,
            )
            session.add(row)
            session.flush()
            return row.id

    def retire(self, candidate_id: uuid.UUID, *, actor: str, reason: str) -> uuid.UUID:
        """Retire an approved champion without a replacement. A strategy keeps trading its
        live version only under a governed champion, so retiring one also disables the
        strategy (the safe direction); re-enabling it is an explicit operator action."""
        with session_scope(self._sessions) as session:
            cand = self._get(session, candidate_id)
            event = self._event(session, cand, C.RETIRED, reason=reason, actor=actor)
            evidence: dict[str, Any] = {}
            if cand.subject_kind is SubjectKind.STRATEGY and cand.strategy_id is not None:
                strategy = self.registry.get(cand.strategy_id)
                status_event, changed = catalog.set_status(
                    session,
                    strategy,
                    enabled=False,
                    reason=f"8B champion {cand.id} retired: {reason}",
                    actor=actor,
                )
                evidence = {"strategy_disabled": changed, "status_event_id": status_event.id}
            elif cand.model_version_id is not None:
                ml.transition(
                    session,
                    cand.model_version_id,
                    LifecycleStatus.RETIRED,
                    reason=reason,
                    actor=actor,
                )
            row = LearningApprovalRecord(
                candidate_id=cand.id,
                event_id=event.id,
                decision=Decision.RETIRE,
                approver=actor,
                reason=reason.strip(),
                evidence=evidence,
                replaced=None,
            )
            session.add(row)
            session.flush()
            return row.id

    def rollback(self, candidate_id: uuid.UUID, *, actor: str, reason: str) -> uuid.UUID:
        """Re-approve a previously approved (now RETIRED) candidate, retiring the current
        champion in the same transaction."""
        with session_scope(self._sessions) as session:
            target = self._get(session, candidate_id)
            if status_of(session, target.id) is not C.RETIRED or not was_approved(
                session, target.id
            ):
                raise GovernanceError("rollback needs a RETIRED candidate that was APPROVED before")
            current = approved_candidate(session, target.subject_kind, target.subject_key)
            replaced = None
            if current is not None:
                self._event(
                    session,
                    current,
                    C.RETIRED,
                    reason=f"rolled back to {target.id}: {reason}",
                    actor=actor,
                )
                replaced = {"candidate_id": str(current.id)}
                if current.model_version_id is not None:
                    status = ml.current_status(session, current.model_version_id)
                    if status is LifecycleStatus.APPROVED:
                        ml.transition(
                            session,
                            current.model_version_id,
                            LifecycleStatus.RETIRED,
                            reason=f"rolled back: {reason}",
                            actor=actor,
                        )
            event = self._event(
                session,
                target,
                C.APPROVED,
                reason=f"rollback: {reason}",
                actor=actor,
                rollback=True,
            )
            version_id, model_id = self._activate(session, target, actor, reason, "rollback")
            row = LearningApprovalRecord(
                candidate_id=target.id,
                event_id=event.id,
                decision=Decision.ROLLBACK,
                approver=actor,
                reason=reason.strip(),
                evidence={"previously_approved": True},
                replaced=replaced,
                strategy_version_id=version_id,
                model_version_id=model_id,
            )
            session.add(row)
            session.flush()
            return row.id


__all__ = [
    "TRANSITIONS",
    "Champion",
    "GovernanceError",
    "GovernanceService",
    "Policy",
    "approved_candidate",
    "candidates_of",
    "champion",
    "latest_event",
    "status_of",
    "was_approved",
]
