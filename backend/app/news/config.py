"""News and event intelligence settings (``NEWS_*``, Phase 3C).

Operational knobs only. The taxonomy, keyword rules and relevance formula are code
constants with a version name (``classification.py``, ``relevance.py``): every stored
classification and relevance names the version that produced it, and a change is a new
version (D-076, D-078).

Content retention is off by default: Alpaca's documentation does not state redistribution
or retention terms for the article text it relays from its news partners, so Atlas stores
headline, summary, URL and metadata only, and asks the REST API not to send content
(D-074). ``NEWS_STORE_CONTENT=true`` is for an operator who has confirmed the terms.
"""

from __future__ import annotations

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

ENV_PREFIX = "NEWS_"
WILDCARD = "*"


class NewsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # News provider (registry key). Only "alpaca" exists.
    provider: str = "alpaca"
    # Keep article bodies? Off until the provider's licence terms are confirmed (D-074).
    store_content: bool = False

    # Historical retrieval.
    historical_page_size: int = Field(default=50, ge=1, le=50)
    historical_max_pages: int = Field(default=400, ge=1, le=10_000)
    historical_max_range_days: int = Field(default=366, ge=1, le=3650)
    historical_max_retries: int = Field(default=5, ge=0, le=10)

    # Real-time stream worker.
    stream_enabled: bool = False
    # "*" (all news) or a comma-separated list of provider symbols.
    stream_symbols: Annotated[tuple[str, ...], NoDecode] = (WILDCARD,)
    stream_heartbeat_timeout: float = Field(default=30.0, ge=5, le=300)
    stream_reconnect_max_delay: float = Field(default=60.0, ge=1, le=900)
    # Status writes; health calls the stream stale after this many seconds without one.
    stream_status_interval: float = Field(default=5.0, ge=1, le=60)
    stream_stale_after: int = Field(default=60, ge=10, le=3600)
    # After every (re)subscription: retrieve [last article received - overlap, now] over REST.
    stream_recovery_overlap_seconds: int = Field(default=300, ge=0, le=86_400)
    stream_recovery_max_hours: int = Field(default=72, ge=1, le=24 * 31)

    # Asset mapping: a symbol whose mapping changed hands this close to publication is
    # AMBIGUOUS rather than forced onto either asset.
    mapping_ambiguity_window_hours: int = Field(default=72, ge=0, le=24 * 30)
    # Relevance: recency half-life used by feeds (query-time weight only).
    relevance_half_life_hours: float = Field(default=24.0, gt=0, le=24 * 365)

    @field_validator("provider", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("stream_symbols", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part for part in value.split(",")]
        if isinstance(value, list | tuple):
            symbols = [str(part).strip().upper() for part in value if str(part).strip()]
            return tuple(dict.fromkeys(symbols))
        return value

    @field_validator("stream_symbols")
    @classmethod
    def _valid_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("must name at least one symbol, or '*' for all news")
        if WILDCARD in value and len(value) > 1:
            raise ValueError("'*' (all news) cannot be combined with symbols")
        for symbol in value:
            if symbol != WILDCARD and not (1 <= len(symbol) <= 16 and symbol.isascii()):
                raise ValueError("symbols must be 1-16 ASCII characters")
        return value

    @property
    def stream_all_news(self) -> bool:
        return self.stream_symbols == (WILDCARD,)


@lru_cache(maxsize=1)
def get_news_settings() -> NewsSettings:
    """Load once. Unknown ``NEWS_*`` keys are typos; errors never include input values."""
    known = {f"{ENV_PREFIX}{name}".upper() for name in NewsSettings.model_fields}
    reject_unknown_keys((ENV_PREFIX,), known)
    try:
        return NewsSettings()
    except ValidationError as exc:
        raise InvalidSettingsError(describe_validation_error(exc, "News settings")) from None


__all__ = ["WILDCARD", "NewsSettings", "get_news_settings"]
