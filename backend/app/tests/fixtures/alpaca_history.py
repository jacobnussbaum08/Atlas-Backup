"""TEST FIXTURE: a dataset-backed fake of Alpaca's historical REST API (bars and trades).

Unlike ``FakeAlpaca`` (scripted responses in order), this fake answers from an in-memory
dataset by symbol and time range, as Alpaca documents (ascending order, inclusive
``start``/``end``, ``next_page_token`` pagination), so recovery code can ask for whatever
intervals it decides are missing. Faults can be injected per endpoint.

Everything served is CONSTRUCTED TEST DATA, not market data, and this is not a
connectivity test. Real-provider recovery is verified only in ``tests/live`` (opt-in).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.clocks import FixedClock
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import AlpacaSettings
from app.tests.fixtures.alpaca import FAKE_KEY, FAKE_SECRET, RecordingSleep, _unquote_numbers


def _rfc(at: datetime, nanos: int = 0) -> str:
    text = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    fraction = at.microsecond * 1000 + nanos
    return f"{text}.{fraction:09d}Z" if fraction else f"{text}Z"


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def history_bar(at: datetime, close: str = "100.20", volume: int = 100) -> dict[str, Any]:
    """A bar in Alpaca's JSON shape (prices as JSON numbers when served)."""
    return {
        "t": _rfc(at),
        "o": "100.10",
        "h": "100.50",
        "l": "99.90",
        "c": close,
        "v": volume,
        "n": 3,
        "vw": "100.21",
    }


def history_trade(
    at: datetime, trade_id: int, price: str = "100.25", size: int = 10
) -> dict[str, Any]:
    return {"t": _rfc(at, 1), "x": "V", "p": price, "s": size, "c": ["@"], "i": trade_id, "z": "C"}


@dataclass
class FakeHistory:
    """Serves ``bars[(symbol, timeframe)]`` and ``trades[symbol]`` by range."""

    bars: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    trades: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    page_size: int = 50
    # Per path: HTTP statuses to answer (in order) before serving data normally.
    faults: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    # Per path: after this many successful pages, answer with this status (once per job).
    fail_after_pages: dict[str, tuple[int, int]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    _pages_served: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.faults[path]:
            status = self.faults[path].pop(0)
            return httpx.Response(status, json={"message": f"injected {status}"})
        if path in self.fail_after_pages:
            pages, status = self.fail_after_pages[path]
            if self._pages_served[path] >= pages:
                del self.fail_after_pages[path]
                return httpx.Response(status, json={"message": f"injected {status}"})
        params = dict(request.url.params)
        symbol = params["symbols"]
        start, end = _parse(params["start"]), _parse(params["end"])
        if path.endswith("/bars"):
            items = self.bars[(symbol, params["timeframe"])]
            key = "bars"
        elif path.endswith("/trades"):
            items = self.trades[symbol]
            key = "trades"
        else:
            return httpx.Response(404, json={"message": "unknown path"})
        selected = sorted(
            (i for i in items if start <= _parse(i["t"]) <= end), key=lambda i: i["t"]
        )
        offset = int(params.get("page_token") or 0)
        limit = min(int(params.get("limit") or self.page_size), self.page_size)
        page = selected[offset : offset + limit]
        token = str(offset + limit) if offset + limit < len(selected) else None
        self._pages_served[path] += 1
        body: dict[str, Any] = {key: {symbol: page} if page else {}, "next_page_token": token}
        if key == "bars":
            body["currency"] = "USD"
        text = json.dumps(body)
        for number in ("o", "h", "l", "c", "vw", "p"):  # serve prices as JSON numbers
            text = _unquote_numbers(text, number)
        return httpx.Response(200, text=text, headers={"Content-Type": "application/json"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def history_provider(
    fake: FakeHistory, clock: FixedClock, *, max_retries: int = 0, **settings: Any
) -> AlpacaMarketDataProvider:
    sleep = RecordingSleep()
    return AlpacaMarketDataProvider(
        AlpacaSettings(_env_file=None, api_key=FAKE_KEY, secret_key=FAKE_SECRET, **settings),
        clock,
        max_retries=max_retries,
        max_pages=200,
        transport=fake.transport,
        sleep=sleep,
        monotonic=sleep.monotonic,
    )


def minutes(start: datetime, count: int, step: timedelta = timedelta(minutes=1)) -> list[datetime]:
    return [start + i * step for i in range(count)]
