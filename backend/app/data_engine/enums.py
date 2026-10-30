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


class PriceAdjustment(StrEnum):
    """Corporate-action adjustment applied by the provider to historical prices.

    Observations with different adjustments are different series and are never mixed.
    """

    RAW = "RAW"  # as traded; no split/dividend adjustment
    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"
    SPIN_OFF = "SPIN_OFF"
    ALL = "ALL"


class FeedCoverage(StrEnum):
    """How much of the market a feed represents. Never treat SINGLE_VENUE as the market."""

    SINGLE_VENUE = "SINGLE_VENUE"  # one exchange, e.g. IEX
    CONSOLIDATED = "CONSOLIDATED"  # all exchanges via the SIPs (CTA + UTP)


class DataSource(StrEnum):
    """Provenance of a record. Historical and live data share one schema."""

    HISTORICAL_REST = "HISTORICAL_REST"
    LIVE_STREAM = "LIVE_STREAM"


class BarObservationKind(StrEnum):
    """How a bar observation was obtained. Only ``LIVE_*`` bars come from a stream.

    * ``HISTORICAL``: a REST snapshot; may later be revised by the provider.
    * ``LIVE_INITIAL``: the first emission of a minute bar, just after its interval ended.
      Not final: late trades can produce an ``LIVE_UPDATED`` emission.
    * ``LIVE_UPDATED``: a re-emission of an already published bar after late trades.
    * ``LIVE_RUNNING``: a running bar whose interval has not ended (e.g. the daily bar).
    """

    HISTORICAL = "HISTORICAL"
    LIVE_INITIAL = "LIVE_INITIAL"
    LIVE_UPDATED = "LIVE_UPDATED"
    LIVE_RUNNING = "LIVE_RUNNING"


class StreamChannel(StrEnum):
    """Provider-independent live channels."""

    BARS = "bars"  # minute bars
    UPDATED_BARS = "updated_bars"  # corrections of already published minute bars
    DAILY_BARS = "daily_bars"  # running daily bar
    TRADES = "trades"
    QUOTES = "quotes"


