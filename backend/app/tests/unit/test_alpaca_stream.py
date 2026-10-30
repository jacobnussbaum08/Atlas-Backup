"""Alpaca WebSocket client against a LOCAL FAKE server (mock-provider tests).

The fake speaks Alpaca's documented protocol with constructed messages. These tests
verify Atlas's protocol handling, state tracking and normalization; they do not
claim connectivity to Alpaca (see ``tests/live``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal

import pytest
from pydantic import SecretStr

from app.core.clocks import SystemClock
from app.data_engine.enums import (
    BarObservationKind,
    DataSource,
    StreamAuthState,
    StreamChannel,
    StreamSubscriptionState,
)
from app.data_engine.errors import (
    StreamAuthenticationError,
    StreamConnectionError,
    StreamProtocolError,
    StreamSubscriptionError,
)
from app.data_engine.providers.alpaca.stream import (
    AlpacaMarketDataStream,
    parse_subscription,
    subscription_message,
)
from app.data_engine.providers.base import StreamSubscription
from app.data_engine.schemas import ConnectionState
from app.tests.fixtures.alpaca_stream import (
    FAKE_STREAM_KEY,
    FAKE_STREAM_SECRET,
    FakeAlpacaStreamServer,
    Script,
    bar_msg,
    frame,
    quote_msg,
    trade_msg,
)

ALL = frozenset(StreamChannel)
SUB = StreamSubscription.of(ALL, frozenset({"AAPL", "SPY"}))


def _stream(url: str, **kwargs) -> AlpacaMarketDataStream:
    return AlpacaMarketDataStream(
        url=url,
        feed="iex",
        api_key=SecretStr(FAKE_STREAM_KEY),
        secret_key=SecretStr(FAKE_STREAM_SECRET),
        clock=SystemClock(),
        **kwargs,
    )


def run(coro):
    return asyncio.run(coro)


async def _collect(stream: AlpacaMarketDataStream, count: int, timeout: float = 5.0):
    batches = []

    async def consume():
        async for batch in stream.events():
            batches.append(batch)
            if len(batches) >= count:
                return

    await asyncio.wait_for(consume(), timeout)
    return batches


# ------------------------------------------------------------------ authentication
def test_connect_confirms_authentication_separately_from_the_socket():
    async def scenario():
        async with FakeAlpacaStreamServer() as server:
            stream = _stream(server.url)
            initial = stream.state
            await stream.connect()
            assert initial is ConnectionState.DISCONNECTED
            assert stream.state is ConnectionState.CONNECTED
            assert stream.auth_state is StreamAuthState.AUTHENTICATED
            assert stream.subscription_state is StreamSubscriptionState.NONE
            assert stream.authenticated_at is not None
            await stream.close()
            assert stream.state.value == "STOPPED"
            return server.connections[0]

    conn = run(scenario())
    assert conn.auth_messages == [
        {"action": "auth", "key": FAKE_STREAM_KEY, "secret": FAKE_STREAM_SECRET}
    ]
    # Credentials travel only in the auth message, never in handshake headers.
    assert not any(FAKE_STREAM_SECRET in v for v in conn.headers.values())


def test_rejected_credentials_are_permanent_and_not_retried():
    async def scenario():
        async with FakeAlpacaStreamServer(Script(auth_errors=[(402, "auth failed")])) as server:
            stream = _stream(server.url)
            with pytest.raises(StreamAuthenticationError) as caught:
                await stream.connect()
            return stream, caught.value

    stream, error = run(scenario())
    assert error.permanent is True
    assert error.provider_code == 402
    assert stream.auth_state is StreamAuthState.REJECTED
    assert stream.state is ConnectionState.FAILED


@pytest.mark.parametrize(
    ("code", "message", "permanent"),
    [
        (406, "connection limit exceeded", False),
        (404, "auth timeout", False),
        (500, "internal error", False),
        (409, "insufficient subscription", True),
        (400, "invalid syntax", True),
    ],
)
def test_authentication_errors_map_to_retryable_or_permanent(code, message, permanent):
    async def scenario():
        async with FakeAlpacaStreamServer(Script(auth_errors=[(code, message)])) as server:
            stream = _stream(server.url)
            with pytest.raises((StreamProtocolError, StreamSubscriptionError)) as caught:
                await stream.connect()
            return stream, caught.value

    stream, error = run(scenario())
    assert error.permanent is permanent
    assert error.provider_code == code
    assert stream.auth_state is not StreamAuthState.AUTHENTICATED


def test_silent_server_times_out_instead_of_hanging():
    async def scenario():
        async with FakeAlpacaStreamServer(Script(silent_after_greeting=True)) as server:
            stream = _stream(server.url, reply_timeout=0.3)
            with pytest.raises(StreamProtocolError, match="no reply"):
                await stream.connect()

    run(scenario())


def test_unreachable_server_is_a_retryable_connection_error():
    async def scenario():
        stream = _stream("ws://127.0.0.1:9/v2/iex", open_timeout=1)
        with pytest.raises(StreamConnectionError) as caught:
            await stream.connect()
        return stream, caught.value

    stream, error = run(scenario())
    assert error.permanent is False
    assert stream.state is ConnectionState.DISCONNECTED


# ------------------------------------------------------------------ subscription
def test_subscription_message_and_confirmation():
    assert subscription_message("subscribe", SUB) == {
        "action": "subscribe",
        "bars": ["AAPL", "SPY"],
        "dailyBars": ["AAPL", "SPY"],
        "quotes": ["AAPL", "SPY"],
        "trades": ["AAPL", "SPY"],
        "updatedBars": ["AAPL", "SPY"],
    }
    confirmed, other = parse_subscription(
        {
            "T": "subscription",
            "trades": ["AAPL"],
            "quotes": [],
            "bars": ["AAPL", "SPY"],
            "corrections": ["AAPL"],
            "cancelErrors": ["AAPL"],
        }
    )
    assert confirmed.trades == {"AAPL"}
    assert confirmed.bars == {"AAPL", "SPY"}
    assert confirmed.quotes == frozenset()
    assert other == {"corrections": ["AAPL"], "cancelErrors": ["AAPL"]}
    assert SUB.missing_from(confirmed)[StreamChannel.TRADES] == {"SPY"}


def test_subscribe_waits_for_the_providers_confirmation():
    async def scenario():
        async with FakeAlpacaStreamServer() as server:
            stream = _stream(server.url)
            await stream.connect()
            confirmed = await stream.subscribe(SUB)
            state = (stream.state, stream.subscription_state)
            await stream.close()
            return server.connections[0], confirmed, state, stream.extra_subscriptions

    conn, confirmed, state, extra = run(scenario())
    assert conn.subscribe_messages == [subscription_message("subscribe", SUB)]
    assert confirmed == SUB
    assert state == (ConnectionState.SUBSCRIBED, StreamSubscriptionState.ACTIVE)
    assert extra == {"corrections": ["AAPL", "SPY"], "cancelErrors": ["AAPL", "SPY"]}


def test_partial_confirmation_is_reported_not_assumed():
    async def scenario():
        async with FakeAlpacaStreamServer(Script(unconfirmed={"dailyBars"})) as server:
            stream = _stream(server.url)
            await stream.connect()
            confirmed = await stream.subscribe(SUB)
            await stream.close()
            return stream, confirmed

    stream, confirmed = run(scenario())
    assert confirmed.daily_bars == frozenset()
    assert stream.subscription_state is StreamSubscriptionState.NONE  # reset by close
    assert SUB.missing_from(confirmed) == {StreamChannel.DAILY_BARS: {"AAPL", "SPY"}}


def test_partial_state_before_close():
    async def scenario():
        async with FakeAlpacaStreamServer(Script(unconfirmed={"dailyBars"})) as server:
            stream = _stream(server.url)
            await stream.connect()
            await stream.subscribe(SUB)
            state = stream.subscription_state
            await stream.close()
            return state

    assert run(scenario()) is StreamSubscriptionState.PARTIAL


@pytest.mark.parametrize("code", [405, 409, 410])
def test_refused_subscription_is_permanent(code):
    async def scenario():
        script = Script(subscribe_errors=[(code, "refused")])
        async with FakeAlpacaStreamServer(script) as server:
            stream = _stream(server.url)
            await stream.connect()
            with pytest.raises(StreamSubscriptionError) as caught:
                await stream.subscribe(SUB)
            state = stream.subscription_state
            await stream.close()
            return caught.value, state

    error, state = run(scenario())
    assert error.permanent is True
    assert error.provider_code == code
    assert state is StreamSubscriptionState.REJECTED


def test_subscribe_requires_authentication():
    async def scenario():
        stream = _stream("ws://127.0.0.1:9/v2/iex")
        with pytest.raises(StreamProtocolError, match="before authentication"):
            await stream.subscribe(SUB)

    run(scenario())


def test_data_arriving_before_the_confirmation_is_not_lost():
    early = frame(trade_msg("AAPL", "2026-01-02T14:30:00.000000001Z", i=7))

    async def scenario():
        async with FakeAlpacaStreamServer(Script(early_frames=[early])) as server:
            stream = _stream(server.url)
            await stream.connect()
            await stream.subscribe(SUB)
            batches = await _collect(stream, 1)
            await stream.close()
            return batches

    (batch,) = run(scenario())
    assert batch.trades[0].provider_trade_id == "7"


# ------------------------------------------------------------------ message parsing
def _decode(text: str):
    return _stream("ws://unused").decode(text, received_at=_RECEIVED)


from datetime import UTC, datetime  # noqa: E402

_RECEIVED = datetime(2026, 1, 2, 14, 31, 5, tzinfo=UTC)


def test_minute_updated_and_daily_bars_are_distinguished():
    batch = _decode(
        frame(
            bar_msg("AAPL", "2026-01-02T14:30:00Z", kind="b"),
            bar_msg("AAPL", "2026-01-02T14:30:00Z", kind="u", c=100.3, v=1300),
            bar_msg("AAPL", "2026-01-02T05:00:00Z", kind="d"),
        )
    )
    initial, updated, daily = batch.bars
    assert initial.observation_kind is BarObservationKind.LIVE_INITIAL
    assert updated.observation_kind is BarObservationKind.LIVE_UPDATED
    assert daily.observation_kind is BarObservationKind.LIVE_RUNNING
    assert str(initial.timeframe) == "1Min"
    assert str(daily.timeframe) == "1Day"
    assert initial.is_complete is True  # interval ended before receipt; may still be updated
    assert daily.is_complete is False  # the trading day had not ended
    assert initial.source is DataSource.LIVE_STREAM
    assert initial.original_available_at == _RECEIVED
    assert initial.feed == "iex"
    assert batch.raw_event.message_type == "b,d,u"
    assert batch.raw_event.provider_symbol == "AAPL"


def test_prices_are_exact_decimals_and_nanoseconds_are_kept():
    text = (
        '[{"T":"t","S":"SPY","i":96921,"x":"V","p":512.123456789,"s":0.5,"c":["@","I"],'
        '"z":"B","t":"2026-01-02T14:30:01.123456789Z"},'
        '{"T":"q","S":"SPY","bx":"V","bp":512.1,"bs":1,"ax":"V","ap":512.13,"as":2,'
        '"c":["R"],"z":"B","t":"2026-01-02T14:30:01.000000001Z"}]'
    )
    batch = _decode(text)
    (trade,) = batch.trades
    (quote,) = batch.quotes
    assert trade.price == Decimal("512.123456789")
    assert trade.size == Decimal("0.5")
    assert trade.provider_trade_id == "96921"
    assert trade.conditions == ("@", "I")
    assert trade.event_time_ns == 1767364201123456789
    assert trade.event_time == datetime(2026, 1, 2, 14, 30, 1, 123456, tzinfo=UTC)
    assert quote.event_time_ns == 1767364201000000001
    assert quote.bid_price == Decimal("512.1")
    assert quote.ask_size == Decimal("2")
    assert batch.raw_event.raw_body == text  # verbatim
    assert batch.raw_event.data_type is None  # mixed frame


def test_missing_optional_fields_stay_missing():
    trade = trade_msg("AAPL", "2026-01-02T14:30:00Z")
    del trade["s"]
    bar = bar_msg("AAPL", "2026-01-02T14:30:00Z", v=None, vw=None, n=None)
    quote = quote_msg("AAPL", "2026-01-02T14:30:00Z")
    del quote["bp"], quote["bs"]
    batch = _decode(frame(trade, bar, quote))
    assert batch.trades[0].size is None
    assert batch.bars[0].volume is None
    assert batch.bars[0].vwap is None
    assert batch.bars[0].trade_count is None
    assert batch.quotes[0].bid_price is None
    assert batch.quotes[0].bid_size is None


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        ({"T": "t", "S": "AAPL", "p": 1, "t": "not-a-time"}, "invalid RFC-3339"),
        ({"T": "t", "S": "AAPL", "t": "2026-01-02T14:30:00Z"}, "missing required field 'p'"),
        ({"T": "q", "bp": 1, "t": "2026-01-02T14:30:00Z"}, "missing required field 'S'"),
        (
            {"T": "b", "S": "AAPL", "o": "1", "h": 1, "l": 1, "c": 1, "t": "2026-01-02T14:30:00Z"},
            "not a number",
        ),
        ({"T": "t", "S": "AAPL", "p": 1, "t": "2026-01-02T14:30:00Z", "i": True}, "not an integer"),
        ({"T": "t", "S": "AAPL", "p": "NaN", "t": "2026-01-02T14:30:00Z"}, "not a number"),
    ],
)
def test_invalid_messages_are_reported_as_malformed(message, reason):
    batch = _decode(frame(message, trade_msg("SPY", "2026-01-02T14:30:00Z")))
    assert len(batch.trades) == 1  # the valid item in the same frame survives
    (bad,) = batch.malformed
    assert reason in bad.reason


@pytest.mark.parametrize(
    ("text", "reason"),
    [("not json", "not valid JSON"), ('{"T":"t"}', "not a JSON array"), ("[1]", "not an object")],
)
def test_unparseable_frames_are_kept_and_reported(text, reason):
    batch = _decode(text)
    assert batch.item_count == 1
    assert reason in batch.malformed[0].reason
    assert batch.raw_event.raw_body == text


def test_binary_frames_are_reported():
    batch = _stream("ws://unused").decode(b"\x81\x00", received_at=_RECEIVED)
    assert "binary frame" in batch.malformed[0].reason


def test_corrections_cancels_and_unknown_messages_become_notices():
    batch = _decode(
        frame(
            {
                "T": "c",
                "S": "AAPL",
                "x": "V",
                "oi": 1,
                "op": 100,
                "os": 5,
                "ci": 2,
                "cp": 101,
                "cs": 5,
                "t": "2026-01-02T14:30:00.5Z",
                "z": "C",
            },
            {
                "T": "x",
                "S": "AAPL",
                "i": 1,
                "x": "V",
                "p": 100,
                "s": 5,
                "a": "C",
                "t": "2026-01-02T14:30:00.5Z",
                "z": "C",
            },
            {"T": "s", "S": "AAPL", "sc": "H"},
        )
    )
    kinds = [n.kind for n in batch.notices]
    assert kinds == ["trade_correction", "trade_cancel", "unhandled_message:s"]
    assert batch.notices[0].details["cp"] == 101
    assert batch.notices[0].event_time == datetime(2026, 1, 2, 14, 30, 0, 500000, tzinfo=UTC)
    assert not batch.trades


# ------------------------------------------------------------------ streaming and closing
def test_events_and_unexpected_disconnect():
    frames = [frame(trade_msg("AAPL", f"2026-01-02T14:30:0{i}Z", i=i)) for i in range(3)]

    async def scenario():
        script = Script(frames=[frames], drop_after_frames=[True])
        async with FakeAlpacaStreamServer(script) as server:
            stream = _stream(server.url)
            await stream.connect()
            await stream.subscribe(SUB)
            received = []

            async def consume():
                async for batch in stream.events():
                    received.append(batch)

            with pytest.raises(StreamConnectionError, match="closed unexpectedly"):
                await consume()
            state = (stream.state, stream.subscription_state)
            await stream.close()
            return received, state

    received, state = run(scenario())
    assert [b.trades[0].provider_trade_id for b in received] == ["0", "1", "2"]
    assert state == (ConnectionState.DISCONNECTED, StreamSubscriptionState.NONE)


def test_close_ends_the_event_iterator_gracefully():
    async def scenario():
        async with FakeAlpacaStreamServer() as server:
            stream = _stream(server.url)
            await stream.connect()
            await stream.subscribe(SUB)

            async def consume():
                return [b async for b in stream.events()]

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.1)
            await stream.close()
            return await asyncio.wait_for(task, 5)

    assert run(scenario()) == []


def test_credentials_never_reach_logs_even_at_debug(caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with FakeAlpacaStreamServer(Script(auth_errors=[(402, "auth failed")])) as server:
            stream = _stream(server.url)
            with pytest.raises(StreamAuthenticationError) as caught:
                await stream.connect()
            return str(caught.value), repr(stream)

    message, representation = run(scenario())
    text = caplog.text + message + representation
    assert FAKE_STREAM_SECRET not in text
    assert FAKE_STREAM_KEY not in text


def test_reconnect_needs_a_new_stream_object():
    async def scenario():
        async with FakeAlpacaStreamServer() as server:
            stream = _stream(server.url)
            await stream.connect()
            with pytest.raises(StreamProtocolError, match="already connected"):
                await stream.connect()
            await stream.close()

    run(scenario())


def test_subscription_helpers():
    sub = StreamSubscription.of(frozenset({StreamChannel.TRADES}), frozenset({"AAPL"}))
    assert sub.channels == {StreamChannel.TRADES}
    assert sub.symbols == {"AAPL"}
    assert sub.as_dict() == {"trades": ["AAPL"]}
    assert StreamSubscription().is_empty
    assert json.dumps(SUB.as_dict())
