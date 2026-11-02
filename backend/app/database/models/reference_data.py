"""Instrument reference data (Phase 3A).

* ``exchanges``: listing venues by MIC with their time zone and calendar. Seeded by
  migration 0006; every calendar code must exist in ``app.asset_discovery.sessions``
  (enforced by an integration test).
* ``provider_exchange_codes``: a provider's exchange codes mapped to MICs. A code without
  a verified MIC maps to NULL and is reported, never guessed.
* ``provider_assets``: a provider's own, permanent instrument identity (Alpaca's asset UUID)
  mapped one-to-one to an Atlas asset. Current-state row: last seen, current version.
* ``asset_reference_versions``: every distinct reference record a provider returned for an
  instrument, verbatim (``raw_json``) and normalized. Append-only. The version observed at
  time T is what Atlas knew about the instrument at T (tradability, shortability, ...).
* ``asset_lifecycle_events``: listings, symbol/exchange changes, suspensions, delistings,
  reactivations. Append-only. Providers report no effective dates, so ``effective_at`` is
  when Atlas observed the change (``effective_at_basis = OBSERVED``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.asset_discovery.enums import LifecycleEventType, ProviderAssetStatus, ReferenceSource
from app.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class ExchangeRecord(Base):
    __tablename__ = "exchanges"
    __table_args__ = (CheckConstraint("mic ~ '^[A-Z0-9]{4}$'", name="mic_format"),)

    mic: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    country: Mapped[str] = mapped_column(String(2))
    # IANA zone of the venue's trading day, e.g. America/New_York.
    timezone: Mapped[str] = mapped_column(String(64))
    # Session calendar implementation (``app.asset_discovery.sessions``); NULL when Atlas has
    # no verified calendar for the venue.
    calendar_code: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProviderExchangeCodeRecord(Base):
    __tablename__ = "provider_exchange_codes"

    provider: Mapped[str] = mapped_column(
        String(32), ForeignKey("market_data_providers.code", ondelete="RESTRICT"), primary_key=True
    )
    provider_exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    mic: Mapped[str | None] = mapped_column(
        String(8), ForeignKey("exchanges.mic", ondelete="RESTRICT")
    )
    notes: Mapped[str | None] = mapped_column(Text)


class ProviderAssetRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "provider_assets"
    __table_args__ = (
        UniqueConstraint("provider", "provider_asset_id"),
        # One Atlas asset has at most one instrument per provider.
        UniqueConstraint("provider", "asset_id"),
        CheckConstraint("current_version >= 1", name="version_positive"),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_ordered"),
        Index(None, "provider", "provider_status"),
    )

    provider: Mapped[str] = mapped_column(
        String(32), ForeignKey("market_data_providers.code", ondelete="RESTRICT")
    )
    provider_asset_id: Mapped[str] = mapped_column(String(64))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider_asset_class: Mapped[str] = mapped_column(String(32))
    provider_status: Mapped[ProviderAssetStatus] = mapped_column(
        str_enum(ProviderAssetStatus, "provider_asset_status")
    )
    current_symbol: Mapped[str] = mapped_column(String(128))
    current_version: Mapped[int] = mapped_column(Integer)
    current_content_sha256: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime]
    # Last time a listing or lookup returned the instrument.
    last_seen_at: Mapped[datetime]
    last_seen_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_jobs.id", ondelete="RESTRICT")
    )
    # Set when a complete listing no longer contains the instrument; cleared when seen again.
    missing_since: Mapped[datetime | None]


class AssetReferenceVersionRecord(Base):
    __tablename__ = "asset_reference_versions"
    __table_args__ = (
        UniqueConstraint("provider_asset_ref", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        Index(None, "asset_id", "observed_at"),
        Index(None, "discovery_job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    provider_asset_ref: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("provider_assets.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(String(32))
    provider_asset_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    # When Atlas first observed this content; the version is what Atlas knew from then on.
    observed_at: Mapped[datetime]
    source: Mapped[ReferenceSource] = mapped_column(str_enum(ReferenceSource, "reference_source"))
    discovery_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_jobs.id", ondelete="RESTRICT")
    )
    # The instrument's trading symbol, and the symbol exactly as the provider wrote it.
    symbol: Mapped[str] = mapped_column(String(64))
    provider_symbol: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(255))
    provider_asset_class: Mapped[str] = mapped_column(String(32))
    provider_exchange: Mapped[str | None] = mapped_column(String(32))
    exchange_mic: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[ProviderAssetStatus] = mapped_column(
        str_enum(ProviderAssetStatus, "provider_asset_status")
    )
    # NULL = the provider did not report the field (never defaulted to False).
    tradable: Mapped[bool | None] = mapped_column(Boolean)
    marginable: Mapped[bool | None] = mapped_column(Boolean)
    shortable: Mapped[bool | None] = mapped_column(Boolean)
    easy_to_borrow: Mapped[bool | None] = mapped_column(Boolean)
    borrow_status: Mapped[str | None] = mapped_column(String(32))
    fractionable: Mapped[bool | None] = mapped_column(Boolean)
    margin_requirement_long: Mapped[Decimal | None]
    margin_requirement_short: Mapped[Decimal | None]
    min_order_size: Mapped[Decimal | None]
    min_trade_increment: Mapped[Decimal | None]
    price_increment: Mapped[Decimal | None]
    attributes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default="{}")
    # How the provider signalled a delisting, if it did.
    delisting_evidence: Mapped[str | None] = mapped_column(Text)
    # The provider's record exactly as received (canonical key order, exact numbers).
    raw_json: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssetLifecycleEventRecord(Base):
    __tablename__ = "asset_lifecycle_events"
    __table_args__ = (
        CheckConstraint("effective_at <= observed_at", name="effective_not_after_observed"),
        Index(None, "asset_id", "effective_at"),
        Index(None, "event_type", "effective_at"),
        Index(None, "discovery_job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider: Mapped[str | None] = mapped_column(String(32))
    event_type: Mapped[LifecycleEventType] = mapped_column(
        str_enum(LifecycleEventType, "lifecycle_event_type")
    )
    effective_at: Mapped[datetime]
    # OBSERVED: the provider reports no date, so this is when Atlas noticed the change.
    effective_at_basis: Mapped[str] = mapped_column(String(32), server_default="OBSERVED")
    observed_at: Mapped[datetime]
    previous_state: Mapped[dict[str, Any] | None]
    new_state: Mapped[dict[str, Any] | None]
    reason: Mapped[str] = mapped_column(Text)
    discovery_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_jobs.id", ondelete="RESTRICT")
    )
    reference_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("asset_reference_versions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
