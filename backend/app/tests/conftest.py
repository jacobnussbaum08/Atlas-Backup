"""Shared test fixtures.

Unit tests never read the developer's real environment or ``.env`` file (which
may hold real provider credentials): ``clean_env`` removes ``ATLAS_*``,
``ALPACA_*``, ``MARKET_DATA_*``, ``HISTORICAL_DATA_*``, ``ASSET_DISCOVERY_*``,
``MARKET_INTELLIGENCE_*``, ``NEWS_*`` and ``STRATEGY_*`` variables (except
``ATLAS_TEST_*``), settings are built with ``_env_file=None``, and tests that use
the real loaders first ``chdir`` to an empty temporary directory.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.ai_engine.config import get_ai_settings
from app.asset_discovery.config import get_asset_discovery_settings
from app.core.clocks import FixedClock
from app.core.config import Settings, get_market_data_settings, get_settings
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.market_intelligence.config import get_market_intelligence_settings
from app.news.config import get_news_settings
from app.strategies.config import get_strategy_settings

FIXED_NOW = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)

BASE_SETTINGS: dict[str, Any] = {
    "environment": "test",
    "postgres_host": "127.0.0.1",
    "postgres_port": 5432,
    "postgres_db": "atlas_test",
    "postgres_user": "atlas",
    "postgres_password": "CHANGE_ME_LOCAL_ONLY",
    "redis_url": "redis://127.0.0.1:6379/0",
    "log_json": True,
}


def _clear_settings_caches() -> None:
    get_settings.cache_clear()
    get_market_data_settings.cache_clear()
    get_alpaca_settings.cache_clear()
    get_asset_discovery_settings.cache_clear()
    get_market_intelligence_settings.cache_clear()
    get_news_settings.cache_clear()
    get_strategy_settings.cache_clear()
    get_ai_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> Iterator[None]:
    _clear_settings_caches()
    yield
    _clear_settings_caches()


_APPLICATION_PREFIXES = (
    "ATLAS_",
    "ALPACA_",
    "MARKET_DATA_",
    "HISTORICAL_DATA_",
    "ASSET_DISCOVERY_",
    "MARKET_INTELLIGENCE_",
    "NEWS_",
    "STRATEGY_",
    "ML_",
    "AI_",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in list(os.environ):
        upper = key.upper()
        if upper.startswith(_APPLICATION_PREFIXES) and not upper.startswith("ATLAS_TEST_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def make_settings(clean_env: pytest.MonkeyPatch) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values = {**BASE_SETTINGS, **overrides}
        return Settings(_env_file=None, **values)

    return factory


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(FIXED_NOW)
