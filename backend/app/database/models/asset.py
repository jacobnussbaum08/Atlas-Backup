"""Asset identity (spec sections 7.3 and 21).

``assets`` holds Atlas's internal, provider-independent instrument identity.
Provider-specific symbols live separately in ``asset_identifiers`` so that a
provider change never rewrites an Atlas asset ID.

Instrument precision (tick/lot sizes), exchanges, sessions, and universe
membership are separate tables introduced in later phases.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    FOREX = "FOREX"
    CRYPTO = "CRYPTO"


class AssetStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DELISTED = "DELISTED"


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "asset_class", "exchange_code", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint("symbol = upper(symbol) AND length(symbol) > 0", name="symbol_upper"),
        CheckConstraint("quote_currency = upper(quote_currency)", name="quote_currency_upper"),
        CheckConstraint(
            "base_currency IS NULL OR base_currency = upper(base_currency)",
            name="base_currency_upper",
        ),
        CheckConstraint(
            "asset_class NOT IN ('FOREX', 'CRYPTO') OR base_currency IS NOT NULL",
            name="pair_has_base_currency",
        ),
        Index(None, "asset_class", "status"),
    )

    symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[AssetClass] = mapped_column(str_enum(AssetClass, "asset_class"))
    name: Mapped[str | None] = mapped_column(String(255))
    # MIC or Atlas exchange code; NULL for OTC markets such as spot forex.
    exchange_code: Mapped[str | None] = mapped_column(String(32))
    quote_currency: Mapped[str] = mapped_column(String(16))
    # For pairs (forex, crypto) the base currency/asset, e.g. EUR in EUR/USD.
    base_currency: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[AssetStatus] = mapped_column(
        str_enum(AssetStatus, "asset_status"), default=AssetStatus.ACTIVE, server_default="ACTIVE"
    )

    identifiers: Mapped[list[AssetIdentifier]] = relationship(back_populates="asset")


class MappingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"  # provider temporarily not serving the symbol; mapping still current
    RETIRED = "RETIRED"  # validity ended (valid_to set); kept for point-in-time history


class AssetIdentifier(UUIDPrimaryKeyMixin, Base):
    """A provider's symbol for an Atlas asset over an effective period [valid_from, valid_to).

    Symbols can change and be reused, so the asset ID, never the ticker, is identity.
    Migration 0002 adds two exclusion constraints (not expressible to autogenerate):
    a provider symbol never maps to two assets in overlapping periods, and an asset
    never has two overlapping symbols at the same provider.
    """

    __tablename__ = "asset_identifiers"
    __table_args__ = (
        UniqueConstraint("provider", "provider_symbol", "valid_from"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="valid_period_ordered"),
        CheckConstraint("(status = 'RETIRED') = (valid_to IS NOT NULL)", name="retired_iff_ended"),
        Index(None, "asset_id"),
        # A provider symbol maps to at most one asset at a time.
        Index(
            "uq_asset_identifiers_current_provider_symbol",
            "provider",
            "provider_symbol",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    provider: Mapped[str] = mapped_column(
        String(64), ForeignKey("market_data_providers.code", ondelete="RESTRICT")
    )
    provider_symbol: Mapped[str] = mapped_column(String(128))
    # The provider's own exchange code for the listing, where it reports one.
    provider_exchange: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[MappingStatus] = mapped_column(
        str_enum(MappingStatus, "mapping_status"),
        default=MappingStatus.ACTIVE,
        server_default="ACTIVE",
    )
    valid_from: Mapped[datetime] = mapped_column(server_default=func.now())
    valid_to: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    asset: Mapped[Asset] = relationship(back_populates="identifiers")
