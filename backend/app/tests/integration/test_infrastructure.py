"""End-to-end checks of the real engine, Redis client, and readiness wiring."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.core.redis_client import build_redis
from app.database.sessions import build_engine, build_session_factory, session_scope
from app.main import create_app
from app.monitoring.health import HealthStatus, check_redis


def _settings_for(url, make_settings, **overrides) -> Settings:
    settings: Settings = make_settings(
        postgres_host=url.host,
        postgres_port=url.port or 5432,
        postgres_db=url.database,
        postgres_user=url.username,
        postgres_password=url.password,
        **overrides,
    )
    return settings


@pytest.mark.integration
def test_engine_sessions_are_utc_with_statement_timeout(migrated_url, make_settings):
    engine = build_engine(_settings_for(migrated_url, make_settings, db_statement_timeout_ms=1500))
    try:
        with session_scope(build_session_factory(engine)) as session:
            assert session.execute(text("SHOW timezone")).scalar_one() == "UTC"
            assert session.execute(text("SHOW statement_timeout")).scalar_one() == "1500ms"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_session_scope_rolls_back_on_error(migrated_url, make_settings):
    engine = build_engine(_settings_for(migrated_url, make_settings))
    factory = build_session_factory(engine)

    def insert_then_fail() -> None:
        with session_scope(factory) as session:
            session.execute(text("INSERT INTO portfolio_accounts (name) VALUES ('rollback-probe')"))
            raise RuntimeError("abort")

    try:
        with pytest.raises(RuntimeError):
            insert_then_fail()
        with session_scope(factory) as session:
            count = session.execute(
                text("SELECT count(*) FROM portfolio_accounts WHERE name = 'rollback-probe'")
            ).scalar_one()
        assert count == 0
    finally:
        engine.dispose()


@pytest.mark.integration
def test_ready_endpoint_with_real_database_and_unreachable_redis(migrated_url, make_settings):
    """Full production wiring: real engine + migration check; Redis deliberately unreachable."""
    settings = _settings_for(
        migrated_url, make_settings, redis_url="redis://127.0.0.1:1/0", redis_timeout_seconds=0.5
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    components = {c["name"]: c for c in response.json()["components"]}
    assert response.status_code == 503
    assert components["database"]["status"] == "ok"
    assert components["migrations"]["status"] == "ok"
    assert components["redis"]["status"] == "error"


@pytest.mark.redis
def test_real_redis_round_trip(make_settings):
    url = os.environ.get("ATLAS_TEST_REDIS_URL")
    if not url:
        if os.environ.get("ATLAS_REQUIRE_INTEGRATION") == "1":
            pytest.fail("ATLAS_TEST_REDIS_URL is not set")
        pytest.skip("ATLAS_TEST_REDIS_URL is not set; Redis integration tests need a real server")

    client = build_redis(make_settings(redis_url=url))
    try:
        assert check_redis(client).status is HealthStatus.OK
        client.set("atlas:test:probe", "1", ex=10)
        assert client.get("atlas:test:probe") == "1"
    finally:
        client.delete("atlas:test:probe")
        client.close()
