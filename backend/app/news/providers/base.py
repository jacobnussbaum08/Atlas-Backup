"""The news-provider contract (Phase 3C).

Every news adapter implements :class:`NewsProvider`; everything outside
``app.news.providers.<name>`` depends only on this module and ``app.news.schemas``.

Rules every adapter follows (as the market-data contract, ``data_engine.providers.base``):

* Never fabricate articles. Empty or partial responses are returned as such; fields the
  provider leaves empty stay ``None``.
* Sanitize every text field (``app.news.text``) and keep timestamps as the provider gave
  them, converted to UTC. The time Atlas received an observation is separate
  (``received_at``) and is never substituted for the publication time.
* Never include credential values in exceptions, logs, ``repr`` or status output.
* Read-only: news adapters call news endpoints only; they never touch brokerage, order,
  account or position endpoints.
* Retain article bodies only when the configuration permits it (``store_content``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import ClassVar, Protocol

from app.data_engine.enums import StreamAuthState, StreamSubscriptionState
from app.data_engine.schemas import ConnectionState, CredentialStatus
from app.news.schemas import (
    NewsAuthenticationResult,
    NewsPage,
    NewsProviderCapabilities,
    NewsQuery,
    NewsStreamBatch,
)


class NewsStream(Protocol):
    """One live connection. Not reusable after ``close``: the worker creates a new stream
    for every reconnect and restores the subscription on it."""

    @property
    def url(self) -> str: ...

    @property
    def state(self) -> ConnectionState: ...

    @property
    def auth_state(self) -> StreamAuthState: ...

    @property
    def subscription_state(self) -> StreamSubscriptionState: ...

    @property
    def confirmed_symbols(self) -> frozenset[str]: ...

    async def connect(self) -> None:
        """Open, then authenticate; raises ``StreamError`` subclasses on failure."""

    async def subscribe(self, symbols: frozenset[str]) -> frozenset[str]:
        """Subscribe (``{"*"}`` = all news); returns the provider-confirmed set."""

    def events(self) -> AsyncIterator[NewsStreamBatch]:
        """Decoded frames until the connection closes."""

    async def close(self) -> None: ...


class NewsProvider(ABC):
    provider_id: ClassVar[str]
    display_name: ClassVar[str]

    @abstractmethod
    def capabilities(self) -> NewsProviderCapabilities: ...

    @abstractmethod
    def credential_status(self) -> CredentialStatus: ...

    @abstractmethod
    def normalize_symbol(self, symbol: str) -> str:
        """Provider form of a symbol; raises ``InvalidProviderSymbolError``."""

    @abstractmethod
    async def check_authentication(self) -> NewsAuthenticationResult: ...

    @abstractmethod
    def fetch_news(self, query: NewsQuery) -> AsyncIterator[NewsPage]:
        """Yield every page of ``query`` until the provider reports no further page.

        Raises ``ProviderPaginationError`` if pagination does not terminate correctly.
        """

    @abstractmethod
    def create_stream(self) -> NewsStream:
        """A new, unconnected live stream. Raises if streaming is unavailable."""

    async def aclose(self) -> None:  # noqa: B027  # optional hook
        """Release shared resources (connection pools)."""


__all__ = ["NewsProvider", "NewsStream"]
