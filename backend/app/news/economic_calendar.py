"""Scheduled economic events: the provider contract and its current status (Phase 3C).

A scheduled economic calendar (release name, country, currency, scheduled time, previous,
forecast and actual values, the source's importance label, revisions) is a different data
product from news. Alpaca's news API is not one: it relays articles, some of which report
economic releases after the fact, with no schedule, consensus forecast or structured
values. Atlas therefore does not derive a calendar from news (D-077).

No calendar provider is connected. The storage schema (``economic_events``,
``economic_event_revisions``, migration 0008) and this interface exist so a provider can
be added later without changing the core; choosing one (and any paid subscription) is a
separate decision. Until then :func:`calendar_status` reports ``NOT_CONFIGURED`` and no
endpoint returns calendar entries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.data_engine.schemas import UtcDatetime
from app.news.enums import CalendarAvailability, EconomicImportance


class EconomicValue(BaseModel):
    """A released, expected or prior figure, exactly as the source reports it."""

    model_config = ConfigDict(frozen=True)

    value: Decimal | None = None  # None when the source gives no number
    unit: str | None = None  # e.g. "%", "K", "USD bn"
    text: str | None = None  # verbatim representation, e.g. "3.2%"


class ProviderEconomicEvent(BaseModel):
    """One observation of one scheduled release from a calendar provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    provider_event_id: str = Field(min_length=1, max_length=128)
    name: str
    country: str | None = None  # ISO 3166-1 alpha-2 where the source gives one
    currency: str | None = None  # ISO 4217
    scheduled_at: UtcDatetime
    previous: EconomicValue | None = None
    forecast: EconomicValue | None = None
    actual: EconomicValue | None = None
    # The source's own label; UNSPECIFIED when it gives none. Atlas never infers one.
    importance: EconomicImportance = EconomicImportance.UNSPECIFIED
    source: str | None = None
    provider_updated_at: UtcDatetime | None = None
    received_at: UtcDatetime


class EconomicCalendarProvider(ABC):
    provider_id: ClassVar[str]
    display_name: ClassVar[str]

    @abstractmethod
    def fetch_events(self, start: datetime, end: datetime) -> AsyncIterator[ProviderEconomicEvent]:
        """Scheduled events in ``[start, end]`` as the provider currently reports them."""


class CalendarStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    availability: CalendarAvailability
    provider: str | None
    reason: str
    notes: tuple[str, ...]


# Registry of calendar adapters (provider id -> class). Deliberately empty (D-077).
CALENDAR_PROVIDERS: dict[str, type[EconomicCalendarProvider]] = {}


def calendar_status() -> CalendarStatus:
    if not CALENDAR_PROVIDERS:
        return CalendarStatus(
            availability=CalendarAvailability.NOT_CONFIGURED,
            provider=None,
            reason=(
                "No economic-calendar provider is connected. Atlas has no independently "
                "verified source of scheduled releases, forecasts or actual values."
            ),
            notes=(
                "Alpaca's news feed is not an economic calendar: it carries articles, some "
                "about economic releases after they happen, without schedules, consensus "
                "forecasts or structured values.",
                "Macroeconomic, inflation, employment and central-bank news is classified "
                "from articles (keyword estimates), which is not a substitute for a calendar.",
                "Selecting a calendar provider, and any subscription it needs, is a separate "
                "decision; the storage schema and provider interface are ready for it.",
            ),
        )
    names = ", ".join(sorted(CALENDAR_PROVIDERS))
    return CalendarStatus(
        availability=CalendarAvailability.AVAILABLE,
        provider=names,
        reason="A calendar provider is registered.",
        notes=(),
    )


__all__ = [
    "CALENDAR_PROVIDERS",
    "CalendarStatus",
    "EconomicCalendarProvider",
    "EconomicValue",
    "ProviderEconomicEvent",
    "calendar_status",
]
