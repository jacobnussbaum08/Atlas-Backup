"""Shared test fixtures.

Unit tests never read the developer's real environment or ``.env`` file:
``make_settings`` removes ``ATLAS_*`` variables (except ``ATLAS_TEST_*``) and
builds settings from explicit values only.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.clocks import FixedClock
from app.core.config import Settings, get_settings

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


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in list(os.environ):
        if key.upper().startswith("ATLAS_") and not key.upper().startswith("ATLAS_TEST_"):
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
