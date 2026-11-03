"""Strategy Laboratory settings (``STRATEGY_*``, Phase 4A).

Operational knobs only. Strategy parameters are not settings: they are validated,
versioned configurations stored in PostgreSQL (``strategy_configurations``), changed
through the API with an operator token.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)
from app.data_engine.enums import PriceAdjustment

ENV_PREFIX = "STRATEGY_"


class StrategySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # Evaluation worker (python -m app.workers.strategy_evaluation). Off by default.
    worker_enabled: bool = False
    worker_interval_seconds: float = Field(default=60.0, ge=5, le=3600)
    # Evaluations running at the same time, and the time one may take.
    max_concurrency: int = Field(default=4, ge=1, le=32)
    evaluation_timeout_seconds: float = Field(default=60.0, ge=1, le=900)
    # A RUNNING evaluation whose lease expired belongs to a stopped worker and is failed
    # (INTERRUPTED) before a retry; failed attempts per update before giving up.
    lease_seconds: int = Field(default=300, ge=10, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    # Daily bars: the adjusted series (as Phase 3B) and the delay after the close before a
    # session's bar counts as completed and settled.
    price_adjustment: PriceAdjustment = PriceAdjustment.SPLIT
    settle_seconds: int = Field(default=3600, ge=0, le=86_400)
    # News events older than this when the worker starts are not re-triggered.
    news_trigger_lookback_minutes: int = Field(default=60, ge=0, le=24 * 60)

    @field_validator("price_adjustment", mode="before")
    @classmethod
    def _normalise_adjustment(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper().replace("-", "_")
        return value

    @model_validator(mode="after")
    def _lease_covers_timeout(self) -> StrategySettings:
        if self.lease_seconds <= self.evaluation_timeout_seconds:
            raise ValueError("lease_seconds must exceed evaluation_timeout_seconds")
        return self


@lru_cache(maxsize=1)
def get_strategy_settings() -> StrategySettings:
    """Load once. Unknown ``STRATEGY_*`` keys are typos; errors never include values."""
    known = {f"{ENV_PREFIX}{name}".upper() for name in StrategySettings.model_fields}
    reject_unknown_keys((ENV_PREFIX,), known)
    try:
        return StrategySettings()
    except ValidationError as exc:
        raise InvalidSettingsError(describe_validation_error(exc, "Strategy settings")) from None


__all__ = ["StrategySettings", "get_strategy_settings"]
