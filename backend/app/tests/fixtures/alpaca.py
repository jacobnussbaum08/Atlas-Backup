"""TEST FIXTURE: a scriptable fake of Alpaca's market-data REST API.

Everything here is constructed test data shaped like Alpaca's *documented* response
format (``GET /v2/stocks/bars``: ``{"bars": {SYMBOL: [{t,o,h,l,c,v,n,vw}]},
"next_page_token": ...}``). It is NOT real market data and NOT a connectivity test.
Real-provider verification lives only in ``tests/live`` (opt-in).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.clocks import FixedClock
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import AlpacaSettings

FAKE_KEY = "TESTKEYFIXTURE0001"  # fake credentials for the fake server
FAKE_SECRET = "TestSecretFixtureNotReal0001"

Responder = Callable[[httpx.Request], httpx.Response]


def fixture_bar(
    at: datetime,
    *,
    o: str = "100.10",
    h: str = "100.50",
    low: str = "99.90",
    c: str = "100.20",
    v: int | None = 1200,
    n: int | None = 15,
    vw: str | None = "100.21",
) -> dict[str, Any]:
    """One bar in Alpaca's JSON shape. Prices are strings so they serialize exactly."""
    bar: dict[str, Any] = {"t": at.astimezone(UTC).isoformat().replace("+00:00", "Z")}
    bar |= {"o": o, "h": h, "l": low, "c": c}
    if v is not None:
        bar["v"] = v
    if n is not None:
        bar["n"] = n
    if vw is not None:
        bar["vw"] = vw
    return bar


def minute_bars(symbol: str, start: datetime, count: int, step: timedelta) -> list[dict[str, Any]]:
    return [fixture_bar(start + i * step) for i in range(count)]


def bars_body(
    bars_by_symbol: dict[str, list[dict[str, Any]]] | None,
    next_page_token: str | None = None,
    currency: str | None = "USD",
) -> str:
    """Serialize a page; numeric strings become JSON numbers verbatim (no float rounding)."""
    body: dict[str, Any] = {"bars": bars_by_symbol, "next_page_token": next_page_token}
    if currency is not None:
        body["currency"] = currency
    text = json.dumps(body)
    # Turn "o": "100.10" into "o": 100.10 so the payload has real JSON numbers.
    for key in ("o", "h", "l", "c", "vw"):
        text = _unquote_numbers(text, key)
    return text


def _unquote_numbers(text: str, key: str) -> str:
    import re

    return re.sub(rf'"{key}": "(-?[0-9.]+)"', rf'"{key}": \1', text)


@dataclass
class FakeAlpaca:
    """Serves scripted responses in order and records every request."""

    responses: list[Responder | httpx.Response] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def add_page(
        self,
        bars_by_symbol: dict[str, list[dict[str, Any]]] | None,
        next_page_token: str | None = None,
        *,
        currency: str | None = "USD",
        headers: dict[str, str] | None = None,
    ) -> FakeAlpaca:
        self.responses.append(
            httpx.Response(
                200,
                text=bars_body(bars_by_symbol, next_page_token, currency),
                headers={"Content-Type": "application/json", **(headers or {})},
            )
        )
        return self

    def add_error(
        self, status: int, message: str = "error", headers: dict[str, str] | None = None
    ) -> FakeAlpaca:
        self.responses.append(
            httpx.Response(status, json={"message": message}, headers=headers or {})
        )
        return self

    def add(self, responder: Responder | httpx.Response) -> FakeAlpaca:
        self.responses.append(responder)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {request.url}")
        response = self.responses.pop(0)
        return response(request) if callable(response) else response

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def params(self, index: int) -> dict[str, str]:
        return dict(self.requests[index].url.params)


class RecordingSleep:
    """Replaces asyncio.sleep: records delays and advances a virtual monotonic clock."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.now = 0.0

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def fake_provider(
    fake: FakeAlpaca,
    *,
    now: datetime,
    sleep: RecordingSleep | None = None,
    max_retries: int = 3,
    max_pages: int = 50,
    **settings: Any,
) -> AlpacaMarketDataProvider:
    values = {"api_key": FAKE_KEY, "secret_key": FAKE_SECRET} | settings
    sleep = sleep or RecordingSleep()
    return AlpacaMarketDataProvider(
        AlpacaSettings(_env_file=None, **values),
        FixedClock(now),
        max_retries=max_retries,
        max_pages=max_pages,
        transport=fake.transport,
        sleep=sleep,
        monotonic=sleep.monotonic,
    )