class StreamAuthState(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    PENDING = "PENDING"
    AUTHENTICATED = "AUTHENTICATED"
    REJECTED = "REJECTED"


class StreamSubscriptionState(StrEnum):
    NONE = "NONE"
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"  # provider confirmed every requested channel/symbol
    PARTIAL = "PARTIAL"  # provider confirmed only part of the request
    REJECTED = "REJECTED"


class StreamHealth(StrEnum):
    """Derived health of a stream, as reported by the API. ``HEALTHY`` is never assumed."""

    DISABLED = "DISABLED"  # streaming is switched off in configuration
    NOT_RUNNING = "NOT_RUNNING"  # no worker heartbeat (never started, stopped or crashed)
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    IDLE = "IDLE"  # subscribed, quiet, and quiet is expected (market closed / sparse session)
    STALE = "STALE"  # subscribed but no data when data is expected
    DEGRADED = "DEGRADED"  # running but losing data, failing to persist, or partially subscribed
    RECONNECTING = "RECONNECTING"
    FAILED = "FAILED"  # permanent failure (e.g. credentials rejected); not retrying


class StreamGapKind(StrEnum):
    """Why live data may be missing for an interval."""

    CONNECTION_LOST = "CONNECTION_LOST"  # the stream disconnected and was later restored
    WORKER_NOT_RUNNING = "WORKER_NOT_RUNNING"  # no worker was streaming (stopped or crashed)
    DATA_DROPPED = "DATA_DROPPED"  # frames were received but discarded under backpressure
    # Frames were received but never persisted (the worker died with them in memory).
    DATA_NOT_PERSISTED = "DATA_NOT_PERSISTED"


class GapRecoveryStatus(StrEnum):
    """Backfill state of a gap. 2C only records PENDING; recovery is Deliverable 2D."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    RECOVERED = "RECOVERED"
    UNRECOVERABLE = "UNRECOVERABLE"
    # 2D: some affected series were recovered, others remain missing (see market_data_gaps).
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"


class DataQualityStatus(StrEnum):
    UNCHECKED = "UNCHECKED"
    VALID = "VALID"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    MISSING = "MISSING"
    INVALID = "INVALID"
    QUARANTINED = "QUARANTINED"
    # Interval states (2D): data that was missing and has been retrieved from the provider's
    # history, and data the provider cannot supply.
    RECOVERED = "RECOVERED"
    UNAVAILABLE = "UNAVAILABLE"


class DataQualityEventType(StrEnum):
    MISSING_INTERVAL = "MISSING_INTERVAL"
    STALE_DATA = "STALE_DATA"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    OUT_OF_ORDER_EVENT = "OUT_OF_ORDER_EVENT"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_OHLC = "INVALID_OHLC"
    INVALID_QUOTE = "INVALID_QUOTE"
    MISSING_FIELD = "MISSING_FIELD"
    REJECTED_MESSAGE = "REJECTED_MESSAGE"
    UNEXPECTED_SYMBOL = "UNEXPECTED_SYMBOL"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    RECOVERY = "RECOVERY"
    CORRECTION = "CORRECTION"
    CAPABILITY_LIMITATION = "CAPABILITY_LIMITATION"
    INVALID_VOLUME = "INVALID_VOLUME"
    OUT_OF_REQUESTED_RANGE = "OUT_OF_REQUESTED_RANGE"
    TIMEFRAME_MISALIGNED = "TIMEFRAME_MISALIGNED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    NO_DATA = "NO_DATA"  # provider answered successfully with no records
    PROVIDER_ERROR = "PROVIDER_ERROR"
    TRADE_CANCEL = "TRADE_CANCEL"  # provider cancelled or flagged an earlier trade as an error
    DATA_DROPPED = "DATA_DROPPED"  # Atlas could not keep up and discarded received data
    # Deliverable 2D
    DATA_NOT_PERSISTED = "DATA_NOT_PERSISTED"  # received, then lost before it was written
    LATE_ARRIVAL = "LATE_ARRIVAL"  # delivered much later than its interval ended
    UNEXPECTED_PRICE_CHANGE = "UNEXPECTED_PRICE_CHANGE"  # flagged for review, never rejected
    SEQUENCE_GAP = "SEQUENCE_GAP"  # provider sequence numbers skipped values
    STREAM_MISSED_DATA = "STREAM_MISSED_DATA"  # provider history holds data the stream lacked
    UNRECOVERABLE_INTERVAL = "UNRECOVERABLE_INTERVAL"  # the provider cannot supply the interval


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class IngestionJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"  # every symbol finished without error (some may have had no data)
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"  # at least one symbol failed, at least one did not
    FAILED = "FAILED"  # every symbol failed, or the job could not run
    CANCELLED = "CANCELLED"


class IngestionSymbolStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"  # every page retrieved; at least one bar
    NO_DATA = "NO_DATA"  # every page retrieved; the provider returned no bars
    FAILED = "FAILED"


# ---------------------------------------------------------------- Deliverable 2D


class CoverageEvidence(StrEnum):
    """Why Atlas believes an interval of a series is complete.

    * ``REST_RETRIEVAL``: the provider's history for the interval was retrieved in full
      (every page). Minutes without a bar inside it had no eligible activity.
    * ``LIVE_STREAM``: the stream was subscribed, nothing was dropped and every received
      frame was persisted. Accepted for bars (the provider aggregates bars from every trade
      on its side); **not** proof for trades/quotes, which the stream can skip (observed on
      IEX at the 2026-10-30 open).
    """

    REST_RETRIEVAL = "REST_RETRIEVAL"
    LIVE_STREAM = "LIVE_STREAM"


class GapCause(StrEnum):
    CONNECTION_LOST = "CONNECTION_LOST"
    WORKER_NOT_RUNNING = "WORKER_NOT_RUNNING"
    DATA_DROPPED = "DATA_DROPPED"
    DATA_NOT_PERSISTED = "DATA_NOT_PERSISTED"
    UNCOVERED = "UNCOVERED"  # expected data with no coverage evidence (completeness scan)
    SEQUENCE_GAP = "SEQUENCE_GAP"  # provider sequence numbers skipped values
    STREAM_MISSED_DATA = "STREAM_MISSED_DATA"  # reconciliation found data the stream lacked
    REQUESTED = "REQUESTED"  # an operator asked for the interval to be verified


class GapStatus(StrEnum):
    OPEN = "OPEN"  # missing; recovery not yet attempted or waiting to retry
    RECOVERING = "RECOVERING"  # a recovery job is running for it
    RECOVERED = "RECOVERED"  # the provider history for the whole interval was retrieved
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"  # part retrieved; the rest is a new gap
    UNAVAILABLE = "UNAVAILABLE"  # the provider cannot supply it (permanent); stays missing


UNRESOLVED_GAP_STATUSES = frozenset({GapStatus.OPEN, GapStatus.RECOVERING})


class RecoveryJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"  # every requested interval verified against the provider history
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"  # part verified; the remainder is reported
    FAILED = "FAILED"  # nothing verified after the last allowed attempt, or permanent error


class RecoveryJobKind(StrEnum):
    RECOVERY = "RECOVERY"  # retrieve a known gap from the provider's history
    # Verify an interval that was collected live against the provider's history; data the
    # stream skipped is stored and reported as a STREAM_MISSED_DATA gap.
    RECONCILIATION = "RECONCILIATION"
