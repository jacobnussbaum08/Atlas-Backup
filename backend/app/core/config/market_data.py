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
    # Seconds without any frame from the stream before the connection is considered dead.
    market_data_heartbeat_timeout: float = Field(default=30.0, gt=0, le=600)
    # Records requested per historical page; the provider's own maximum is checked separately.
    historical_data_batch_size: int = Field(default=1000, ge=1, le=100_000)
    historical_data_max_retries: int = Field(default=5, ge=0, le=20)

    @field_validator("market_data_provider", mode="before")
    @classmethod
    def _normalise_provider(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            if not _PROVIDER_ID.match(value):
                raise ValueError("MARKET_DATA_PROVIDER must be a lower-case provider identifier")
        return value

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


@lru_cache(maxsize=1)
def get_market_data_settings() -> MarketDataSettings:
    """Load once; unknown ``MARKET_DATA_*``/``HISTORICAL_DATA_*`` keys are rejected as typos."""
    reject_unknown_keys(MARKET_DATA_PREFIXES, MarketDataSettings.model_fields)
    try:
        return MarketDataSettings()
    except ValidationError as exc:
        raise InvalidSettingsError(describe_validation_error(exc, "Market data settings")) from None
