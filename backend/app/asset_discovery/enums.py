"""Enumerations of the asset discovery and universe domain (Phase 3A)."""

from __future__ import annotations

from enum import StrEnum


class InstrumentType(StrEnum):
    """What kind of instrument an asset is, as far as Atlas actually knows.

    Alpaca's directory reports every US exchange-listed security as ``us_equity``: it does
    not separate common stock from ETFs, preferreds, warrants, units or rights. Assets
    discovered there are ``UNCLASSIFIED`` until a source that knows (the operator reference
    file today, a reference-data provider later) says otherwise. Atlas never guesses the
    type from the name or the ticker.
    """

    STOCK = "STOCK"
    ETF = "ETF"
    UNCLASSIFIED = "UNCLASSIFIED"


class ClassificationSource(StrEnum):
    OPERATOR_REFERENCE = "OPERATOR_REFERENCE"
    PROVIDER = "PROVIDER"
    NONE = "NONE"


class ValidFromBasis(StrEnum):
    """Why a provider symbol mapping starts when it starts.

    * ``OPERATOR_REFERENCE``: an operator asserted it (``reference_assets.toml``).
    * ``PROVIDER_HISTORY_ASSUMED``: an instrument active in the directory when first
      discovered, whose symbol no other known instrument uses; the mapping is assumed to
      cover the provider's history (Alpaca maps renamed tickers to today's instrument).
    * ``OBSERVED``: Atlas only knows it from the moment it observed it (new listings,
      reactivations, symbol changes, symbols shared with other instruments).
    """

    OPERATOR_REFERENCE = "OPERATOR_REFERENCE"
    PROVIDER_HISTORY_ASSUMED = "PROVIDER_HISTORY_ASSUMED"
    OBSERVED = "OBSERVED"


class ProviderAssetStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ReferenceSource(StrEnum):
    """How a reference record was obtained."""

    DIRECTORY_FULL = "DIRECTORY_FULL"
    DIRECTORY_ACTIVE = "DIRECTORY_ACTIVE"
    SINGLE_LOOKUP = "SINGLE_LOOKUP"


class LifecycleEventType(StrEnum):
    FIRST_DISCOVERED = "FIRST_DISCOVERED"  # in the initial catalog: not a listing date
    NEW_LISTING = "NEW_LISTING"  # appeared after the initial catalog
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    NAME_CHANGE = "NAME_CHANGE"
    EXCHANGE_CHANGE = "EXCHANGE_CHANGE"
    TRADING_SUSPENDED = "TRADING_SUSPENDED"  # provider's tradable flag turned false
    TRADING_RESUMED = "TRADING_RESUMED"
    DEACTIVATED = "DEACTIVATED"
    DELISTED = "DELISTED"
    REACTIVATED = "REACTIVATED"
    MISSING_FROM_DIRECTORY = "MISSING_FROM_DIRECTORY"
    REAPPEARED_IN_DIRECTORY = "REAPPEARED_IN_DIRECTORY"
    MAPPING_CONFLICT = "MAPPING_CONFLICT"
    METADATA_CHANGE = "METADATA_CHANGE"  # e.g. shortability, margin, attributes


class DiscoveryJobKind(StrEnum):
    DIRECTORY_DISCOVERY = "DIRECTORY_DISCOVERY"
    METADATA_REFRESH = "METADATA_REFRESH"
    QUALIFICATION = "QUALIFICATION"
    WATCHLIST_SCORING = "WATCHLIST_SCORING"
    MEMBERSHIP_UPDATE = "MEMBERSHIP_UPDATE"


class DiscoveryJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"


ACTIVE_JOB_STATUSES = (DiscoveryJobStatus.PENDING, DiscoveryJobStatus.RUNNING)


class DiscoveryMode(StrEnum):
    """``FULL`` lists every status; ``INCREMENTAL`` lists active instruments only and
    verifies known-active instruments that disappeared from it one by one."""

    FULL = "FULL"
    INCREMENTAL = "INCREMENTAL"


class DiscoveryOutcome(StrEnum):
    NEW = "NEW"
    CHANGED = "CHANGED"
    REJECTED = "REJECTED"  # invalid provider record; nothing stored for it
    CONFLICT = "CONFLICT"  # valid record that contradicts known identity; needs review
    MISSING = "MISSING"  # known instrument absent from a complete listing
    VERIFIED = "VERIFIED"  # single lookup confirmed the current state


class JobTrigger(StrEnum):
    API = "API"
    SCHEDULE = "SCHEDULE"
    CLI = "CLI"
    PIPELINE = "PIPELINE"


class UniverseLayer(StrEnum):
    DISCOVERY_POOL = "DISCOVERY_POOL"
    QUALIFIED_UNIVERSE = "QUALIFIED_UNIVERSE"
    ACTIVE_WATCHLIST = "ACTIVE_WATCHLIST"


class MembershipAction(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"


class QualificationOutcome(StrEnum):
    QUALIFIED = "QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"  # a mandatory rule failed
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # nothing failed, but a mandatory input is missing


class RuleStatus(StrEnum):
    PASS = "PASS"  # noqa: S105  # rule outcome, not a credential
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"  # the input does not exist (yet); not a failure
    NOT_APPLICABLE = "NOT_APPLICABLE"  # disabled, or skipped after an earlier failure


class MetricStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # some observations, fewer than the window needs
    NO_DATA = "NO_DATA"


class SessionDayStatus(StrEnum):
    """What one expected trading session looks like in the stored daily bars."""

    TRADED = "TRADED"
    ZERO_VOLUME = "ZERO_VOLUME"  # a bar with volume 0: genuine absence of volume
    VOLUME_MISSING = "VOLUME_MISSING"  # a bar whose volume field was not reported
    QUARANTINED = "QUARANTINED"  # a bar that failed validation
    NO_REPORTED_ACTIVITY = "NO_REPORTED_ACTIVITY"  # covered by evidence, but no bar
    PROVIDER_GAP = "PROVIDER_GAP"  # a recorded gap with a provider/stream cause
    MISSING = "MISSING"  # no bar and no evidence either way
