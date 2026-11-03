"""News-provider registry: the only place that maps a provider ID to its adapter."""

from __future__ import annotations

from collections.abc import Callable

from app.core.clocks import Clock, SystemClock
from app.data_engine.errors import UnknownProviderError
from app.news.config import NewsSettings
from app.news.providers.alpaca.adapter import AlpacaNewsProvider
from app.news.providers.base import NewsProvider

NewsProviderFactory = Callable[[Clock], NewsProvider]

_FACTORIES: dict[str, NewsProviderFactory] = {
    AlpacaNewsProvider.provider_id: AlpacaNewsProvider.from_environment,
}


def available_news_providers() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def create_news_provider(settings: NewsSettings, clock: Clock | None = None) -> NewsProvider:
    factory = _FACTORIES.get(settings.provider)
    if factory is None:
        raise UnknownProviderError(
            f"unknown news provider '{settings.provider}' "
            f"(available: {', '.join(available_news_providers())})"
        )
    return factory(clock or SystemClock())


__all__ = ["available_news_providers", "create_news_provider"]
