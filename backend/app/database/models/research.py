"""Independent research data foundation (D-239). Paper trading only; simulation research.

All tables are append-only (UPDATE, DELETE and TRUNCATE refused by
``atlas_reject_modification``); a change is a new row (a new revision or a new code).

* ``research_securities``: a security master built from provider and regulator listings:
  one row per (provider, symbol, revision) with its type, the bars observed in the scanned
  range, and an inferred delisting date when the provider lists it as inactive.
* ``research_universes``: a universe rule (code, version, the full rule and its hash).
* ``research_universe_candidates``: every candidate a formation considered, with the
  metrics used, its eligibility (and why not), its rank and whether it became a member,
  effective from the session after formation to the next formation or its delisting.
* ``research_data_reservations``: data set aside for independent validation (a period, a
  universe, or an exploration-only record), with its dataset hash.
* ``research_preregistrations``: a pre-registration document's hash and the reservations it
  may use; a backtest touching reserved data must name one.
* ``research_exclusions`` (D-240): assets a registered exclusion pre-registration removes from
  every research universe and from every run on reserved data, from its creation onward
  (rows created after a run's knowledge time never change it). Scope ``ASSET`` removes the
  asset; scope ``WINDOW`` (D-242) marks contaminated sessions of one asset, from which the
  replay derives the sessions it must not hold or decide on.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class ResearchSecurityRecord(Base):
    __tablename__ = "research_securities"
    __table_args__ = (
        UniqueConstraint("provider", "symbol", "revision", name="uq_research_securities_revision"),
        CheckConstraint("security_type IN ('STOCK', 'ETF', 'UNKNOWN')", name="security_type"),
        CheckConstraint(
            "first_bar IS NULL OR last_bar IS NULL OR first_bar <= last_bar", name="bars_ordered"
        ),
        Index(None, "symbol"),
        Index(None, "asset_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, server_default="0")
    provider_asset_id: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(255))
    exchange: Mapped[str | None] = mapped_column(String(32))
    provider_status: Mapped[str] = mapped_column(String(16))
    security_type: Mapped[str] = mapped_column(String(8))
    type_basis: Mapped[str] = mapped_column(Text)
    cik: Mapped[str | None] = mapped_column(String(10))
    scan_start: Mapped[date] = mapped_column(Date)
    scan_end: Mapped[date] = mapped_column(Date)
    first_bar: Mapped[date | None] = mapped_column(Date)
    last_bar: Mapped[date | None] = mapped_column(Date)
    delisted_on: Mapped[date | None] = mapped_column(Date)
    delisting_basis: Mapped[str | None] = mapped_column(Text)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    retrieved_at: Mapped[datetime]
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResearchUniverseRecord(Base):
    __tablename__ = "research_universes"
    __table_args__ = (UniqueConstraint("code", name="uq_research_universes_code"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    rule: Mapped[dict[str, Any]]
    rule_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResearchUniverseCandidateRecord(Base):
    __tablename__ = "research_universe_candidates"
    __table_args__ = (
        UniqueConstraint(
            "universe_code",
            "formation_session",
            "symbol",
            name="uq_research_universe_candidates_formation",
        ),
        CheckConstraint("NOT member OR eligible", name="member_is_eligible"),
        CheckConstraint(
            "NOT member OR (effective_from IS NOT NULL AND effective_to IS NOT NULL)",
            name="member_has_span",
        ),
        Index(None, "universe_code", "member", "effective_from"),
        Index(None, "asset_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    universe_code: Mapped[str] = mapped_column(String(64))
    formation_session: Mapped[date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String(64))
    security_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_securities.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    last_close: Mapped[Decimal | None]
    median_dollar_volume: Mapped[Decimal | None]
    sessions_observed: Mapped[int] = mapped_column(Integer)
    eligible: Mapped[bool] = mapped_column(Boolean)
    ineligible_reason: Mapped[str | None] = mapped_column(String(64))
    rank: Mapped[int | None] = mapped_column(Integer)
    member: Mapped[bool] = mapped_column(Boolean)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResearchDataReservationRecord(Base):
    __tablename__ = "research_data_reservations"
    __table_args__ = (
        UniqueConstraint("code", name="uq_research_data_reservations_code"),
        CheckConstraint("scope IN ('PERIOD', 'UNIVERSE', 'EXPLORATION_ONLY')", name="scope"),
        CheckConstraint(
            "scope <> 'PERIOD' OR (start_date IS NOT NULL AND end_date IS NOT NULL)",
            name="period_has_dates",
        ),
        CheckConstraint(
            "scope <> 'UNIVERSE' OR universe_code IS NOT NULL", name="universe_has_code"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    universe_code: Mapped[str | None] = mapped_column(String(64))
    symbols: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    dataset_sha256: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResearchPreregistrationRecord(Base):
    __tablename__ = "research_preregistrations"
    __table_args__ = (UniqueConstraint("code", name="uq_research_preregistrations_code"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    document_path: Mapped[str] = mapped_column(Text)
    document_sha256: Mapped[str] = mapped_column(String(64))
    reservation_codes: Mapped[list[str]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ResearchExclusionRecord(Base):
    __tablename__ = "research_exclusions"
    __table_args__ = (
        UniqueConstraint("code", "asset_id", "event_session", name="uq_research_exclusions_entry"),
        CheckConstraint("scope IN ('ASSET', 'WINDOW')", name="scope"),
        # D-242: ASSET rows carry no window; WINDOW rows carry the contaminated sessions.
        CheckConstraint(
            "(scope = 'ASSET' AND contaminated_from IS NULL AND contaminated_to IS NULL "
            "AND blocked_through IS NULL) OR "
            "(scope = 'WINDOW' AND contaminated_from IS NOT NULL AND contaminated_to IS NOT NULL "
            "AND contaminated_from <= contaminated_to "
            "AND event_session BETWEEN contaminated_from AND contaminated_to "
            "AND (blocked_through IS NULL OR blocked_through >= contaminated_to))",
            name="window",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="RESTRICT"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(64))
    universe_code: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16))
    event_session: Mapped[date] = mapped_column(Date)
    # D-242 WINDOW scope: the contaminated sessions (inclusive) and, for an identity splice
    # inside a membership span, the last session the asset stays blocked regardless.
    contaminated_from: Mapped[date | None] = mapped_column(Date)
    contaminated_to: Mapped[date | None] = mapped_column(Date)
    blocked_through: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(Text)
    preregistration_code: Mapped[str] = mapped_column(String(64))
    document_sha256: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "ResearchDataReservationRecord",
    "ResearchExclusionRecord",
    "ResearchPreregistrationRecord",
    "ResearchSecurityRecord",
    "ResearchUniverseCandidateRecord",
    "ResearchUniverseRecord",
]
