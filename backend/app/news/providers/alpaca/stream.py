"""Alpaca real-time news over WebSocket (Phase 3C).

Protocol (https://docs.alpaca.markets/us/docs/streaming-real-time-news, checked 2026-11-02):
``wss://stream.data.alpaca.markets/v1beta1/news``, a separate endpoint from the stock
stream with the same message protocol: greeting ``{"T":"success","msg":"connected"}``,
``{"action":"auth",...}`` -> ``{"T":"success","msg":"authenticated"}``, then
``{"action":"subscribe","news":["*"]}`` -> ``{"T":"subscription","news":["*"]}``. Articles
arrive as ``{"T":"n", ...}`` objects in JSON arrays. ``*`` subscribes to all news.

Transport, authentication, error codes and ping/pong liveness are the shared
:class:`AlpacaStreamConnection`; this class adds the news subscription and decoding. The
connection is opened only by the news stream worker (one per provider), never per
dashboard user, and is independent of the market-data stream worker's connection.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.data_engine.enums import StreamAuthState, StreamSubscriptionState
from app.data_engine.errors import StreamError, StreamProtocolError, StreamSubscriptionError
from app.data_engine.providers.alpaca.stream import AlpacaStreamConnection
from app.data_engine.schemas import ConnectionState
from app.news.enums import ArticleOrigin
from app.news.providers.alpaca.normalize import NewsFormatError, normalize_article
from app.news.schemas import MalformedArticle, NewsStreamBatch, ProviderNewsArticle

logger = get_logger(__name__)

NEWS_MESSAGE_TYPE = "n"
_CONTROL_TYPES = frozenset({"success", "error", "subscription"})


class AlpacaNewsStream(AlpacaStreamConnection):
    user_agent = "atlas-ai-trader (news, read-only)"

    def __init__(self, *, store_content: bool, **kwargs: Any) -> None:
        super().__init__(feed="news", **kwargs)
        self._store_content = store_content
        self._confirmed: frozenset[str] = frozenset()

    @property
    def confirmed_symbols(self) -> frozenset[str]:
        return self._confirmed

    async def subscribe(self, symbols: frozenset[str]) -> frozenset[str]:
        if self._auth_state is not StreamAuthState.AUTHENTICATED:
            raise StreamProtocolError("cannot subscribe before authentication")
        if not symbols:
            raise StreamProtocolError("a news subscription needs '*' or at least one symbol")
        self._state = ConnectionState.SUBSCRIBING
        self._subscription_state = StreamSubscriptionState.PENDING
        try:
            await self._send({"action": "subscribe", "news": sorted(symbols)})
            reply = await self._control("subscribe")
        except StreamSubscriptionError:
            self._subscription_state = StreamSubscriptionState.REJECTED
            self._state = ConnectionState.CONNECTED
            raise
        except StreamError:
            self._subscription_state = StreamSubscriptionState.NONE
            raise
        if reply.get("T") != "subscription":
            raise StreamProtocolError(
                f"unexpected subscribe reply: {redact_text(json.dumps(reply)[:200])}"
            )
        self._confirmed = _news_symbols(reply)
        missing = symbols - self._confirmed
        self._subscription_state = (
            StreamSubscriptionState.ACTIVE
            if self._confirmed and not missing
            else StreamSubscriptionState.PARTIAL
            if self._confirmed
            else StreamSubscriptionState.NONE
        )
        self._state = ConnectionState.SUBSCRIBED if self._confirmed else ConnectionState.CONNECTED
        self.subscribed_at = self._clock.now()
        logger.info(
            "alpaca.news_subscription",
            state=self._subscription_state.value,
            symbols=sorted(self._confirmed)[:50],
        )
        return self._confirmed

    async def events(self) -> AsyncIterator[NewsStreamBatch]:
        while self._pending:
            frame, received_at = self._pending.pop(0)
            yield self.decode(frame, received_at)
        while True:
            received, at = await self._receive()
            if received is None:
                return
            yield self.decode(received, at)

    def decode(self, frame: str | bytes, received_at: datetime) -> NewsStreamBatch:
        """Decode one frame. Never raises for bad content: problems become ``malformed``."""
        if not isinstance(frame, str):
            return NewsStreamBatch(
                received_at=received_at,
                malformed=(
                    MalformedArticle(
                        provider_article_id=None,
                        reason="binary frame received; Atlas requests JSON text frames",
                        item={"bytes": len(frame)},
                    ),
                ),
            )
        try:
            body = json.loads(frame)
        except ValueError:
            return _malformed(received_at, "frame is not valid JSON")
        if not isinstance(body, list):
            return _malformed(received_at, "frame is not a JSON array")

        articles: list[ProviderNewsArticle] = []
        malformed: list[MalformedArticle] = []
        notices: list[dict[str, Any]] = []
        for item in body:
            if not isinstance(item, dict):
                malformed.append(
                    MalformedArticle(
                        provider_article_id=None,
                        reason="item is not an object",
                        item={"value": repr(item)[:200]},
                    )
                )
                continue
            kind = item.get("T")
            if kind == NEWS_MESSAGE_TYPE:
                try:
                    articles.append(
                        normalize_article(
                            item,
                            origin=ArticleOrigin.REALTIME_STREAM,
                            received_at=received_at,
                            store_content=self._store_content,
                        )
                    )
                except (NewsFormatError, ValueError) as exc:
                    malformed.append(
                        MalformedArticle(
                            provider_article_id=str(item.get("id"))[:64],
                            reason=str(exc)[:300],
                            item={k: v for k, v in item.items() if k != "content"},
                        )
                    )
            elif kind in _CONTROL_TYPES:
                if kind == "subscription":
                    self._confirmed = _news_symbols(item)
                notices.append({"kind": f"control:{kind}", "details": _redacted(item)})
            else:
                notices.append(
                    {"kind": f"unhandled_message:{str(kind)[:16]}", "details": _redacted(item)}
                )
        return NewsStreamBatch(
            received_at=received_at,
            articles=tuple(articles),
            malformed=tuple(malformed),
            notices=tuple(notices),
        )


def _news_symbols(item: dict[str, Any]) -> frozenset[str]:
    value = item.get("news")
    if not isinstance(value, list):
        return frozenset()
    return frozenset(s for s in value if isinstance(s, str))


def _malformed(received_at: datetime, reason: str) -> NewsStreamBatch:
    return NewsStreamBatch(
        received_at=received_at,
        malformed=(MalformedArticle(provider_article_id=None, reason=reason, item={}),),
    )


def _redacted(item: dict[str, Any]) -> dict[str, Any]:
    return {
        str(k): redact_text(v)[:300] if isinstance(v, str) else v
        for k, v in item.items()
        if k != "content"
    }


__all__ = ["NEWS_MESSAGE_TYPE", "AlpacaNewsStream"]
