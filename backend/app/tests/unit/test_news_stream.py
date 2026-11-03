"""Alpaca news WebSocket client against a FAKE local server (Phase 3C).

The fake speaks Alpaca's documented stream protocol with constructed TEST FIXTURE articles;
it is not Alpaca. Real-provider checks live in tests/live.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.core.clocks import FixedClock
from app.data_engine.enums import StreamAuthState, StreamSubscriptionState
from app.data_engine.errors import (
    StreamAuthenticationError,
    StreamProtocolError,
    StreamSubscriptionError,
)
from app.data_engine.schemas import ConnectionState
from app.news.enums import ArticleOrigin
from app.tests.fixtures.alpaca import FAKE_KEY, FAKE_SECRET, FakeAlpaca
from app.tests.fixtures.alpaca_news import fake_news_provider, news_item
from app.tests.fixtures.alpaca_stream import FakeAlpacaStreamServer, Script, frame

NOW = datetime(2026, 3, 2, 14, 31, tzinfo=UTC)
T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def _stream(server, **settings):
    provider = fake_news_provider(FakeAlpaca(), now=NOW, stream_url=server.url, **settings)
    return provider.create_stream()


async def _collect(stream, count):
    batches = []
    async for batch in stream.events():
        batches.append(batch)
        if len(batches) >= count:
            break
    return batches


def test_authenticates_subscribes_and_decodes_articles():
    async def scenario():
        script = Script(
            frames=[
                [
                    frame(
                        news_item(101, T0, headline="Fixture Corp Beats Estimates", stream=True),
                        news_item(102, T0, symbols=["MSFT"], stream=True),
                    )
                ]
            ]
        )
        async with FakeAlpacaStreamServer(script) as server:
            stream = _stream(server)
            await stream.connect()
            assert stream.auth_state is StreamAuthState.AUTHENTICATED
            confirmed = await stream.subscribe(frozenset({"*"}))
            assert confirmed == frozenset({"*"})
            assert stream.subscription_state is StreamSubscriptionState.ACTIVE
            assert stream.state is ConnectionState.SUBSCRIBED
            [batch] = await _collect(stream, 1)
            await stream.close()
            conn = server.connections[0]
            return conn, batch

    conn, batch = asyncio.run(scenario())
    assert conn.auth_messages == [{"action": "auth", "key": FAKE_KEY, "secret": FAKE_SECRET}]
    assert conn.subscribe_messages == [{"action": "subscribe", "news": ["*"]}]
    assert [a.provider_article_id for a in batch.articles] == ["101", "102"]
    assert all(a.origin is ArticleOrigin.REALTIME_STREAM for a in batch.articles)
    assert all(a.received_at == NOW for a in batch.articles)
    assert "T" not in batch.articles[0].provider_payload


def test_symbol_subscription_and_partial_confirmation():
    async def scenario():
        async with FakeAlpacaStreamServer(Script()) as server:
            stream = _stream(server)
            await stream.connect()
            confirmed = await stream.subscribe(frozenset({"AAPL", "MSFT"}))
            await stream.close()
            return confirmed, server.connections[0].subscribe_messages

    confirmed, sent = asyncio.run(scenario())
    assert confirmed == frozenset({"AAPL", "MSFT"})
    assert sent == [{"action": "subscribe", "news": ["AAPL", "MSFT"]}]


def test_rejected_credentials_are_a_permanent_failure():
    async def scenario():
        async with FakeAlpacaStreamServer(Script(auth_errors=[(402, "auth failed")])) as server:
            stream = _stream(server)
            with pytest.raises(StreamAuthenticationError) as caught:
                await stream.connect()
            return stream, caught.value

    stream, error = asyncio.run(scenario())
    assert error.permanent is True
    assert stream.auth_state is StreamAuthState.REJECTED
    assert stream.state is ConnectionState.FAILED


def test_subscription_errors_and_protocol_misuse():
    async def scenario():
        script = Script(subscribe_errors=[(409, "insufficient subscription")])
        async with FakeAlpacaStreamServer(script) as server:
            stream = _stream(server)
            with pytest.raises(StreamProtocolError):
                await stream.subscribe(frozenset({"*"}))  # before authentication
            await stream.connect()
            with pytest.raises(StreamProtocolError):
                await stream.subscribe(frozenset())
            with pytest.raises(StreamSubscriptionError):
                await stream.subscribe(frozenset({"*"}))
            state = stream.subscription_state
            await stream.close()
            return state

    assert asyncio.run(scenario()) is StreamSubscriptionState.REJECTED


def test_decode_never_raises_on_bad_frames():
    provider = fake_news_provider(FakeAlpaca(), now=NOW)
    stream = provider.create_stream()
    assert stream.decode(b"\x00\x01", NOW).malformed[0].reason.startswith("binary frame")
    assert stream.decode("not json", NOW).malformed[0].reason == "frame is not valid JSON"
    assert stream.decode('{"T":"n"}', NOW).malformed[0].reason == "frame is not a JSON array"
    batch = stream.decode(
        json.dumps(
            [
                {**news_item(1, T0, stream=True), "headline": ""},
                5,
                {"T": "x", "foo": "bar"},
                {"T": "error", "code": 500, "msg": "internal error"},
                news_item(2, T0, stream=True),
            ]
        ),
        NOW,
    )
    assert [a.provider_article_id for a in batch.articles] == ["2"]
    assert [m.reason for m in batch.malformed] == [
        "article has no headline",
        "item is not an object",
    ]
    assert [n["kind"] for n in batch.notices] == ["unhandled_message:x", "control:error"]


def test_stream_keeps_bodies_only_when_permitted():
    item = json.dumps([news_item(3, T0, content="<p>Body</p>", stream=True)])
    withheld = fake_news_provider(FakeAlpaca(), now=NOW).create_stream().decode(item, NOW)
    assert withheld.articles[0].content is None
    assert withheld.articles[0].has_body is True
    kept = fake_news_provider(FakeAlpaca(), now=NOW, store_content=True).create_stream()
    assert kept.decode(item, NOW).articles[0].content == "Body"


def test_repr_never_contains_credentials():
    stream = fake_news_provider(FakeAlpaca(), now=NOW, clock=FixedClock(NOW)).create_stream()
    assert "TESTKEY" not in repr(stream)
    assert "Secret" not in repr(stream)
    assert stream.url == "wss://stream.data.alpaca.markets/v1beta1/news"
