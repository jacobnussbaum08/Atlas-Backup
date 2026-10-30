"""Market-data reference tables (Phase 2A).

``market_data_providers`` and ``market_data_feeds`` give provider and feed identity
referential integrity: every mapping (and, from 2B, every stored bar/quote/trade)
must name a registered provider and feed. Rows are seeded by migrations and must
match the adapters' declared capabilities (enforced by an integration test).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.data_engine.enums import FeedCoverage
from app.database.models.base import Base, str_enum


class MarketDataProviderRecord(Base):
    __tablename__ = "market_data_providers"
    __table_args__ = (CheckConstraint("code ~ '^[a-z][a-z0-9_]{1,31}$'", name="code_format"),)

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MarketDataFeedRecord(Base):
    __tablename__ = "market_data_feeds"
    __table_args__ = (
        CheckConstraint("code ~ '^[a-z][a-z0-9_]{1,31}$'", name="code_format"),
        CheckConstraint("delay_seconds >= 0", name="delay_non_negative"),
    )

    provider: Mapped[str] = mapped_column(
        ForeignKey("market_data_providers.code", ondelete="RESTRICT"), primary_key=True
    )
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    coverage: Mapped[FeedCoverage] = mapped_column(str_enum(FeedCoverage, "feed_coverage"))
    delay_seconds: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
