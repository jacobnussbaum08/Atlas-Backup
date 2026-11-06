from __future__ import annotations

from sqlalchemy import create_engine

from app.core.security.redaction import REDACTED
from app.monitoring.health import (
    HealthStatus,
    check_database,
    check_redis,
    expected_migration_heads,
)


class StubRedis:
    """Test double for the Redis client's ``ping``; not a Redis implementation."""

    def __init__(self, reply: object = True, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error

    def ping(self):
        if self.error:
            raise self.error
        return self.reply


def test_redis_ok():
    result = check_redis(StubRedis())
    assert result.status is HealthStatus.OK
    assert result.detail is None
    assert result.latency_ms is not None


def test_redis_unexpected_reply_is_an_error():
    result = check_redis(StubRedis(reply=False))
    assert result.detail is not None
    assert result.status is HealthStatus.ERROR
    assert "unexpected PING reply" in result.detail


def test_redis_failure_detail_is_redacted_and_single_line():
    error = ConnectionError("cannot reach redis://:topsecret@cache:6379\nsecond line")
    result = check_redis(StubRedis(error=error))
    assert result.detail is not None

    assert result.status is HealthStatus.ERROR
    assert "topsecret" not in result.detail
    assert REDACTED in result.detail
    assert "\n" not in result.detail
    assert result.detail.startswith("ConnectionError:")


def test_database_unreachable_is_reported_not_raised():
    engine = create_engine(
        "postgresql+psycopg://atlas:pw@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    try:
        result = check_database(engine)
    finally:
        engine.dispose()

    assert result.status is HealthStatus.ERROR
    assert result.detail is not None
    assert result.detail.startswith("OperationalError")


def test_expected_migration_heads_come_from_the_code_base():
    assert expected_migration_heads() == frozenset({"0015"})
