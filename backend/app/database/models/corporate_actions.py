"""Cash dividends retrieved from the market-data provider (D-237).

Both tables are append-only (UPDATE, DELETE and TRUNCATE refused by
``atlas_reject_modification``):

* ``cash_dividends``: one row per provider corporate-action id and revision. A later
  retrieval that returns different values for the same id is stored as the next revision;
  nothing is overwritten. ``retrieved_at`` is when Atlas received the row: a backtest uses
  only rows retrieved by its dataset knowledge time, newest revision first.
* ``cash_dividend_retrievals``: one row per symbol and requested date range per retrieval,
  with the number of dividends returned. It is the coverage record: a symbol with no
  retrieval covering a period has *unknown* dividends there, not zero.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base


class CashDividendRecord(Base):
    __tablename__ = "cash_dividends"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_action_id", "revision", name="uq_cash_dividends_revision"
        ),
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint("revision >= 0", name="revision_non_negative"),
        Index(None, "asset_id", "ex_date"),
        Index(None, "symbol", "ex_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_action_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, server_default="0")
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    ex_date: Mapped[date] = mapped_column(Date)
    # Per share, in the security's currency, on the ex-date's share basis (provider value).
    rate: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3), server_default="USD")
    special: Mapped[bool]
    foreign: Mapped[bool]
    process_date: Mapped[date | None] = mapped_column(Date)
    payable_date: Mapped[date | None] = mapped_column(Date)
    cusip: Mapped[str | None] = mapped_column(String(16))
    retrieved_at: Mapped[datetime]
    raw: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CashDividendRetrievalRecord(Base):
    __tablename__ = "cash_dividend_retrievals"
    __table_args__ = (
        CheckConstraint("range_start <= range_end", name="range_ordered"),
        CheckConstraint("dividends >= 0", name="dividends_non_negative"),
        Index(None, "symbol", "range_start", "range_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    range_start: Mapped[date] = mapped_column(Date)
    range_end: Mapped[date] = mapped_column(Date)
    dividends: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # OK | FAILED
    retrieved_at: Mapped[datetime]
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


EVENT_TYPES = ("TICKER_SUCCESSION", "CASH_DIVIDEND", "CASH_DIVIDEND_VOID")
VERIFICATION_STATUSES = ("VERIFIED", "UNRESOLVED", "REJECTED")


class CorporateActionEventRecord(Base):
    """One corporate-action correction record (D-243). Append-only; a changed record is a new
    ``revision`` of the same ``code``, never an edit. Provider data is never modified: a
    record *refers* to a provider record (``provider``, ``provider_ticker``,
    ``provider_action_id``) and states how it applies to a pinned research identity
    (``asset_id`` + ``identity_sha256``).

    * ``TICKER_SUCCESSION``: ``provider_ticker`` denoted the asset's security on the
      verified sessions ``valid_from``..``valid_to`` (identical-close probes).
    * ``CASH_DIVIDEND``: a provider cash dividend filed under ``provider_ticker`` belongs to
      the asset (``cash_amount`` per share at ``ex_date``; ``link_code`` names the
      succession record).
    * ``CASH_DIVIDEND_VOID``: a provider dividend filed under the asset's own ticker belongs to
      another security; the replay must not credit it.

    Only ``VERIFIED`` records can be members of a correction set.
    """

    __tablename__ = "corporate_action_events"
    __table_args__ = (
        UniqueConstraint("code", "revision", name="uq_corporate_action_events_code_revision"),
        CheckConstraint(
            "event_type IN ('TICKER_SUCCESSION', 'CASH_DIVIDEND', 'CASH_DIVIDEND_VOID')",
            name="event_type",
        ),
        CheckConstraint(
            "verification_status IN ('VERIFIED', 'UNRESOLVED', 'REJECTED')",
            name="verification_status",
        ),
        CheckConstraint("revision >= 0", name="revision_non_negative"),
        CheckConstraint(
            "event_type <> 'TICKER_SUCCESSION' OR (valid_from IS NOT NULL AND valid_to IS NOT "
            "NULL AND valid_from <= valid_to AND ex_date IS NULL AND cash_amount IS NULL)",
            name="succession_fields",
        ),
        CheckConstraint(
            "event_type NOT IN ('CASH_DIVIDEND', 'CASH_DIVIDEND_VOID') OR (ex_date IS NOT NULL "
            "AND provider_action_id IS NOT NULL AND cash_amount IS NOT NULL AND cash_amount > 0 "
            "AND valid_from IS NULL AND valid_to IS NULL)",
            name="dividend_fields",
        ),
        Index("ix_corporate_action_events_asset_id_ex_date", "asset_id", "ex_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(96))
    revision: Mapped[int] = mapped_column(Integer, server_default="0")
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    asset_label: Mapped[str] = mapped_column(String(64))
    identity_sha256: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    provider_ticker: Mapped[str] = mapped_column(String(64))
    provider_action_id: Mapped[str | None] = mapped_column(String(64))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    ex_date: Mapped[date | None] = mapped_column(Date)
    payable_date: Mapped[date | None] = mapped_column(Date)
    cash_amount: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(String(3), server_default="USD")
    special: Mapped[bool | None]
    foreign: Mapped[bool | None]
    link_code: Mapped[str | None] = mapped_column(String(96))
    # When the record's facts were knowable: the ex-date for a dividend (no declaration date
    # is available, so the conservative choice), the last verified session for a link.
    known_on: Mapped[date | None] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(String(16))
    verification: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    evidence: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CorporateActionSetRecord(Base):
    """A registered, immutable correction set (D-243): its members (event code + revision,
    all ``VERIFIED``) and ``content_sha256`` over their canonical content. A run that
    applies corrections names the set; the replay recomputes the hash before using it."""

    __tablename__ = "corporate_action_sets"
    __table_args__ = (
        UniqueConstraint("code", name="uq_corporate_action_sets_code"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    scope: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(500))
    rule: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    members: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    preregistration_code: Mapped[str] = mapped_column(String(64))
    document_sha256: Mapped[str] = mapped_column(String(64))
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "EVENT_TYPES",
    "VERIFICATION_STATUSES",
    "CashDividendRecord",
    "CashDividendRetrievalRecord",
    "CorporateActionEventRecord",
    "CorporateActionSetRecord",
]
