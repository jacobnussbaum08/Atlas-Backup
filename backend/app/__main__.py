"""Run the Atlas API server: ``python -m app``.

Settings are validated before uvicorn starts, so a misconfigured environment
fails immediately with a clear error instead of a half-started server.
"""

from __future__ import annotations

import sys

import uvicorn
from pydantic import ValidationError

from app.core.config import UnknownSettingError, get_settings


def main() -> int:
    try:
        settings = get_settings()
    except (ValidationError, UnknownSettingError) as exc:
        # Pydantic error text includes field names and messages, not secret values.
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
