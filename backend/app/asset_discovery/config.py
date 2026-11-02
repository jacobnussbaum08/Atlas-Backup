"""Asset discovery configuration (``ASSET_DISCOVERY_*`` variables, Phase 3A).

Operational knobs only: schedules, bounds and the names of the rule set and scoring
version to use. The qualification thresholds and ranking weights themselves are
versioned database configuration (``qualification_rule_sets``, ``scoring_versions``),
so every evaluation can name exactly the version it applied.

The worker is disabled by default: discovery calls the provider's asset directory.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from functools import lru_cache

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)
from app.data_engine.enums import PriceAdjustment

ENV_PREFIX = "ASSET_DISCOVERY_"


class AssetDiscoverySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # Background worker (python -m app.workers.asset_discovery). Off by default.
    enabled: bool = False
    # Seconds between worker cycles (claim due jobs, schedule periodic ones).
    poll_interval: float = Field(default=60.0, ge=5, le=3600)
    # Full directory listing (every status): detects deactivations and disappearances.
    full_interval_hours: float = Field(default=168.0, ge=1, le=24 * 90)
    # Active-only listing: detects new listings and changes in between full listings.
    incremental_interval_hours: float = Field(default=24.0, ge=1, le=24 * 30)
    # Single-asset lookups for the tracked universe (watchlist and qualified assets first).
    refresh_interval_hours: float = Field(default=12.0, ge=1, le=24 * 30)
    refresh_max_assets: int = Field(default=100, ge=1, le=1000)
    # Known-active instruments absent from an active-only listing are looked up one by one,
    # at most this many per job (the rest wait for the next job).
    verify_missing_max: int = Field(default=200, ge=0, le=5000)
    # Qualification + scoring pipeline cadence.
    qualification_interval_hours: float = Field(default=24.0, ge=1, le=24 * 30)
    # Mapping start assumed for instruments first discovered active whose symbol no other
    # known instrument uses: the start of Alpaca's documented stock history (as D-026).
    assumed_history_start: date = date(2016, 1, 1)
    # Directory records applied per database transaction.
    chunk_size: int = Field(default=500, ge=10, le=5000)
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    # A RUNNING job whose lease is not renewed within this time is considered abandoned.
    job_lease_seconds: float = Field(default=900.0, ge=30, le=86_400)
    # Qualification rule set and scoring version codes ("auto": us-equity-<feed>).
    rule_set: str = "auto"
    scoring_version: str = "watchlist-default"
    # Daily-bar history retrieved per qualification job for instruments that pass the
    # metadata rules but lack history (0 disables). Bounded: never the whole pool.
    history_prep_max_assets: int = Field(default=200, ge=0, le=2000)
    # Calendar days of daily bars retrieved for such instruments.
    history_prep_lookback_days: int = Field(default=130, ge=30, le=730)
    # Benchmark for relative strength (an Atlas symbol with daily history).
    benchmark_symbol: str = "SPY"
    # Price adjustment of the daily bars qualification measures (decision D-063). Returns,
    # volatility, ATR and momentum across a split are meaningless on RAW prices (a 4:1
    # split reads as -75%), so the default is SPLIT. RAW reintroduces that error.
    price_adjustment: PriceAdjustment = PriceAdjustment.SPLIT

    @field_validator("price_adjustment", mode="before")
    @classmethod
    def _normalise_adjustment(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper().replace("-", "_")
        return value

    @property
    def assumed_history_start_at(self) -> datetime:
        return datetime(
            self.assumed_history_start.year,
            self.assumed_history_start.month,
            self.assumed_history_start.day,
            tzinfo=UTC,
        )

    def rule_set_code(self, feed: str) -> str:
        return f"us-equity-{feed}" if self.rule_set == "auto" else self.rule_set


@lru_cache(maxsize=1)
def get_asset_discovery_settings() -> AssetDiscoverySettings:
    """Load once; unknown ``ASSET_DISCOVERY_*`` keys are rejected as typos."""
    known = {f"{ENV_PREFIX}{name}" for name in AssetDiscoverySettings.model_fields}
    reject_unknown_keys((ENV_PREFIX,), known)
    try:
        return AssetDiscoverySettings()
    except ValidationError as exc:
        raise InvalidSettingsError(
            describe_validation_error(exc, "Asset discovery settings")
        ) from None
