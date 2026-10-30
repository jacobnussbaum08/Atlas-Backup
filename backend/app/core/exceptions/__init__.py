"""Atlas exception hierarchy and HTTP error mapping."""

from __future__ import annotations

from typing import ClassVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AtlasError(Exception):
    """Base class for expected, domain-level failures."""

    code: ClassVar[str] = "atlas_error"
    http_status: ClassVar[int] = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(AtlasError):
    code = "configuration_error"


class DependencyUnavailableError(AtlasError):
    code = "dependency_unavailable"
    http_status = 503


class NotFoundError(AtlasError):
    code = "not_found"
    http_status = 404


class InvalidRequestError(AtlasError):
    code = "invalid_request"
    http_status = 422


class UnauthorizedError(AtlasError):
    code = "unauthorized"
    http_status = 401


class ForbiddenError(AtlasError):
    code = "forbidden"
    http_status = 403


def _error_body(code: str, message: str, request: Request) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def unexpected_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Log an unhandled exception and return a generic 500 body.

    Details go to the log only; clients never see internal messages.
    """
    logger.error("request.unhandled_error", error_type=type(exc).__name__, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=_error_body("internal_error", "An internal error occurred.", request),
    )


async def _handle_atlas_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AtlasError):  # pragma: no cover - registered for AtlasError only
        return unexpected_error_response(request, exc)
    logger.warning("request.atlas_error", code=exc.code, error_message=exc.message)
    return JSONResponse(
        status_code=exc.http_status, content=_error_body(exc.code, exc.message, request)
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Last-resort handler for errors raised outside the request-context middleware.
    return unexpected_error_response(request, exc)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AtlasError, _handle_atlas_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)


__all__ = [
    "AtlasError",
    "ConfigurationError",
    "DependencyUnavailableError",
    "ForbiddenError",
    "InvalidRequestError",
    "NotFoundError",
    "UnauthorizedError",
    "register_exception_handlers",
]
