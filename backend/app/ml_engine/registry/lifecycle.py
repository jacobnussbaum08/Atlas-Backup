"""Model registry lifecycle (Phase 5A, spec 19.2 and 19.4).

A model version's status is the newest of its ``ml_model_status_events``. A version is
registered as CANDIDATE when its training run finishes; every later transition is an
explicit request with an actor and a reason (operator token on the API, or the CLI).
Nothing in Atlas promotes a model on its own, and results alone never change a status.

Allowed transitions::

    CANDIDATE -> VALIDATED  (needs validation_status PASSED: out-of-sample evidence)
    VALIDATED -> SHADOW     (runs alongside, recorded, never acted on)
    SHADOW    -> APPROVED   (needs PASSED, and no other APPROVED version of the same name)
    CANDIDATE, VALIDATED, SHADOW -> REJECTED
    VALIDATED, SHADOW, APPROVED -> RETIRED

REJECTED and RETIRED are final. Approving a version never retires the current approved
one implicitly: the operator retires it first, so an approved model is never silently
replaced. Transitions of one model name are serialized by an advisory lock, and the
status-event chain (one successor per event) rejects a concurrent transition.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.core.exceptions import AtlasError
from app.database.models import (
    ActorType,
    MLModelStatusEventRecord,
    MLModelVersionRecord,
)
from app.database.repositories import AuditLogRepository
from app.ml_engine.enums import LifecycleStatus, ValidationStatus

S = LifecycleStatus

TRANSITIONS: dict[LifecycleStatus, frozenset[LifecycleStatus]] = {
    S.CANDIDATE: frozenset({S.VALIDATED, S.REJECTED}),
    S.VALIDATED: frozenset({S.SHADOW, S.REJECTED, S.RETIRED}),
    S.SHADOW: frozenset({S.APPROVED, S.REJECTED, S.RETIRED}),
    S.APPROVED: frozenset({S.RETIRED}),
    S.REJECTED: frozenset(),
    S.RETIRED: frozenset(),
}
NEEDS_EVIDENCE = frozenset({S.VALIDATED, S.APPROVED})


class LifecycleError(AtlasError):
    """The transition is not allowed (the API answers 409)."""

    code = "ml_lifecycle_conflict"
    http_status = 409


class ModelNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class TransitionResult:
    event: MLModelStatusEventRecord
    previous: LifecycleStatus


def current_event(session: Session, model_version_id: uuid.UUID) -> MLModelStatusEventRecord | None:
    e = MLModelStatusEventRecord
    return session.scalar(
        select(e).where(e.model_version_id == model_version_id).order_by(e.id.desc()).limit(1)
    )


def current_status(session: Session, model_version_id: uuid.UUID) -> LifecycleStatus:
    event = current_event(session, model_version_id)
    if event is None:
        raise ModelNotFoundError(f"model version {model_version_id} has no status")
    return event.to_status


def statuses(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, LifecycleStatus]:
    if not ids:
        return {}
    e = MLModelStatusEventRecord
    rows = session.execute(
        select(e.model_version_id, e.to_status)
        .where(e.model_version_id.in_(ids))
        .ext(distinct_on(e.model_version_id))
        .order_by(e.model_version_id, e.id.desc())
    ).all()
    return {row.model_version_id: row.to_status for row in rows}


def _lock(session: Session, model_name: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"ml_model:{model_name}"}
    )


def register_initial(session: Session, model: MLModelVersionRecord, actor: str) -> None:
    session.add(
        MLModelStatusEventRecord(
            model_version_id=model.id,
            model_name=model.model_name,
            previous_event_id=None,
            from_status=None,
            to_status=S.CANDIDATE,
            reason="registered when its training run finished",
            actor=actor,
        )
    )
    session.flush()


def approved_versions(session: Session, model_name: str) -> list[uuid.UUID]:
    ids = list(
        session.scalars(
            select(MLModelVersionRecord.id).where(MLModelVersionRecord.model_name == model_name)
        )
    )
    return [i for i, status in statuses(session, ids).items() if status is S.APPROVED]


def transition(
    session: Session,
    model_version_id: uuid.UUID,
    to: LifecycleStatus,
    *,
    reason: str,
    actor: str,
) -> TransitionResult:
    if not reason.strip():
        raise LifecycleError("a reason is required")
    model = session.get(MLModelVersionRecord, model_version_id)
    if model is None:
        raise ModelNotFoundError(f"model version {model_version_id} does not exist")
    _lock(session, model.model_name)
    latest = current_event(session, model.id)
    if latest is None:
        raise ModelNotFoundError(f"model version {model_version_id} has no status")
    previous = latest.to_status
    if to not in TRANSITIONS[previous]:
        raise LifecycleError(f"{previous.value} -> {to.value} is not an allowed transition")
    if to in NEEDS_EVIDENCE and model.validation_status is not ValidationStatus.PASSED:
        raise LifecycleError(
            f"{to.value} needs out-of-sample evidence: validation status is "
            f"{model.validation_status.value}"
        )
    if to is S.APPROVED:
        others = [i for i in approved_versions(session, model.model_name) if i != model.id]
        if others:
            raise LifecycleError(
                f"{model.model_name} already has an APPROVED version ({others[0]}); retire it "
                "first: an approved model is never replaced implicitly"
            )
    event = MLModelStatusEventRecord(
        model_version_id=model.id,
        model_name=model.model_name,
        previous_event_id=latest.id,
        from_status=previous,
        to_status=to,
        reason=reason.strip(),
        actor=actor,
    )
    session.add(event)
    session.flush()
    AuditLogRepository(session).append(
        actor_type=ActorType.USER,
        actor_id=actor,
        action="ml_model.status_changed",
        entity_type="ml_model_version",
        entity_id=str(model.id),
        reason=reason.strip(),
        previous_state={"status": previous.value},
        new_state={"status": to.value},
        details={"model_name": model.model_name, "version": model.version},
    )
    return TransitionResult(event, previous)


def was_approved(session: Session, model_version_id: uuid.UUID) -> bool:
    e = MLModelStatusEventRecord
    return (
        session.scalar(
            select(e.id)
            .where(e.model_version_id == model_version_id, e.to_status == S.APPROVED)
            .limit(1)
        )
        is not None
    )


def reinstate(
    session: Session, model_version_id: uuid.UUID, *, reason: str, actor: str
) -> TransitionResult:
    """Rollback (Phase 8B, D-190): RETIRED -> APPROVED for a version that was APPROVED
    before, still has PASSED evidence, and only when no other version of its name is
    APPROVED. It is the only way out of RETIRED; :data:`TRANSITIONS` is unchanged, so an
    ordinary transition can never reach it. Explicit, audited, serialized per name."""
    if not reason.strip():
        raise LifecycleError("a reason is required")
    model = session.get(MLModelVersionRecord, model_version_id)
    if model is None:
        raise ModelNotFoundError(f"model version {model_version_id} does not exist")
    _lock(session, model.model_name)
    latest = current_event(session, model.id)
    if latest is None or latest.to_status is not S.RETIRED:
        raise LifecycleError("only a RETIRED version can be reinstated")
    if not was_approved(session, model.id):
        raise LifecycleError("only a version that was APPROVED before can be reinstated")
    if model.validation_status is not ValidationStatus.PASSED:
        raise LifecycleError("the version's out-of-sample evidence is not PASSED")
    others = [i for i in approved_versions(session, model.model_name) if i != model.id]
    if others:
        raise LifecycleError(
            f"{model.model_name} already has an APPROVED version ({others[0]}); retire it first"
        )
    event = MLModelStatusEventRecord(
        model_version_id=model.id,
        model_name=model.model_name,
        previous_event_id=latest.id,
        from_status=S.RETIRED,
        to_status=S.APPROVED,
        reason=f"rollback: {reason.strip()}",
        actor=actor,
    )
    session.add(event)
    session.flush()
    AuditLogRepository(session).append(
        actor_type=ActorType.USER,
        actor_id=actor,
        action="ml_model.reinstated",
        entity_type="ml_model_version",
        entity_id=str(model.id),
        reason=reason.strip(),
        previous_state={"status": S.RETIRED.value},
        new_state={"status": S.APPROVED.value},
        details={"model_name": model.model_name, "version": model.version},
    )
    return TransitionResult(event, S.RETIRED)


__all__ = [
    "NEEDS_EVIDENCE",
    "TRANSITIONS",
    "LifecycleError",
    "ModelNotFoundError",
    "TransitionResult",
    "approved_versions",
    "current_event",
    "current_status",
    "register_initial",
    "reinstate",
    "statuses",
    "transition",
    "was_approved",
]
