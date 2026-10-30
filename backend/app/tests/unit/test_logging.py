from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.logging import bind_context, clear_context, configure_logging, get_logger
from app.core.security.redaction import REDACTED


@pytest.fixture
def log_stream():
    stream = io.StringIO()
    configure_logging("DEBUG", json_output=True, stream=stream, environment="test")
    yield stream
    clear_context()
    configure_logging("WARNING", json_output=True)


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_structlog_records_are_json_with_utc_timestamp(log_stream):
    get_logger("atlas.test").info("unit.event", answer=42)

    (record,) = _records(log_stream)
    assert record["event"] == "unit.event"
    assert record["answer"] == 42
    assert record["level"] == "info"
    assert record["logger"] == "atlas.test"
    assert record["service"] == "atlas-ai-trader"
    assert record["environment"] == "test"
    timestamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    assert timestamp.utcoffset() == UTC.utcoffset(None)


def test_stdlib_logging_uses_same_json_pipeline(log_stream):
    logging.getLogger("third.party").warning("plain %s message", "stdlib")

    (record,) = _records(log_stream)
    assert record["event"] == "plain stdlib message"
    assert record["level"] == "warning"
    assert record["logger"] == "third.party"
    assert "timestamp" in record


def test_secrets_are_redacted(log_stream):
    get_logger().info(
        "connecting",
        password="hunter2",
        api_key="abc123",
        dsn="postgresql://atlas:hunter2@db:5432/atlas",
        nested={"token": "t0k3n", "safe": "visible"},
        note="retry with password=hunter2 later",
    )

    (record,) = _records(log_stream)
    raw = log_stream.getvalue()
    assert "hunter2" not in raw
    assert "abc123" not in raw
    assert "t0k3n" not in raw
    assert record["password"] == REDACTED
    assert record["api_key"] == REDACTED
    assert record["dsn"] == f"postgresql://atlas:{REDACTED}@db:5432/atlas"
    assert record["nested"] == {"token": REDACTED, "safe": "visible"}


def test_context_variables_are_merged(log_stream):
    bind_context(request_id="req-1")
    get_logger().info("with.context")
    clear_context()
    get_logger().info("without.context")

    first, second = _records(log_stream)
    assert first["request_id"] == "req-1"
    assert "request_id" not in second


def test_exceptions_are_rendered_into_the_record(log_stream):
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger().exception("failed")

    (record,) = _records(log_stream)
    assert "RuntimeError: boom" in record["exception"]


def test_uvicorn_access_log_is_suppressed_and_color_field_dropped(log_stream):
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /x?token=abc HTTP/1.1" 200')
    logging.getLogger("uvicorn.error").info("Started", extra={"color_message": "\x1b[36mStarted"})

    (record,) = _records(log_stream)
    assert record["event"] == "Started"
    assert "color_message" not in record


def test_level_filtering(log_stream):
    configure_logging("WARNING", json_output=True, stream=log_stream)
    get_logger().info("dropped")
    get_logger().warning("kept")

    assert [r["event"] for r in _records(log_stream)] == ["kept"]
