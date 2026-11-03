"""TEST FIXTURE: constructed news items and a fake of Alpaca's news REST endpoint.

Every article here is made-up test data shaped like Alpaca's *documented* news object
(``GET /v1beta1/news``: ``{"news": [{id, headline, author, created_at, updated_at,
summary, content, images, symbols, source, url}], "next_page_token": ...}``; the stream
sends the same object with ``"T": "n"``). Headlines are invented and are NOT real news.
Real-provider verification lives only in ``tests/live`` (opt-in).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.clocks import FixedClock
from app.data_engine.providers.alpaca.config import AlpacaSettings
from app.news.config import NewsSettings
from app.news.providers.alpaca.adapter import AlpacaNewsProvider
from app.tests.fixtures.alpaca import FAKE_KEY, FAKE_SECRET, FakeAlpaca, RecordingSleep


def rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def news_item(
    article_id: int,
    created_at: datetime,
    *,
    headline: str = "TEST FIXTURE headline",
    summary: str | None = "",
    author: str | None = "Fixture Desk",
    symbols: list[str] | None = None,
    source: str | None = "fixturewire",
    url: str | None = None,
    updated_at: datetime | None = None,
    content: str | None = "",
    images: list[dict[str, str]] | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """One news object in Alpaca's documented JSON shape (constructed test data)."""
    item: dict[str, Any] = {
        "id": article_id,
        "headline": headline,
        "author": author,
        "created_at": rfc3339(created_at),
        "updated_at": rfc3339(updated_at or created_at),
        "summary": summary,
        "content": content,
        "images": images or [],
        "symbols": symbols if symbols is not None else ["AAPL"],
        "source": source,
    }
    if url is not None:
        item["url"] = url
    if stream:
        item = {"T": "n", **item}
    return item


def news_page(items: list[Any], next_page_token: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={"news": items, "next_page_token": next_page_token},
        headers={"Content-Type": "application/json"},
    )


def news_settings(**overrides: Any) -> NewsSettings:
    return NewsSettings(_env_file=None, **overrides)


def fake_news_provider(
    fake: FakeAlpaca | None = None,
    *,
    now: datetime,
    clock: FixedClock | None = None,
    stream_url: str | None = None,
    credentials: bool = True,
    **settings: Any,
) -> AlpacaNewsProvider:
    """A news adapter whose HTTP goes to ``fake`` (never the network) and sleeps nothing."""
    values: dict[str, Any] = {"api_key": FAKE_KEY, "secret_key": FAKE_SECRET} if credentials else {}
    sleep = RecordingSleep()
    return AlpacaNewsProvider(
        AlpacaSettings(_env_file=None, **values),
        news_settings(**settings),
        clock or FixedClock(now),
        transport=(fake or FakeAlpaca()).transport,
        sleep=sleep,
        monotonic=sleep.monotonic,
        stream_url=stream_url,
    )


async def collect(provider: AlpacaNewsProvider, query: Any) -> list[Any]:
    return [page async for page in provider.fetch_news(query)]


def pages(provider: AlpacaNewsProvider, query: Any) -> list[Any]:
    return asyncio.run(collect(provider, query))


__all__ = [
    "FAKE_KEY",
    "FAKE_SECRET",
    "fake_news_provider",
    "news_item",
    "news_page",
    "news_settings",
    "pages",
    "rfc3339",
]
