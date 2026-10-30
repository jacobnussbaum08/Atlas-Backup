"""Opt-in real-provider tests. Excluded from normal runs (``-m "not live"`` in pytest config).

Run explicitly from the repository root, with real credentials in ``.env``:

    ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=... uv run pytest -m live -s

They call Alpaca's market-data API (never trading endpoints) and persist into a
throw-away database copied from the migrated template.
"""

from __future__ import annotations

import os

import pytest

from app.tests.integration.conftest import (  # noqa: F401  # re-exported fixtures
    fresh_engine,
    server_url,
    template_database,
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("ATLAS_LIVE_PROVIDER_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set ATLAS_LIVE_PROVIDER_TESTS=1 to run real-provider tests")
    for item in items:
        # Match the marker itself; keywords would also match parameter ids like "[live]".
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)
