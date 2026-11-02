"""TEST FIXTURE: a scriptable fake of Alpaca's asset directory (``GET /v2/assets``).

Records are constructed test data shaped like Alpaca's *documented* asset object (id,
class, exchange, symbol, name, status, tradable, marginable, shortable, easy_to_borrow,
fractionable, attributes, borrow_status, margin_requirement_*). Symbols, names, IDs and
flags are invented; this is NOT real reference data. Real-provider verification lives only
in ``tests/live`` (opt-in).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.core.clocks import Clock, FixedClock
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import AlpacaSettings
from app.tests.fixtures.alpaca import FAKE_KEY, FAKE_SECRET, RecordingSleep

PAPER_HOST = "paper-api.alpaca.markets"


def fixture_id(name: str) -> str:
    """A stable, obviously synthetic UUID per fixture name."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atlas-test-asset:{name}"))


def asset_record(
    symbol: str,
    *,
    key: str | None = None,
    exchange: str | None = "NASDAQ",
    status: str = "active",
    tradable: bool = True,
    name: str | None = None,
    shortable: bool = True,
    easy_to_borrow: bool = True,
    marginable: bool = True,
    fractionable: bool = True,
    attributes: tuple[str, ...] = (),
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": fixture_id(key or symbol),
        "class": "us_equity",
        "exchange": exchange,
        "symbol": symbol,
        "name": name or f"Fixture {symbol} Inc. (test data)",
        "status": status,
        "tradable": tradable,
        "marginable": marginable,
        "maintenance_margin_requirement": 30,
        "margin_requirement_long": "30",
        "margin_requirement_short": "30",
        "shortable": shortable,
        "easy_to_borrow": easy_to_borrow,
        "borrow_status": "easy_to_borrow" if easy_to_borrow else "hard_to_borrow",
        "fractionable": fractionable,
        "attributes": list(attributes),
    }
    record.update(extra)
    return record


@dataclass
class FakeAssetDirectory:
    """Serves the current ``records`` like Alpaca: list (status filter) and lookup by ID.

    ``fail_with`` makes the next requests fail with that HTTP status (outage tests);
    ``raw_body`` replaces the list response verbatim (malformed-payload tests).
    """

    records: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    fail_with: list[int] = field(default_factory=list)
    raw_body: str | None = None
    on_request: Callable[[httpx.Request], None] | None = None

    def set(self, *records: dict[str, Any]) -> FakeAssetDirectory:
        self.records = list(records)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        assert request.url.host == PAPER_HOST, f"unexpected host {request.url.host}"
        assert request.method == "GET", f"unexpected method {request.method}"
        if self.fail_with:
            return httpx.Response(self.fail_with.pop(0), json={"message": "fixture failure"})
        path = request.url.path
        if path == "/v2/assets":
            if self.raw_body is not None:
                return httpx.Response(200, text=self.raw_body)
            status = request.url.params.get("status")
            body = [r for r in self.records if status is None or r.get("status") == status]
            return httpx.Response(200, text=json.dumps(body))
        if path.startswith("/v2/assets/"):
            wanted = path.rsplit("/", 1)[1]
            for record in self.records:
                if record.get("id") == wanted:
                    return httpx.Response(200, text=json.dumps(record))
            return httpx.Response(404, json={"message": "asset not found"})
        raise AssertionError(f"unexpected path {path}")

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def list_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v2/assets"]


def directory_provider(
    directory: FakeAssetDirectory,
    *,
    now: datetime | None = None,
    clock: Clock | None = None,
    max_retries: int = 2,
) -> AlpacaMarketDataProvider:
    sleep = RecordingSleep()
    return AlpacaMarketDataProvider(
        AlpacaSettings(_env_file=None, api_key=FAKE_KEY, secret_key=FAKE_SECRET),
        clock or FixedClock(now or datetime.fromisoformat("2026-03-10T15:00:00+00:00")),
        max_retries=max_retries,
        transport=directory.transport,
        sleep=sleep,
        monotonic=sleep.monotonic,
    )
