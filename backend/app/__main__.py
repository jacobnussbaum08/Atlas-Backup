"""Run the Atlas API server: ``python -m app``.

All configuration is validated before uvicorn starts, so a misconfigured
environment fails immediately with a clear error instead of a half-started
server. Error output names fields but never prints input values, which may be
credentials.
"""

from __future__ import annotations

import logging
import sys

import uvicorn
from pydantic import ValidationError

from app.core.config import (
    InvalidSettingsError,
    Settings,
    UnknownSettingError,
    describe_validation_error,
    get_market_data_settings,
    get_settings,
)
from app.core.logging import configure_logging, get_logger
from app.data_engine.errors import MarketDataError
from app.data_engine.providers.registry import create_provider
from app.data_engine.schemas import Severity

_LOG_LEVELS = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
}


def _load_configuration() -> Settings:
    """Validate every settings group and the provider's offline configuration."""
    settings = get_settings()
    configure_logging(
        settings.log_level,
        json_output=settings.log_json,
        service=settings.service_name,
        environment=settings.environment.value,
    )
    market_data = get_market_data_settings()
    provider = create_provider(market_data)
    report = provider.validate_configuration(market_data)
    logger = get_logger("app.startup")
    for issue in report.issues:
        logger.log(
            _LOG_LEVELS[issue.severity],
            "market_data.configuration",
            provider=report.provider,
            feed=report.feed,
            code=issue.code,
            detail=issue.message,
        )
    report.raise_for_errors()
    return settings


def main() -> int:
    try:
        settings = _load_configuration()
    except ValidationError as exc:
        print(f"Invalid Atlas configuration:\n{describe_validation_error(exc)}", file=sys.stderr)
        return 2
    except (InvalidSettingsError, UnknownSettingError, MarketDataError) as exc:
        print(f"Invalid Atlas configuration:\n{exc}", file=sys.stderr)
        return 2

    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # Atlas configures logging itself (JSON, UTC, redaction)
        access_log=False,  # replaced by the request-context middleware
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
