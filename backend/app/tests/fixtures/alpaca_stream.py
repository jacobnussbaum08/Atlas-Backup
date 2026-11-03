"""TEST FIXTURE: a scriptable fake of Alpaca's real-time stock data WebSocket protocol.

It is a real WebSocket server on 127.0.0.1 speaking the *documented* protocol
(greeting, auth, subscribe/subscription confirmation, error codes, JSON arrays of
``T``-typed messages), so the client's real network code runs. Every message it sends
is constructed test data. It is NOT Alpaca and NOT real market data; tests that use it
are mock-provider tests, never connectivity tests. Real-provider tests live in
``tests/live`` (opt-in).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

_QUIET = logging.getLogger("app.tests.fake_alpaca_stream")
_QUIET.setLevel(logging.WARNING)

FAKE_STREAM_KEY = "TESTKEYFIXTURE0001"
FAKE_STREAM_SECRET = "TestSecretFixtureNotReal0001"

_KEYS = ("trades", "quotes", "bars", "updatedBars", "dailyBars", "news")


def bar_msg(
    symbol: str,
    t: str,
    *,
    kind: str = "b",
    o: Any = 100.1,
    h: Any = 100.5,
    low: Any = 99.9,
    c: Any = 100.2,
    v: Any = 1200,
    n: Any = 15,
    vw: Any = 100.21,
) -> dict[str, Any]:
    msg = {"T": kind, "S": symbol, "o": o, "h": h, "l": low, "c": c, "t": t}
    for key, value in (("v", v), ("n", n), ("vw", vw)):
        if value is not None:
            msg[key] = value
    return msg


def quote_msg(
    symbol: str,
    t: str,
    *,
    bp: Any = 100.1,
    bs: Any = 2,
    ap: Any = 100.12,
    as_: Any = 3,
    bx: str = "V",
    ax: str = "V",
) -> dict[str, Any]:
    return {
        "T": "q",
        "S": symbol,
        "bx": bx,
        "bp": bp,
        "bs": bs,
        "ax": ax,
        "ap": ap,
        "as": as_,
        "c": ["R"],
        "z": "C",
        "t": t,
    }


def trade_msg(
    symbol: str, t: str, *, i: int = 1, p: Any = 100.11, s: Any = 10, x: str = "V"
) -> dict[str, Any]:
    return {"T": "t", "S": symbol, "i": i, "x": x, "p": p, "s": s, "c": ["@"], "z": "C", "t": t}


def frame(*messages: dict[str, Any]) -> str:
    """One frame; numbers written as JSON numbers (decimal text, never binary floats)."""
    return json.dumps(list(messages))


@dataclass
class Connection:
    """What the fake server saw on one connection."""

    received: list[dict[str, Any]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def auth_messages(self) -> list[dict[str, Any]]:
        return [m for m in self.received if m.get("action") == "auth"]

    @property
    def subscribe_messages(self) -> list[dict[str, Any]]:
        return [m for m in self.received if m.get("action") == "subscribe"]


@dataclass
class Script:
    """Per-connection behaviour. Index ``n`` of a list applies to the n-th connection
    (the last entry repeats)."""

    auth_errors: list[tuple[int, str] | None] = field(default_factory=lambda: [None])
    subscribe_errors: list[tuple[int, str] | None] = field(default_factory=lambda: [None])
    # Channels the server refuses to confirm (e.g. {"dailyBars"}): a partial subscription.
    unconfirmed: set[str] = field(default_factory=set)
    # Frames sent after the subscription confirmation, per connection.
    frames: list[list[str]] = field(default_factory=lambda: [[]])
    # Frames sent *before* the confirmation (data racing the control message).
    early_frames: list[str] = field(default_factory=list)
    # After sending its frames, close the connection abruptly (simulated network drop).
    drop_after_frames: list[bool] = field(default_factory=lambda: [False])
    send_greeting: bool = True
    silent_after_greeting: bool = False
    frame_delay: float = 0.0

    @staticmethod
    def pick(values: list[Any], index: int) -> Any:
        return values[min(index, len(values) - 1)]


class FakeAlpacaStreamServer:
    """``async with FakeAlpacaStreamServer(script) as server: server.url``"""

    def __init__(self, script: Script | None = None) -> None:
        self.script = script or Script()
        self.connections: list[Connection] = []
        self.url = ""
        self._server: Any = None
        self.release = asyncio.Event()  # set to let held connections finish
        self.release.set()

    async def __aenter__(self) -> FakeAlpacaStreamServer:
        self._server = await serve(self._handler, "127.0.0.1", 0, logger=_QUIET)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/v2/iex"
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), timeout=5)

    async def wait_for_connections(self, count: int, timeout: float = 10.0) -> None:
        async with asyncio.timeout(timeout):
            while len(self.connections) < count:
                await asyncio.sleep(0.01)

    async def _handler(self, ws: ServerConnection) -> None:
        index = len(self.connections)
        conn = Connection(headers=dict(ws.request.headers) if ws.request else {})
        self.connections.append(conn)
        script = self.script
        if script.send_greeting:
            await ws.send(frame({"T": "success", "msg": "connected"}))
        if script.silent_after_greeting:
            await ws.wait_closed()
            return
        subscribed: dict[str, set[str]] = {key: set() for key in _KEYS}
        try:
            async for raw in self._messages(ws, conn):
                action = raw.get("action")
                if action == "auth":
                    error = Script.pick(script.auth_errors, index)
                    if error is not None:
                        await ws.send(frame({"T": "error", "code": error[0], "msg": error[1]}))
                        await ws.close()
                        return
                    await ws.send(frame({"T": "success", "msg": "authenticated"}))
                elif action == "subscribe":
                    for text in script.early_frames:
                        await ws.send(text)
                    error = Script.pick(script.subscribe_errors, index)
                    if error is not None:
                        await ws.send(frame({"T": "error", "code": error[0], "msg": error[1]}))
                        continue
                    for key in _KEYS:
                        if key not in script.unconfirmed:
                            subscribed[key] |= set(raw.get(key, []))
                    confirmation: dict[str, Any] = {"T": "subscription"}
                    confirmation |= {k: sorted(v) for k, v in subscribed.items()}
                    confirmation["corrections"] = sorted(subscribed["trades"])
                    confirmation["cancelErrors"] = sorted(subscribed["trades"])
                    await ws.send(frame(confirmation))
                    for text in Script.pick(script.frames, index):
                        if script.frame_delay:
                            await asyncio.sleep(script.frame_delay)
                        await ws.send(text)
                    if Script.pick(script.drop_after_frames, index):
                        await asyncio.sleep(0.1)  # let sent frames reach the client
                        ws.transport.abort()  # abrupt: no close frame, like a network drop
                        return
                else:
                    await ws.send(frame({"T": "error", "code": 400, "msg": "invalid syntax"}))
        except Exception:  # noqa: S110  # the client going away ends the handler
            pass

    @staticmethod
    async def _messages(ws: ServerConnection, conn: Connection) -> AsyncIterator[dict[str, Any]]:
        async for text in ws:
            message = json.loads(text)
            conn.received.append(message)
            yield message


async def eventually(
    check: Callable[[], bool], timeout: float = 10.0, interval: float = 0.02
) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(interval)
