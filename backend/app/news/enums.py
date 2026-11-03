"""Enumerations of the news and event intelligence domain (Phase 3C)."""

from __future__ import annotations

from enum import StrEnum


class ProviderKind(StrEnum):
    """What a provider registered in ``news_providers`` supplies."""

    NEWS = "NEWS"
    ECONOMIC_CALENDAR = "ECONOMIC_CALENDAR"


class ArticleOrigin(StrEnum):
    """How Atlas obtained one observation of an article."""

    REALTIME_STREAM = "REALTIME_STREAM"  # pushed over the provider's live connection
    HISTORICAL_REST = "HISTORICAL_REST"  # retrieved later through the REST endpoint


class RevisionReason(StrEnum):
    """Why a new revision of an article was stored."""

    FIRST_OBSERVATION = "FIRST_OBSERVATION"
    PROVIDER_UPDATE = "PROVIDER_UPDATE"  # the provider's updated_at advanced
    CONTENT_CHANGED = "CONTENT_CHANGED"  # fields differ, updated_at unchanged
    LATE_EARLIER_VERSION = "LATE_EARLIER_VERSION"  # an older version arrived after a newer one


class MappingStatus(StrEnum):
    """Outcome of mapping one provider symbol of an article to an Atlas asset."""

    MAPPED = "MAPPED"
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"  # Atlas has never mapped this symbol at this provider
    INVALID_SYMBOL = "INVALID_SYMBOL"  # not a symbol form of the namespace (e.g. TSX:CPX)
    NOT_EFFECTIVE = "NOT_EFFECTIVE"  # mappings exist, but none was valid at publication
    AMBIGUOUS = "AMBIGUOUS"  # the symbol changed hands near publication; not forced


class NewsJobKind(StrEnum):
    HISTORICAL_BACKFILL = "HISTORICAL_BACKFILL"  # requested by an operator or the CLI
    STREAM_RECOVERY = "STREAM_RECOVERY"  # overlap window after a stream (re)connection


class NewsJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_JOB_STATUSES = (NewsJobStatus.PENDING, NewsJobStatus.RUNNING)


class EventCategory(StrEnum):
    """Event taxonomy ``news-taxonomy-v1``. Order is the tie-break precedence."""

    EARNINGS = "EARNINGS"
    ANALYST_RATINGS = "ANALYST_RATINGS"
    MERGERS_ACQUISITIONS = "MERGERS_ACQUISITIONS"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    MANAGEMENT_CHANGES = "MANAGEMENT_CHANGES"
    PRODUCT_LAUNCHES = "PRODUCT_LAUNCHES"
    REGULATORY = "REGULATORY"
    LITIGATION = "LITIGATION"
    CENTRAL_BANKS = "CENTRAL_BANKS"
    INFLATION = "INFLATION"
    EMPLOYMENT = "EMPLOYMENT"
    MACROECONOMIC = "MACROECONOMIC"
    GEOPOLITICAL = "GEOPOLITICAL"
    CRYPTOCURRENCY = "CRYPTOCURRENCY"
    INDUSTRY = "INDUSTRY"
    COMPANY_ANNOUNCEMENTS = "COMPANY_ANNOUNCEMENTS"
    OTHER = "OTHER"


# Categories about the economy or markets as a whole rather than one company.
MARKET_WIDE_CATEGORIES = frozenset(
    {
        EventCategory.CENTRAL_BANKS,
        EventCategory.INFLATION,
        EventCategory.EMPLOYMENT,
        EventCategory.MACROECONOMIC,
        EventCategory.GEOPOLITICAL,
    }
)


class ClassificationSource(StrEnum):
    """Who assigned a category."""

    PROVIDER = "PROVIDER"  # the provider's own label, stored verbatim and mapped
    ATLAS = "ATLAS"  # Atlas's deterministic rules


class ClassificationMethod(StrEnum):
    PROVIDER_METADATA = "PROVIDER_METADATA"
    KEYWORD_RULES = "KEYWORD_RULES"
    SYMBOL_RULES = "SYMBOL_RULES"  # e.g. a crypto pair among the article's symbols
    DEFAULT = "DEFAULT"  # nothing matched: OTHER


class ClassificationConfidence(StrEnum):
    """How strong the *evidence* for a rule-based label is. Not a probability, and never
    verification: keyword rules cannot exceed MEDIUM (D-076)."""

    MEDIUM = "MEDIUM"  # matched in the headline, or several summary matches
    LOW = "LOW"  # a single match outside the headline
    NONE = "NONE"  # the OTHER fallback: no evidence at all


class VerificationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"  # every Atlas rule-based label
    PROVIDER_ASSERTED = "PROVIDER_ASSERTED"  # the provider says so; Atlas has not checked


class EventScope(StrEnum):
    ASSET = "ASSET"  # at least one mapped Atlas asset
    MARKET = "MARKET"  # no mapped asset, market-wide category
    UNLINKED = "UNLINKED"  # no mapped asset, company-type or unclassified news


class AssetRelationship(StrEnum):
    DIRECT_PROVIDER_SYMBOL = "DIRECT_PROVIDER_SYMBOL"  # the provider tagged the symbol


class SourceReliability(StrEnum):
    """Atlas has no basis for rating publishers yet; nothing is invented."""

    NOT_ASSESSED = "NOT_ASSESSED"


class EconomicImportance(StrEnum):
    """A calendar provider's own importance label, stored as reported (never inferred)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNSPECIFIED = "UNSPECIFIED"


class CalendarAvailability(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AVAILABLE = "AVAILABLE"
