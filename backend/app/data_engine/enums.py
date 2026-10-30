"""Market-data enums shared by schemas, capabilities and ORM models.

This module imports nothing from Atlas so both the data engine and the database
layer can depend on it without import cycles.
"""

from __future__ import annotations

from enum import StrEnum


class DataType(StrEnum):
    BAR = "BAR"
    QUOTE = "QUOTE"
    TRADE = "TRADE"


class FeedCoverage(StrEnum):
    """How much of the market a feed represents. Never treat SINGLE_VENUE as the market."""

    SINGLE_VENUE = "SINGLE_VENUE"  # one exchange, e.g. IEX
    CONSOLIDATED = "CONSOLIDATED"  # all exchanges via the SIPs (CTA + UTP)
