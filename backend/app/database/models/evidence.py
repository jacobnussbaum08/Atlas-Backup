"""Phase 11A evidence program (migration 0022). Paper trading only.

* ``evidence_protocols``: pre-registered, versioned promotion and rejection criteria. A
  protocol is fixed before the evidence it judges is evaluated; it is never changed or
  deleted (a new version is a new row). Registered by an operator only.
* ``evidence_data_audits``: a stored audit of the historical dataset behind an evaluation
  (coverage, gaps, corporate actions, survivorship). Append-only.
* ``evidence_scorecards`` and ``evidence_scorecard_entries``: a readiness scorecard under
  one protocol, with the backtest, out-of-sample and paper evidence kept apart, and one
  classification per strategy plus the portfolio. Append-only. A scorecard changes nothing
  that trades; governance reads it as a gate (an approval needs CANDIDATE_FOR_REVIEW).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum
from app.evidence.enums import AuditStatus, Classification, ReadinessVerdict

SHA = "~ '^[0-9a-f]{64}$'"
OPERATOR = "~ '^operator:[A-Za-z0-9_.@-]+$'"
PORTFOLIO = "PORTFOLIO"  # the subject of the portfolio-level scorecard entry


class EvidenceProtocolRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evidence_protocols"
    __table_args__ = (
        UniqueConstraint("name", "version"),
        UniqueConstraint("criteria_sha256"),
        CheckConstraint(f"criteria_sha256 {SHA}", name="criteria_sha_format"),
        CheckConstraint(f"registered_by {OPERATOR}", name="operator_only"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("length(btrim(rationale)) >= 3", name="rationale_given"),
    )

    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB)
    criteria_sha256: Mapped[str] = mapped_column(String(64))
    rationale: Mapped[str] = mapped_column(Text)
    registered_by: Mapped[str] = mapped_column(String(128))
    registered_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvidenceDataAuditRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evidence_data_audits"
    __table_args__ = (
        CheckConstraint(f"result_sha256 {SHA}", name="result_sha_format"),
        Index(None, "created_at"),
    )

    scope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[AuditStatus] = mapped_column(str_enum(AuditStatus, "evidence_audit_status"))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    audited_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvidenceScorecardRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "evidence_scorecards"
    __table_args__ = (
        CheckConstraint(f"result_sha256 {SHA}", name="result_sha_format"),
        Index(None, "created_at"),
    )

    protocol_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence_protocols.id", ondelete="RESTRICT")
    )
    data_audit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence_data_audits.id", ondelete="RESTRICT")
    )
    verdict: Mapped[ReadinessVerdict] = mapped_column(
        str_enum(ReadinessVerdict, "evidence_readiness_verdict")
    )
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    as_of: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvidenceScorecardEntryRecord(Base):
    __tablename__ = "evidence_scorecard_entries"
    __table_args__ = (
        UniqueConstraint("scorecard_id", "subject"),
        Index(None, "subject", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    scorecard_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence_scorecards.id", ondelete="RESTRICT")
    )
    subject: Mapped[str] = mapped_column(String(64))  # a strategy id, or PORTFOLIO
    classification: Mapped[Classification] = mapped_column(
        str_enum(Classification, "evidence_classification")
    )
    reasons: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "PORTFOLIO",
    "EvidenceDataAuditRecord",
    "EvidenceProtocolRecord",
    "EvidenceScorecardEntryRecord",
    "EvidenceScorecardRecord",
]
