"""Security lifecycle ledger (X4.2, D-246). Append-only (UPDATE, DELETE and TRUNCATE refused).

Four tables keep apart what the research universes used to conflate:

* ``security_entities``: issuers (an SEC CIK), named as the SEC names them.
* ``security_identities``: securities. A fund share class is its SEC class id; an operating
  company's security is its issuer plus its class of securities (``CIK|LABEL``). A ticker is
  **not** an identity: tickers move between securities (reuse) and securities change
  tickers.
* ``security_lifecycle_events``: what happened to a security and when, as far as the
  evidence says. Ticker, venue and classification are event attributes, so a security's
  ticker, venue and type at any date are reconstructed from its events
  (:func:`app.research.lifecycle.reconstruct`). Every event carries its effective date (only
  when evidence states it), date bounds, the filing date, the confidence, the resolution
  status, an explanation and the evidence (artifact path, SHA-256, locator).
* ``security_lifecycle_sets``: registered, hashed ledgers built deterministically from the
  X4.1 evidence.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
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

from app.database.models.base import Base

EVENT_TYPES = (
    "INITIAL_LISTING",
    "LISTING_OBSERVED",
    "VENUE_REMOVAL",
    "TICKER_CHANGE",
    "EXCHANGE_TRANSFER",
    "DELISTING",
    "MERGER_ACQUISITION",
    "FUND_TERMINATION",
    "CLASSIFICATION",
    "RECLASSIFICATION",
)
STATUSES = ("CONFIRMED", "UNRESOLVED", "CONFLICTING", "REJECTED")
CONFIDENCE = ("HIGH", "MEDIUM", "LOW")
PRECISION = ("EXACT", "BOUNDED", "UNKNOWN")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class SecurityEntityRecord(Base):
    __tablename__ = "security_entities"
    __table_args__ = (
        UniqueConstraint("entity_key", name="uq_security_entities_entity_key"),
        CheckConstraint("cik IS NULL OR length(cik) = 10", name="cik_ten_digits"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entity_key: Mapped[str] = mapped_column(String(64))
    cik: Mapped[str | None] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(255))
    name_source: Mapped[str] = mapped_column(String(255))
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SecurityIdentityRecord(Base):
    __tablename__ = "security_identities"
    __table_args__ = (
        UniqueConstraint("security_key", name="uq_security_identities_security_key"),
        Index("ix_security_identities_entity_key", "entity_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    security_key: Mapped[str] = mapped_column(String(128))
    entity_key: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("security_entities.entity_key", ondelete="RESTRICT")
    )
    sec_class_id: Mapped[str | None] = mapped_column(String(16))
    class_label: Mapped[str] = mapped_column(String(64))
    class_description: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SecurityLifecycleEventRecord(Base):
    __tablename__ = "security_lifecycle_events"
    __table_args__ = (
        UniqueConstraint("code", "revision", name="uq_security_lifecycle_events_code_revision"),
        Index("ix_security_lifecycle_events_security_key", "security_key"),
        Index("ix_security_lifecycle_events_ticker", "ticker"),
        CheckConstraint(_in("event_type", EVENT_TYPES), name="event_type"),
        CheckConstraint(_in("resolution_status", STATUSES), name="resolution_status"),
        CheckConstraint(_in("confidence", CONFIDENCE), name="confidence"),
        CheckConstraint(_in("effective_precision", PRECISION), name="effective_precision"),
        CheckConstraint("revision >= 0", name="revision_non_negative"),
        # An effective date only when the evidence states it; otherwise bounds or nothing.
        CheckConstraint(
            "(effective_precision = 'EXACT') = (effective_date IS NOT NULL)",
            name="effective_date_iff_exact",
        ),
        CheckConstraint(
            "effective_precision <> 'BOUNDED' OR not_before IS NOT NULL OR not_after IS NOT NULL",
            name="bounded_has_a_bound",
        ),
        CheckConstraint(
            "effective_precision <> 'UNKNOWN' OR (not_before IS NULL AND not_after IS NULL)",
            name="unknown_has_no_bound",
        ),
        CheckConstraint(
            "not_before IS NULL OR not_after IS NULL OR not_before <= not_after",
            name="bounds_ordered",
        ),
        CheckConstraint(
            "event_type <> 'EXCHANGE_TRANSFER' OR "
            "(from_venue IS NOT NULL AND to_venue IS NOT NULL AND from_venue <> to_venue)",
            name="transfer_has_two_venues",
        ),
        CheckConstraint(
            "event_type <> 'TICKER_CHANGE' OR "
            "(from_ticker IS NOT NULL AND to_ticker IS NOT NULL AND from_ticker <> to_ticker)",
            name="ticker_change_has_two_tickers",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, server_default="0")
    security_key: Mapped[str] = mapped_column(
        String(128), ForeignKey("security_identities.security_key", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(24))
    effective_date: Mapped[date | None] = mapped_column(Date)
    effective_precision: Mapped[str] = mapped_column(String(8))
    not_before: Mapped[date | None] = mapped_column(Date)
    not_after: Mapped[date | None] = mapped_column(Date)
    filing_date: Mapped[date | None] = mapped_column(Date)
    ticker: Mapped[str | None] = mapped_column(String(16))
    from_ticker: Mapped[str | None] = mapped_column(String(16))
    to_ticker: Mapped[str | None] = mapped_column(String(16))
    venue: Mapped[str | None] = mapped_column(String(32))
    from_venue: Mapped[str | None] = mapped_column(String(32))
    to_venue: Mapped[str | None] = mapped_column(String(32))
    security_type: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(64))
    counterparty_entity_key: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[str] = mapped_column(String(8))
    resolution_status: Mapped[str] = mapped_column(String(12))
    explanation: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SecurityLifecycleSetRecord(Base):
    __tablename__ = "security_lifecycle_sets"
    __table_args__ = (
        UniqueConstraint("code", name="uq_security_lifecycle_sets_code"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(500))
    rule: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    members: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    ledger_sha256: Mapped[str] = mapped_column(String(64))
    evidence_manifest_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "CONFIDENCE",
    "EVENT_TYPES",
    "PRECISION",
    "STATUSES",
    "SecurityEntityRecord",
    "SecurityIdentityRecord",
    "SecurityLifecycleEventRecord",
    "SecurityLifecycleSetRecord",
]
