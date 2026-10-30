"""Structured logging.

Every log record — from Atlas code via structlog and from third-party libraries
via the standard ``logging`` module — is rendered by one pipeline that adds a
UTC ISO-8601 timestamp, merges request context, and redacts credentials before
output. JSON is the default; a console renderer is available for local use.
"""

from __future__ import annotations

import logging
import logging.config
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core.security.redaction import REDACTED, is_sensitive_key, redact_value


def _redact_event(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    event_dict.pop("color_message", None)  # uvicorn's ANSI-coloured duplicate of "event"
    for key, value in list(event_dict.items()):
        if is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact_value(value)
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]


def configure_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
    stream: TextIO | None = None,
    service: str = "atlas-ai-trader",
    environment: str | None = None,
) -> None:
    """Configure structlog and the stdlib root logger. Safe to call repeatedly."""
    shared = _shared_processors()
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    def add_service(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        if environment is not None:
            event_dict.setdefault("environment", environment)
        return event_dict

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            add_service,
            structlog.processors.format_exc_info,
            _redact_event,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn's loggers through the root handler instead of their own.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # uvicorn's access log is replaced by the request-context middleware, which
    # adds request IDs and never logs query strings (they may carry credentials).
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    # SQL statements may contain parameters; keep them out of logs unless asked.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name, **initial_values)
    return logger


def bind_context(**values: Any) -> None:
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def current_context() -> MutableMapping[str, Any]:
    return structlog.contextvars.get_contextvars()


__all__ = ["bind_context", "clear_context", "configure_logging", "current_context", "get_logger"]
