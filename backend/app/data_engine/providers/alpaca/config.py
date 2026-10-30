"""Alpaca market-data configuration (``ALPACA_*`` variables).

Credentials are optional so Atlas starts without them. A half-configured pair
(key without secret, or the reverse) is a configuration error. Values are held
as ``SecretStr`` and never appear in ``repr``, logs, exceptions or API output.

There is deliberately no base-URL setting: the adapter only talks to Alpaca's
documented market-data hosts, never to trading or brokerage hosts.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)

ENV_PREFIX = "ALPACA_"


class AlpacaFeed(StrEnum):
    IEX = "iex"
    SIP = "sip"
    DELAYED_SIP = "delayed_sip"


class AlpacaDataPlan(StrEnum):
    """The Alpaca market-data subscription the credentials belong to."""

    BASIC = "basic"
    ALGO_TRADER_PLUS = "algo_trader_plus"


class AlpacaSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    api_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    data_feed: AlpacaFeed = AlpacaFeed.IEX
    data_plan: AlpacaDataPlan = AlpacaDataPlan.BASIC
    # Seconds allowed for one HTTP request to the market-data API.
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    @field_validator("api_key", "secret_key", mode="before")
    @classmethod
    def _empty_is_missing(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("api_key", "secret_key")
    @classmethod
    def _well_formed(cls, value: SecretStr | None) -> SecretStr | None:
        # Messages describe the problem only; the value itself is never echoed.
        if value is None:
            return None
        raw = value.get_secret_value()
        if raw != raw.strip() or any(ch.isspace() for ch in raw):
            raise ValueError("must not contain whitespace")
        if not 8 <= len(raw) <= 256:
            raise ValueError("has an implausible length")
        if not raw.isascii() or not raw.isprintable():
            raise ValueError("must be printable ASCII")
        return value

    @model_validator(mode="after")
    def _complete_pair(self) -> Self:
        if (self.api_key is None) != (self.secret_key is None):
            raise ValueError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must both be set or both be empty"
            )
        return self

    @property
    def credentials_configured(self) -> bool:
        return self.api_key is not None and self.secret_key is not None


@lru_cache(maxsize=1)
def get_alpaca_settings() -> AlpacaSettings:
    """Load once. Unknown ``ALPACA_*`` keys are typos; errors never include input values."""
    known = {f"{ENV_PREFIX}{name}" for name in AlpacaSettings.model_fields}
    reject_unknown_keys((ENV_PREFIX,), known)
    try:
        return AlpacaSettings()
    except ValidationError as exc:
        raise InvalidSettingsError(describe_validation_error(exc, "Alpaca settings")) from None
