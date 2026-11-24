"""Provider-independent market-data configuration (Phase 2).

Variable names follow the Phase 2 brief (``MARKET_DATA_*``, ``HISTORICAL_DATA_*``)
rather than the ``ATLAS_`` prefix; see decision D-014. Provider-specific settings,
including credentials, live in the provider's own module (e.g. ``ALPACA_*``).

Streaming is disabled by default: Atlas starts safely without credentials and
only connects to a provider when explicitly configured.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)
from app.data_engine.enums import DataType, PriceAdjustment, StreamChannel
from app.data_engine.timeframes import Timeframe

MARKET_DATA_PREFIXES = ("MARKET_DATA_", "HISTORICAL_DATA_")

# Atlas symbol grammar: upper-case letters/digits plus '.', '-', '/' (e.g. BRK.B, EUR/USD).
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9./-]{0,31}$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


class MarketDataSettings(BaseSettings):
    """Validated market-data settings. Construct via :func:`get_market_data_settings`."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # Provider identifier; resolved against the provider registry when the provider is built.
    market_data_provider: str = "alpaca"
    # Initial universe (provider-independent Atlas symbols). Empty until configured.
    market_data_symbols: Annotated[tuple[str, ...], NoDecode] = ()
    market_data_stream_enabled: bool = False
    # Upper bound, in seconds, of the exponential reconnect backoff.
    market_data_reconnect_max_delay: float = Field(default=60.0, gt=0, le=3600)
    # Seconds without any frame from the stream (data or WebSocket pong) before the
    # connection is considered dead and replaced.
    market_data_heartbeat_timeout: float = Field(default=30.0, gt=0, le=600)
    # Live channels to subscribe for every configured symbol (provider-independent names).
    market_data_stream_channels: Annotated[tuple[StreamChannel, ...], NoDecode] = (
        StreamChannel.BARS,
        StreamChannel.UPDATED_BARS,
        StreamChannel.TRADES,
        StreamChannel.QUOTES,
    )
    # Received frames buffered between the socket and the database writer. When full, the
    # receiver waits briefly, then discards and records the loss (never silently).
    market_data_stream_queue_size: int = Field(default=10_000, ge=10, le=1_000_000)
    # During the regular session, seconds without a persisted observation before the
    # stream is reported STALE. Not applied when the market is closed.
    market_data_stream_stale_after: float = Field(default=180.0, ge=10, le=3600)
    # Records requested per historical page; the provider's own maximum is checked separately.
    historical_data_batch_size: int = Field(default=1000, ge=1, le=100_000)
    historical_data_max_retries: int = Field(default=5, ge=0, le=20)
    # Price adjustment requested from providers. RAW = as traded (decision D-020).
    historical_data_adjustment: PriceAdjustment = PriceAdjustment.RAW
    # Safety bound on pages per symbol request; exceeding it fails the symbol loudly.
    historical_data_max_pages: int = Field(default=1000, ge=1, le=100_000)

    # ---- Deliverable 2D: completeness monitoring and recovery
    # Off by default: when enabled, the recovery worker calls the provider's history.
    market_data_recovery_enabled: bool = False
    # Seconds between recovery cycles (detect gaps, run due jobs).
    market_data_recovery_interval: float = Field(default=60.0, ge=1, le=3600)
    # Bar timeframes whose completeness is tracked for MARKET_DATA_SYMBOLS.
    market_data_recovery_timeframes: Annotated[tuple[str, ...], NoDecode] = ("1Min", "1Day")
    # Data types recovered from the provider's history. Bars and trades by default; quotes
    # (very high volume) only when listed. Unrecovered gaps stay reported as missing.
    market_data_recovery_data_types: Annotated[tuple[DataType, ...], NoDecode] = (
        DataType.BAR,
        DataType.TRADE,
    )
    # Days back (New York calendar) that the completeness scan examines.
    market_data_completeness_lookback_days: int = Field(default=5, ge=0, le=90)  # 0 = today
    # Seconds after an interval ends before its history is requested: providers publish
    # late trades and bar updates after the interval closes.
    market_data_recovery_settle_seconds: float = Field(default=120.0, ge=30, le=86_400)
    # Attempts per recovery job before it is FAILED (backoff between attempts).
    market_data_recovery_max_attempts: int = Field(default=5, ge=1, le=50)
    # Window size for reconciling live trades against the provider's trade history.
    market_data_trade_reconciliation_minutes: int = Field(default=30, ge=5, le=390)
    # A live quote or trade delivered this many seconds after its event is stored STALE
    # (and a new bar this late after its interval is flagged LATE_ARRIVAL). 0 disables.
    market_data_stale_observation_seconds: float = Field(default=60.0, ge=0, le=86_400)

    @field_validator("market_data_recovery_timeframes", mode="before")
    @classmethod
    def _parse_timeframes(cls, value: object) -> object:
        items = _items(value)
        if items is None:
            return value
        try:
            parsed = tuple(str(Timeframe.parse(item)) for item in items)
        except ValueError as exc:
            raise ValueError(f"MARKET_DATA_RECOVERY_TIMEFRAMES: {exc}") from None
        if len(set(parsed)) != len(parsed):
            raise ValueError("duplicate timeframe(s) in MARKET_DATA_RECOVERY_TIMEFRAMES")
        return parsed

    @field_validator("market_data_recovery_data_types", mode="before")
    @classmethod
    def _parse_data_types(cls, value: object) -> object:
        items = _items(value)
        if items is None:
            return value
        # "bars"/"trades"/"quotes" (or the singular enum values).
        names = {"bar": DataType.BAR, "trade": DataType.TRADE, "quote": DataType.QUOTE}
        keys = [item.lower().removesuffix("s") for item in items]
        unknown = [item for item, key in zip(items, keys, strict=True) if key not in names]
        if unknown:
            raise ValueError(
                f"unknown data type(s) in MARKET_DATA_RECOVERY_DATA_TYPES: {', '.join(unknown)} "
                "(known: bars, trades, quotes)"
            )
        parsed = tuple(names[key] for key in keys)
        if len(set(parsed)) != len(parsed):
            raise ValueError("duplicate data type(s) in MARKET_DATA_RECOVERY_DATA_TYPES")
        return parsed

    @field_validator("historical_data_adjustment", mode="before")
    @classmethod
    def _normalise_adjustment(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper().replace("-", "_")
        return value

    @field_validator("market_data_provider", mode="before")
    @classmethod
    def _normalise_provider(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            if not _PROVIDER_ID.match(value):
                raise ValueError("MARKET_DATA_PROVIDER must be a lower-case provider identifier")
        return value

    @field_validator("market_data_stream_channels", mode="before")
    @classmethod
    def _parse_channels(cls, value: object) -> object:
        if isinstance(value, str):
            items = [item.strip().lower() for item in value.split(",") if item.strip()]
        elif isinstance(value, list | tuple):
            items = [str(item).strip().lower() for item in value]
        else:
            return value
        known = {c.value for c in StreamChannel}
        unknown = [item for item in items if item not in known]
        if unknown:
            raise ValueError(
                f"unknown channel(s) in MARKET_DATA_STREAM_CHANNELS: {', '.join(unknown)} "
                f"(known: {', '.join(sorted(known))})"
            )
        if len(set(items)) != len(items):
            raise ValueError("duplicate channel(s) in MARKET_DATA_STREAM_CHANNELS")
        if not items:
            raise ValueError("MARKET_DATA_STREAM_CHANNELS must name at least one channel")
        return tuple(items)

    @field_validator("market_data_symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, value: object) -> object:
        items: list[str]
        if isinstance(value, str):
            items = [item.strip().upper() for item in value.split(",") if item.strip()]
        elif isinstance(value, list | tuple):
            items = [str(item).strip().upper() for item in value]
        else:
            return value
        invalid = [item for item in items if not _SYMBOL.match(item)]
        if invalid:
            raise ValueError(f"invalid symbol(s) in MARKET_DATA_SYMBOLS: {', '.join(invalid)}")
        duplicates = sorted({item for item in items if items.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate symbol(s) in MARKET_DATA_SYMBOLS: {', '.join(duplicates)}")
        return tuple(items)


def _items(value: object) -> list[str] | None:
    """Comma-separated text or a sequence as stripped items; None for other values."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value]
    return None


@lru_cache(maxsize=1)
def get_market_data_settings() -> MarketDataSettings:
    """Load once; unknown ``MARKET_DATA_*``/``HISTORICAL_DATA_*`` keys are rejected as typos."""
    reject_unknown_keys(MARKET_DATA_PREFIXES, MarketDataSettings.model_fields)
    try:
        return MarketDataSettings()
    except ValidationError as exc:
        raise InvalidSettingsError(describe_validation_error(exc, "Market data settings")) from None
