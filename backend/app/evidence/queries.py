"""Read models of the Phase 11A evidence program (API and CLI)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.database.models.evidence import (
    EvidenceDataAuditRecord,
    EvidenceProtocolRecord,
    EvidenceScorecardEntryRecord,
    EvidenceScorecardRecord,
)


def protocol_view(p: EvidenceProtocolRecord, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(p.id),
        "name": p.name,
        "version": p.version,
        "criteria_sha256": p.criteria_sha256,
        "rationale": p.rationale,
        "registered_by": p.registered_by,
        "registered_at": p.registered_at.isoformat(),
    }
    if full:
        out["criteria"] = p.criteria
    return out


def protocols(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(EvidenceProtocolRecord).order_by(EvidenceProtocolRecord.registered_at.desc())
    ).all()
    return [protocol_view(p) for p in rows]


def protocol_detail(session: Session, protocol_id: uuid.UUID) -> dict[str, Any]:
    p = session.get(EvidenceProtocolRecord, protocol_id)
    if p is None:
        raise NotFoundError(f"evidence protocol {protocol_id} does not exist")
    return protocol_view(p, full=True)


def audit_view(a: EvidenceDataAuditRecord, *, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(a.id),
        "status": a.status.value,
        "scope": a.scope,
        "failures": a.result.get("failures", []),
        "warnings": a.result.get("warnings", []),
        "result_sha256": a.result_sha256,
        "created_by": a.created_by,
        "audited_at": a.audited_at.isoformat(),
    }
    if full:
        out["result"] = a.result
    return out


def audits(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(EvidenceDataAuditRecord)
        .order_by(EvidenceDataAuditRecord.created_at.desc())
        .limit(limit)
    ).all()
    return [audit_view(a) for a in rows]


def audit_detail(session: Session, audit_id: uuid.UUID) -> dict[str, Any]:
    a = session.get(EvidenceDataAuditRecord, audit_id)
    if a is None:
        raise NotFoundError(f"data audit {audit_id} does not exist")
    return audit_view(a, full=True)


def _entries(session: Session, scorecard_id: uuid.UUID) -> list[dict[str, Any]]:
    e = EvidenceScorecardEntryRecord
    return [
        {"subject": r.subject, "classification": r.classification.value, "reasons": r.reasons}
        for r in session.scalars(select(e).where(e.scorecard_id == scorecard_id).order_by(e.id))
    ]


def scorecard_view(
    session: Session, s: EvidenceScorecardRecord, *, full: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(s.id),
        "protocol_id": str(s.protocol_id),
        "data_audit_id": None if s.data_audit_id is None else str(s.data_audit_id),
        "verdict": s.verdict.value,
        "as_of": s.as_of.isoformat(),
        "created_by": s.created_by,
        "result_sha256": s.result_sha256,
        "inputs": s.inputs,
        "entries": _entries(session, s.id),
    }
    if full:
        out["result"] = s.result
    return out


def scorecards(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(EvidenceScorecardRecord)
        .order_by(EvidenceScorecardRecord.created_at.desc())
        .limit(limit)
    ).all()
    return [scorecard_view(session, s) for s in rows]


def scorecard_detail(session: Session, scorecard_id: uuid.UUID) -> dict[str, Any]:
    s = session.get(EvidenceScorecardRecord, scorecard_id)
    if s is None:
        raise NotFoundError(f"scorecard {scorecard_id} does not exist")
    return scorecard_view(session, s, full=True)


def latest_scorecard(session: Session) -> dict[str, Any] | None:
    s = session.scalars(
        select(EvidenceScorecardRecord).order_by(EvidenceScorecardRecord.created_at.desc()).limit(1)
    ).first()
    return None if s is None else scorecard_view(session, s, full=True)


__all__ = [
    "audit_detail",
    "audits",
    "latest_scorecard",
    "protocol_detail",
    "protocols",
    "scorecard_detail",
    "scorecards",
]
