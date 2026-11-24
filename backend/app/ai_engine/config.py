"""AI decision engine and LLM settings (``AI_*``, Phase 5B).

Operational knobs only. Scoring weights, thresholds and decision rules are not settings:
they are a versioned :class:`~app.ai_engine.coordinator.policy.DecisionPolicy`, stored
with every run that used it.

**LLM spending is off and bounded by default.** ``AI_LLM_PROVIDER=none`` makes every LLM
analysis UNAVAILABLE (deterministic components follow their documented fallback). A
provider needs a model, a key and **prices**: without ``AI_LLM_INPUT_USD_PER_MTOK`` and
``AI_LLM_OUTPUT_USD_PER_MTOK`` the spending limits cannot be enforced, so every call is
refused (UNPRICED) rather than made blind. Atlas never switches to another model on its
own: the configured model is the only one called.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.ai_engine.enums import ProviderKind
from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)

ENV_PREFIX = "AI_"
# "AI_" alone is too common a prefix (other tools set AI_* variables): typo detection covers
# only the sub-namespaces Atlas owns.
OWNED_PREFIXES = ("AI_LLM_", "AI_NEWS_", "AI_DECISION", "AI_CONFLICT_")
ANTHROPIC_DEFAULT_URL = "https://api.anthropic.com"


class AISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # ------------------------------------------------------------ LLM provider
    llm_provider: ProviderKind = ProviderKind.NONE
    # The exact model identifier sent to the provider (never substituted).
    llm_model: str = Field(default="", max_length=128)
    llm_api_key: SecretStr | None = None
    # Anthropic: defaults to the public API. OpenAI-compatible: required (".../v1").
    llm_base_url: AnyHttpUrl | None = None
    # Prices in USD per million tokens, and where they came from (shown with every cost).
    llm_input_usd_per_mtok: Decimal | None = Field(default=None, ge=0, le=1000)
    llm_output_usd_per_mtok: Decimal | None = Field(default=None, ge=0, le=1000)
    llm_pricing_source: str = Field(default="", max_length=256)

    # ------------------------------------------------------------ spending and bounds
    llm_daily_budget_usd: Decimal = Field(default=Decimal("0.50"), ge=0, le=1000)
    llm_monthly_budget_usd: Decimal = Field(default=Decimal("5.00"), ge=0, le=10000)
    llm_max_requests_per_day: int = Field(default=200, ge=0, le=100_000)
    # Upper bound on one request's input (estimated before sending) and output tokens.
    llm_max_input_tokens: int = Field(default=6000, ge=256, le=200_000)
    llm_max_output_tokens: int = Field(default=1500, ge=64, le=16_000)
    llm_max_concurrency: int = Field(default=2, ge=1, le=16)
    llm_timeout_seconds: float = Field(default=30.0, ge=1, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # Articles per NEWS_ANALYSIS request (prompt-level batching).
    llm_batch_size: int = Field(default=5, ge=1, le=20)
    # Consecutive provider failures before the service pauses, and for how long.
    llm_failure_threshold: int = Field(default=3, ge=1, le=50)
    llm_cooldown_seconds: float = Field(default=300.0, ge=0, le=86_400)
    # Characters of an article's text sent at most (headline + summary, or body if stored).
    llm_max_article_chars: int = Field(default=2000, ge=200, le=50_000)

    # ------------------------------------------------------------ workers
    news_analysis_enabled: bool = False
    news_analysis_interval_seconds: float = Field(default=300.0, ge=10, le=86_400)
    news_analysis_lookback_hours: int = Field(default=48, ge=1, le=24 * 30)
    news_analysis_max_articles_per_cycle: int = Field(default=20, ge=1, le=1000)
    decisions_enabled: bool = False
    decisions_interval_seconds: float = Field(default=900.0, ge=30, le=86_400)
    conflict_explanations_enabled: bool = False
    # A decision policy JSON file; unset: the documented default policy.
    decision_policy_file: Path | None = None

    @field_validator("llm_api_key", "llm_base_url", "decision_policy_file", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("llm_input_usd_per_mtok", "llm_output_usd_per_mtok", mode="before")
    @classmethod
    def _empty_price_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _provider_complete(self) -> Self:
        if self.llm_provider in (ProviderKind.ANTHROPIC, ProviderKind.OPENAI_COMPATIBLE):
            if not self.llm_model:
                raise ValueError("AI_LLM_MODEL is required when an LLM provider is configured")
            if self.llm_api_key is None:
                raise ValueError("AI_LLM_API_KEY is required when an LLM provider is configured")
        if self.llm_provider is ProviderKind.OPENAI_COMPATIBLE and self.llm_base_url is None:
            raise ValueError("AI_LLM_BASE_URL is required for an OpenAI-compatible provider")
        if self.llm_provider is ProviderKind.FIXTURE:
            raise ValueError("the fixture provider exists for tests only")
        return self

    @property
    def priced(self) -> bool:
        return self.llm_input_usd_per_mtok is not None and self.llm_output_usd_per_mtok is not None

    def base_url(self) -> str:
        if self.llm_base_url is not None:
            return str(self.llm_base_url).rstrip("/")
        return ANTHROPIC_DEFAULT_URL


@lru_cache(maxsize=1)
def get_ai_settings() -> AISettings:
    """Load once. Unknown ``AI_*`` keys are typos; errors never include values."""
    known = {f"{ENV_PREFIX}{name}".upper() for name in AISettings.model_fields}
    reject_unknown_keys(OWNED_PREFIXES, known)
    try:
        return AISettings()
    except ValidationError as exc:
        raise InvalidSettingsError(describe_validation_error(exc, "AI settings")) from None


__all__ = ["ANTHROPIC_DEFAULT_URL", "OWNED_PREFIXES", "AISettings", "get_ai_settings"]
