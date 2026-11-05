"""Accounting and portfolio settings (``ACCOUNTING_*``, Phase 6A).

Operational knobs only. Nothing here can enable execution: there is no broker setting and
no execution source in the ledger (spec 24.1).
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.accounting.money import CURRENCIES, MoneyError, to_decimal
from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)

ENV_PREFIX = "ACCOUNTING_"


class AccountingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # The primary paper account created by ``python -m app.accounting.cli init``.
    primary_account_name: str = Field(default="Atlas paper account", min_length=3, max_length=128)
    functional_currency: str = "USD"
    # Simulated opening funding (spec 2: $100,000). Posted once; never a return.
    opening_funding: Decimal = Decimal("100000.00")
    # Valuation price freshness. Equities/ETFs: during the regular session a price older
    # than this is stale; outside it, the price must come from the latest session.
    equity_intraday_max_age_minutes: int = Field(default=30, ge=1, le=24 * 60)
    # Crypto trades around the clock: a price older than this is stale.
    crypto_max_age_minutes: int = Field(default=60, ge=1, le=7 * 24 * 60)
    # FX rates older than this are stale (covers a weekend with margin).
    fx_rate_max_age_hours: int = Field(default=96, ge=1, le=30 * 24)

    @field_validator("functional_currency", mode="before")
    @classmethod
    def _currency(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().upper()
            if value not in CURRENCIES:
                raise ValueError("unsupported currency")
        return value

    @field_validator("opening_funding", mode="before")
    @classmethod
    def _funding(cls, value: object) -> Decimal:
        try:
            amount = to_decimal(value, "opening funding")
        except MoneyError as exc:
            raise ValueError(str(exc)) from None
        if amount <= 0:
            raise ValueError("opening funding must be positive")
        return amount


@lru_cache(maxsize=1)
def get_accounting_settings() -> AccountingSettings:
    """Load once. Unknown ``ACCOUNTING_*`` keys are typos; errors never include values."""
    known = {f"{ENV_PREFIX}{name}".upper() for name in AccountingSettings.model_fields}
    reject_unknown_keys((ENV_PREFIX,), known)
    try:
        return AccountingSettings()
    except ValidationError as exc:
        raise InvalidSettingsError(describe_validation_error(exc, "Accounting settings")) from None


__all__ = ["AccountingSettings", "get_accounting_settings"]
