"""HTTP transport for Alpaca's market-data REST API: pacing, retries, error mapping.

* Requests are paced to the plan's documented rate (e.g. 200/min on Basic) and
  additionally stop when ``X-RateLimit-Remaining`` reaches 0, until ``X-RateLimit-Reset``.
* Transient failures (timeouts, network errors, HTTP 429 and 5xx) are retried with
  bounded exponential backoff. Authentication (401), entitlement (403) and request
  (4xx) errors are permanent and never retried.
* Error messages are reduced to one redacted line. Request headers (which carry the
  credentials) are never logged or included in exceptions.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime

import httpx

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.errors import (
    MarketDataError,
    ProviderAuthenticationError,
    ProviderEntitlementError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)

logger = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]

BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 30.0
RATE_LIMIT_MAX_WAIT_SECONDS = 65.0


class RequestPacer:
    """Spaces requests evenly so a plan's per-minute limit is never exceeded.

    Thread-safe: one provider instance may serve jobs on several threads.
    """

    def __init__(
        self, requests_per_minute: int | None, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self._monotonic = monotonic
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def reserve(self) -> float:
        """Claim the next request slot; returns seconds to wait before sending."""
        with self._lock:
            now = self._monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
            return slot - now

    def pause_for(self, seconds: float) -> None:
        """No request may be sent for ``seconds`` (provider quota exhausted)."""
        with self._lock:
            self._next_slot = max(self._next_slot, self._monotonic() + max(0.0, seconds))


def backoff_delay(attempt: int) -> float:
    """Bounded exponential backoff: 0.5 s, 1 s, 2 s, ... capped at 30 s."""
    return float(min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2**attempt)))


def provider_message(response: httpx.Response, scrub: tuple[str, ...] = ()) -> str:
    """One redacted line describing an error response.

    ``scrub`` holds the configured credential values; if the provider echoes one
    back, it is masked as well as anything the generic redaction recognizes.
    """
    text = response.text or ""
    try:
        body = json.loads(text)
        if isinstance(body, dict) and isinstance(body.get("message"), str):
            text = body["message"]
    except ValueError:
        pass
    line = " ".join(text.split())
    for value in scrub:
        if value:
            line = line.replace(value, "***REDACTED***")
    return redact_text(line)[:300] or response.reason_phrase or "no message"


def seconds_until_reset(headers: Mapping[str, str], now: datetime) -> float | None:
    reset = headers.get("X-RateLimit-Reset")
    if reset is None:
        return None
    try:
        seconds = float(reset) - now.timestamp()
    except ValueError:
        return None
    return min(RATE_LIMIT_MAX_WAIT_SECONDS, max(0.0, seconds))


class AlpacaHttp:
    def __init__(
        self,
        *,
        clock: Clock,
        pacer: RequestPacer,
        max_retries: int,
        sleep: Sleep = asyncio.sleep,
        scrub: tuple[str, ...] = (),
    ) -> None:
        self._scrub = scrub
        self._clock = clock
        self._pacer = pacer
        self._max_retries = max_retries
        self._sleep = sleep

    async def get(
        self, client: httpx.AsyncClient, path: str, params: Mapping[str, str]
    ) -> httpx.Response:
        attempt = 0
        while True:
            wait = self._pacer.reserve()
            if wait > 0:
                await self._sleep(wait)

            delay: float | None = None
            error: MarketDataError
            try:
                response = await client.get(path, params=dict(params))
            except httpx.TimeoutException:
                error = ProviderUnavailableError("Alpaca request timed out")
            except httpx.TransportError as exc:
                error = ProviderUnavailableError(f"network error: {type(exc).__name__}")
            else:
                self._observe_quota(response)
                status = response.status_code
                if status == 200:
                    return response
                message = provider_message(response, self._scrub)
                if status == 401:
                    raise ProviderAuthenticationError(f"Alpaca rejected the credentials: {message}")
                if status == 403:
                    raise ProviderEntitlementError(f"Alpaca refused the request (403): {message}")
                if status == 429:
                    error = ProviderRateLimitError(f"Alpaca rate limit reached: {message}")
                    delay = seconds_until_reset(response.headers, self._clock.now())
                elif status >= 500:
                    error = ProviderUnavailableError(f"Alpaca server error {status}: {message}")
                else:
                    raise ProviderRequestError(f"Alpaca rejected the request ({status}): {message}")

            if attempt >= self._max_retries:
                logger.warning("alpaca.request_failed", path=path, attempts=attempt + 1)
                raise error
            delay = backoff_delay(attempt) if delay is None else max(delay, 0.0)
            logger.info(
                "alpaca.request_retry",
                path=path,
                attempt=attempt + 1,
                delay_seconds=round(delay, 3),
                reason=error.code,
            )
            await self._sleep(delay)
            attempt += 1

    def _observe_quota(self, response: httpx.Response) -> None:
        if response.headers.get("X-RateLimit-Remaining") == "0":
            wait = seconds_until_reset(response.headers, self._clock.now())
            if wait:
                self._pacer.pause_for(wait)
