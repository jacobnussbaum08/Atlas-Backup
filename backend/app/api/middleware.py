"""Request correlation and access logging."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from app.core.exceptions import unexpected_error_response
from app.core.logging import bind_context, clear_context, get_logger

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

logger = get_logger("app.access")


async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Attach a request ID to logs and the response; log one line per request.

    The query string is never logged because it may carry credentials.
    """
    supplied = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = supplied if _VALID_REQUEST_ID.match(supplied) else uuid.uuid4().hex
    request.state.request_id = request_id

    clear_context()
    bind_context(request_id=request_id)
    started = time.perf_counter()
    status_code = 500
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            response = unexpected_error_response(request, exc)
        status_code = response.status_code
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        clear_context()
