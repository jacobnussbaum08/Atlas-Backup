"""Provider registry: the only place that maps a provider ID to its adapter."""

from __future__ import annotations

from collections.abc import Callable

from app.core.clocks import Clock, SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.errors import UnknownProviderError
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.base import MarketDataProvider

ProviderFactory = Callable[[Clock], MarketDataProvider]

_FACTORIES: dict[str, ProviderFactory] = {
    AlpacaMarketDataProvider.provider_id: AlpacaMarketDataProvider.from_environment,
}


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def create_provider(settings: MarketDataSettings, clock: Clock | None = None) -> MarketDataProvider:
    """Build the configured provider. Provider-specific settings are loaded by the adapter."""
    factory = _FACTORIES.get(settings.market_data_provider)
    if factory is None:
        raise UnknownProviderError(
            f"unknown market-data provider '{settings.market_data_provider}' "
            f"(available: {', '.join(available_providers())})"
        )
    return factory(clock or SystemClock())
