"""Alpaca real-time stock data over WebSocket (Deliverable 2C).

Protocol (https://docs.alpaca.markets/us/docs/streaming-market-data, checked 2026-10-29):

1. Connect to ``wss://stream.data.alpaca.markets/v2/{feed}``; the server greets with
   ``[{"T":"success","msg":"connected"}]``.
2. Within 10 seconds send ``{"action":"auth","key":...,"secret":...}``; success is
   ``[{"T":"success","msg":"authenticated"}]``. A feed outside the subscription fails here.
3. Send ``{"action":"subscribe","bars":[...],"trades":[...],...}``; the server answers
   with ``[{"T":"subscription",...}]`` listing the *complete* current subscription.
4. Data arrives as JSON arrays of objects keyed by ``T``; control messages are always
   single-element arrays. Errors are ``{"T":"error","code":N,"msg":...}``.

Each state is confirmed separately: an open socket is not authentication, and a sent
subscribe is not a subscription. Alpaca documents no application heartbeat, so liveness
uses WebSocket ping/pong: no pong within the heartbeat timeout closes the connection,
which the worker treats as a disconnect.

Credentials are sent only inside the auth message. The ``websockets`` library logs
frames and headers at DEBUG level, so its logger is pinned to WARNING here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.enums import (
    DataSource,
    DataType,
    StreamAuthState,
    StreamChannel,
    StreamSubscriptionState,
)
from app.data_engine.errors import (
    StreamAuthenticationError,
    StreamConnectionError,
    StreamError,
    StreamProtocolError,
    StreamSubscriptionError,
)
from app.data_engine.providers.alpaca.capabilities import PROVIDER_ID
from app.data_engine.providers.alpaca.normalize import (
    BAR_MESSAGE_TYPES,
    BarFormatError,
    normalize_quote,
    normalize_stream_bar,
    normalize_trade,
    parse_timestamp_ns,
)
from app.data_engine.providers.base import (
    MalformedRecord,
    StreamBatch,
    StreamNotice,
    StreamSubscription,
)
from app.data_engine.schemas import (
    ConnectionState,
    MarketDataEvent,
    ProviderBar,
    ProviderQuote,
    ProviderTrade,
)

logger = get_logger(__name__)

# Never let the websockets library log frames (the auth frame carries the secret).
_WS_LOGGER = logging.getLogger("app.data_engine.alpaca.websocket")
_WS_LOGGER.setLevel(logging.WARNING)

# Atlas channel -> Alpaca subscription key.
CHANNEL_KEYS: dict[StreamChannel, str] = {
    StreamChannel.BARS: "bars",
    StreamChannel.UPDATED_BARS: "updatedBars",
    StreamChannel.DAILY_BARS: "dailyBars",
    StreamChannel.TRADES: "trades",
    StreamChannel.QUOTES: "quotes",
}
_KEY_CHANNELS = {key: channel for channel, key in CHANNEL_KEYS.items()}

# Documented error codes. Permanent ones are not retried automatically.
ERROR_CODES: dict[int, str] = {
    400: "invalid syntax",
    401: "not authenticated",
    402: "auth failed",
    403: "already authenticated",
    404: "auth timeout",
    405: "symbol limit exceeded",
    406: "connection limit exceeded",
    407: "slow client",
    409: "insufficient subscription",
    410: "invalid subscribe action for this feed",
    500: "internal error",
}
_AUTH_FAILURES = frozenset({402})
_SUBSCRIPTION_FAILURES = frozenset({405, 409, 410})
# 400 means Atlas sent something Alpaca cannot parse: a bug, not a transient fault.
_PERMANENT_PROTOCOL_FAILURES = frozenset({400})

MAX_FRAME_BYTES = 8 * 1024 * 1024
_TRADE_NOTICES = {"c": "trade_correction", "x": "trade_cancel"}

Connector = Callable[..., Any]


def _error(item: dict[str, Any], during: str) -> StreamError:
    code = item.get("code")
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    text = item.get("msg")
    message = redact_text(str(text if isinstance(text, str) else ERROR_CODES.get(code or 0, "")))
    description = f"Alpaca stream error {code} during {during}: {message}"
    if code in _AUTH_FAILURES:
        return StreamAuthenticationError(description, provider_code=code)
    if code in _SUBSCRIPTION_FAILURES:
        return StreamSubscriptionError(description, provider_code=code)
    error = StreamProtocolError(description, provider_code=code)
    if code in _PERMANENT_PROTOCOL_FAILURES:
        error.permanent = True
    return error


def subscription_message(action: str, subscription: StreamSubscription) -> dict[str, Any]:
    message: dict[str, Any] = {"action": action}
    for channel in sorted(subscription.channels):
        message[CHANNEL_KEYS[channel]] = sorted(subscription.channel(channel))
    return message


def parse_subscription(item: dict[str, Any]) -> tuple[StreamSubscription, dict[str, list[str]]]:
    """Confirmed subscription, plus keys Atlas does not consume (e.g. ``corrections``)."""
    channels: dict[str, frozenset[str]] = {}
    other: dict[str, list[str]] = {}
    for key, value in item.items():
        if key == "T":
            continue
        symbols = [s for s in value if isinstance(s, str)] if isinstance(value, list) else []
        if key in _KEY_CHANNELS:
            channels[_KEY_CHANNELS[key].value] = frozenset(symbols)
        elif symbols:
            other[key] = sorted(symbols)
    return StreamSubscription(**channels), other


class AlpacaStreamConnection:
    """Transport, authentication and control-message handling shared by Alpaca's WebSocket
    endpoints (market data, Deliverable 2C; news, Phase 3C), which use the same protocol.
    Subclasses add their subscription format and decoding. Not reusable after ``close``."""

    user_agent = "atlas-ai-trader (read-only data)"

    def __init__(
        self,
        *,
        url: str,
        feed: str,
        api_key: SecretStr,
        secret_key: SecretStr,
        clock: Clock,
        heartbeat_timeout: float = 30.0,
        open_timeout: float = 10.0,
        reply_timeout: float = 10.0,
        connector: Connector = connect,
    ) -> None:
        self._url = url
        self._feed = feed
        self._key = api_key
        self._secret = secret_key
        self._clock = clock
        self._heartbeat_timeout = heartbeat_timeout
        self._open_timeout = open_timeout
        self._reply_timeout = reply_timeout
        self._connector = connector
        self._ws: ClientConnection | None = None
        self._closing = False
        self._pending: list[tuple[str | bytes, datetime]] = []
        self._state = ConnectionState.DISCONNECTED
        self._auth_state = StreamAuthState.NOT_ATTEMPTED
        self._subscription_state = StreamSubscriptionState.NONE
        self.authenticated_at: datetime | None = None
        self.subscribed_at: datetime | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self._url!r}, state={self._state.value})"

    # ------------------------------------------------------------ state
    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def auth_state(self) -> StreamAuthState:
        return self._auth_state

    @property
    def subscription_state(self) -> StreamSubscriptionState:
        return self._subscription_state

    @property
    def url(self) -> str:
        return self._url

    # ------------------------------------------------------------ connection
    async def connect(self) -> None:
        if self._ws is not None:
            raise StreamProtocolError("stream already connected; create a new stream instead")
        self._state = ConnectionState.CONNECTING
        try:
            # Ping/pong is the heartbeat: no pong within ping_timeout closes with 1011.
            self._ws = await self._connector(
                self._url,
                open_timeout=self._open_timeout,
                ping_interval=max(1.0, self._heartbeat_timeout / 2),
                ping_timeout=self._heartbeat_timeout,
                close_timeout=5,
                max_size=MAX_FRAME_BYTES,
                max_queue=64,
                logger=_WS_LOGGER,
                user_agent_header=self.user_agent,
            )
        except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as exc:
            self._state = ConnectionState.DISCONNECTED
            raise StreamConnectionError(
                f"could not connect to the Alpaca stream: {type(exc).__name__}"
            ) from None

        try:
            welcome = await self._control("connection")
            if welcome.get("T") != "success" or welcome.get("msg") != "connected":
                raise StreamProtocolError(f"unexpected greeting: {_describe(welcome)}")

            self._state = ConnectionState.AUTHENTICATING
            self._auth_state = StreamAuthState.PENDING
            await self._send(
                {
                    "action": "auth",
                    "key": self._key.get_secret_value(),
                    "secret": self._secret.get_secret_value(),
                }
            )
            reply = await self._control("authentication")
            if reply.get("T") != "success" or reply.get("msg") != "authenticated":
                raise StreamProtocolError(f"unexpected auth reply: {_describe(reply)}")
        except StreamAuthenticationError:
            self._auth_state = StreamAuthState.REJECTED
            await self._abort(ConnectionState.FAILED)
            raise
        except StreamError:
            if self._auth_state is StreamAuthState.PENDING:
                self._auth_state = StreamAuthState.NOT_ATTEMPTED
            await self._abort(ConnectionState.DISCONNECTED)
            raise
        self._auth_state = StreamAuthState.AUTHENTICATED
        self.authenticated_at = self._clock.now()
        self._state = ConnectionState.CONNECTED
        logger.info("alpaca.stream_authenticated", feed=self._feed)

    async def close(self) -> None:
        self._closing = True
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # closing a broken socket must not fail shutdown
                logger.debug("alpaca.stream_close_failed")
        self._state = ConnectionState.STOPPED
        self._subscription_state = StreamSubscriptionState.NONE

    # ------------------------------------------------------------ transport
    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is None:
            raise StreamConnectionError("stream is not connected")
        try:
            await self._ws.send(json.dumps(message, separators=(",", ":")))
        except ConnectionClosed as exc:
            raise self._closed_error(exc) from None

    async def _receive(self) -> tuple[str | bytes | None, datetime]:
        if self._ws is None:
            if self._closing:
                return None, self._clock.now()
            raise StreamConnectionError("stream is not connected")
        try:
            frame = await self._ws.recv()
        except ConnectionClosed as exc:
            if self._closing:
                return None, self._clock.now()
            self._state = ConnectionState.DISCONNECTED
            self._subscription_state = StreamSubscriptionState.NONE
            raise self._closed_error(exc) from None
        return frame, self._clock.now()

    async def _control(self, during: str) -> dict[str, Any]:
        """Wait for the next control message; data frames meanwhile are kept for ``events``."""
        try:
            async with asyncio.timeout(self._reply_timeout):
                while True:
                    frame, received_at = await self._receive()
                    if frame is None:
                        raise StreamConnectionError(f"stream closed during {during}")
                    control = _control_item(frame)
                    if control is None:
                        self._pending.append((frame, received_at))
                        continue
                    if control.get("T") == "error":
                        raise _error(control, during)
                    return control
        except TimeoutError:
            raise StreamProtocolError(
                f"no reply from Alpaca within {self._reply_timeout}s during {during}"
            ) from None

    async def _abort(self, state: ConnectionState) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("alpaca.stream_abort_close_failed")
        self._state = state

    def _closed_error(self, exc: ConnectionClosed) -> StreamConnectionError:
        close = exc.rcvd or exc.sent
        if close is None:
            return StreamConnectionError(
                "Alpaca stream closed unexpectedly (connection lost without a close frame)"
            )
        reason = f": {redact_text(close.reason)}" if close.reason else ""
        return StreamConnectionError(
            f"Alpaca stream closed unexpectedly (code {close.code}{reason})"
        )


class AlpacaMarketDataStream(AlpacaStreamConnection):
    """One Alpaca market-data stream connection. Not reusable after ``close``; the worker
    makes a new one for every reconnect and restores the subscription."""

    user_agent = "atlas-ai-trader (market data only)"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._subscription = StreamSubscription()
        self.extra_subscriptions: dict[str, list[str]] = {}

    @property
    def subscription(self) -> StreamSubscription:
        return self._subscription

    async def subscribe(self, subscription: StreamSubscription) -> StreamSubscription:
        return await self._change_subscription("subscribe", subscription)

    async def unsubscribe(self, subscription: StreamSubscription) -> StreamSubscription:
        return await self._change_subscription("unsubscribe", subscription)

    async def _change_subscription(
        self, action: str, subscription: StreamSubscription
    ) -> StreamSubscription:
        if self._auth_state is not StreamAuthState.AUTHENTICATED:
            raise StreamProtocolError(f"cannot {action} before authentication")
        if subscription.is_empty:
            return self._subscription
        self._state = ConnectionState.SUBSCRIBING
        self._subscription_state = StreamSubscriptionState.PENDING
        try:
            await self._send(subscription_message(action, subscription))
            reply = await self._control(action)
        except StreamSubscriptionError:
            self._subscription_state = StreamSubscriptionState.REJECTED
            self._state = ConnectionState.CONNECTED
            raise
        except StreamError:
            self._subscription_state = StreamSubscriptionState.NONE
            raise
        if reply.get("T") != "subscription":
            raise StreamProtocolError(f"unexpected {action} reply: {_describe(reply)}")
        confirmed, self.extra_subscriptions = parse_subscription(reply)
        self._subscription = confirmed
        if action == "subscribe":
            complete = not subscription.missing_from(confirmed)
        else:
            complete = not any(
                confirmed.channel(c) & subscription.channel(c) for c in confirmed.channels
            )
        self._subscription_state = (
            StreamSubscriptionState.ACTIVE
            if complete and not confirmed.is_empty
            else StreamSubscriptionState.PARTIAL
            if not confirmed.is_empty
            else StreamSubscriptionState.NONE
        )
        self._state = (
            ConnectionState.SUBSCRIBED if not confirmed.is_empty else ConnectionState.CONNECTED
        )
        self.subscribed_at = self._clock.now()
        logger.info(
            "alpaca.stream_subscription",
            action=action,
            state=self._subscription_state.value,
            subscription=confirmed.as_dict(),
        )
        return confirmed

    async def events(self) -> AsyncIterator[StreamBatch]:
        while self._pending:
            frame, received_at = self._pending.pop(0)
            yield self.decode(frame, received_at)
        while True:
            received, at = await self._receive()
            if received is None:
                return
            yield self.decode(received, at)

    # ------------------------------------------------------------ decoding
    def decode(self, frame: str | bytes, received_at: datetime) -> StreamBatch:
        """Decode one frame. Never raises for bad content: problems become ``malformed``."""
        text = frame if isinstance(frame, str) else None
        items: list[Any]
        malformed: list[MalformedRecord] = []
        if text is None:
            items = []
            malformed.append(
                MalformedRecord(
                    provider_symbol=None,
                    reason="binary frame received; Atlas requests JSON text frames",
                    item={"bytes": len(frame)},
                )
            )
        else:
            try:
                body = json.loads(text, parse_float=Decimal)
            except ValueError:
                body = None
                malformed.append(
                    MalformedRecord(provider_symbol=None, reason="frame is not valid JSON", item={})
                )
            if body is None:
                items = []
            elif isinstance(body, list):
                items = body
            else:
                items = []
                malformed.append(
                    MalformedRecord(
                        provider_symbol=None, reason="frame is not a JSON array", item={}
                    )
                )

        bars: list[ProviderBar] = []
        quotes: list[ProviderQuote] = []
        trades: list[ProviderTrade] = []
        notices: list[StreamNotice] = []
        types: set[str] = set()
        symbols: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                malformed.append(
                    MalformedRecord(
                        provider_symbol=None,
                        reason="item is not an object",
                        item={"value": repr(item)[:200]},
                    )
                )
                continue
            kind = item.get("T")
            types.add(str(kind))
            symbol = item.get("S") if isinstance(item.get("S"), str) else None
            if symbol:
                symbols.add(symbol)
            try:
                if kind in BAR_MESSAGE_TYPES:
                    bars.append(
                        normalize_stream_bar(item, feed=self._feed, received_at=received_at)
                    )
                elif kind == "q":
                    quotes.append(normalize_quote(item, feed=self._feed, received_at=received_at))
                elif kind == "t":
                    trades.append(normalize_trade(item, feed=self._feed, received_at=received_at))
                elif kind in _TRADE_NOTICES:
                    notices.append(self._trade_notice(str(kind), item))
                elif kind in ("error", "success", "subscription"):
                    notices.append(self._control_notice(item))
                else:
                    notices.append(
                        StreamNotice(
                            kind=f"unhandled_message:{str(kind)[:16]}",
                            provider_symbol=symbol,
                            details=_jsonable(item),
                        )
                    )
            except BarFormatError as exc:
                malformed.append(
                    MalformedRecord(provider_symbol=symbol, reason=str(exc), item=_jsonable(item))
                )

        data_types = {
            DataType.BAR
            if t in BAR_MESSAGE_TYPES
            else DataType.QUOTE
            if t == "q"
            else DataType.TRADE
            for t in types
            if t in BAR_MESSAGE_TYPES or t in ("q", "t")
        }
        raw = MarketDataEvent(
            provider=PROVIDER_ID,
            feed=self._feed,
            source=DataSource.LIVE_STREAM,
            message_type=(",".join(sorted(types)) or "invalid")[:64],
            data_type=data_types.pop() if len(data_types) == 1 else None,
            provider_symbol=symbols.pop() if len(symbols) == 1 else None,
            received_at=received_at,
            payload={},  # the verbatim frame is in raw_body
            raw_body=text if text is not None else f"<binary frame, {len(frame)} bytes>",
        )
        return StreamBatch(
            raw_event=raw,
            bars=tuple(bars),
            quotes=tuple(quotes),
            trades=tuple(trades),
            notices=tuple(notices),
            malformed=tuple(malformed),
        )

    def _trade_notice(self, kind: str, item: dict[str, Any]) -> StreamNotice:
        event_time = None
        with contextlib.suppress(BarFormatError):
            event_time = parse_timestamp_ns(item.get("t"))[0]
        symbol = item.get("S")
        return StreamNotice(
            kind=_TRADE_NOTICES[kind],
            provider_symbol=symbol if isinstance(symbol, str) else None,
            event_time=event_time,
            data_type=DataType.TRADE,
            details=_jsonable(item),
        )

    def _control_notice(self, item: dict[str, Any]) -> StreamNotice:
        if item.get("T") == "subscription":
            self._subscription, self.extra_subscriptions = parse_subscription(item)
        return StreamNotice(kind=f"control:{item.get('T')}", details=_jsonable(item))


def _control_item(frame: str | bytes) -> dict[str, Any] | None:
    """The control object if ``frame`` is a single control message, else ``None``."""
    if not isinstance(frame, str):
        return None
    try:
        body = json.loads(frame, parse_float=Decimal)
    except ValueError:
        return None
    if (
        isinstance(body, list)
        and len(body) == 1
        and isinstance(body[0], dict)
        and body[0].get("T") in ("success", "error", "subscription")
    ):
        item: dict[str, Any] = body[0]
        return item
    return None


def _describe(item: dict[str, Any]) -> str:
    return redact_text(json.dumps(_jsonable(item))[:200])


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


__all__ = [
    "CHANNEL_KEYS",
    "ERROR_CODES",
    "AlpacaMarketDataStream",
    "AlpacaStreamConnection",
    "parse_subscription",
    "subscription_message",
]
