"""Market-data error hierarchy.

Messages must never contain credential values. Provider adapters raise these
types so the rest of Atlas never handles provider-specific exceptions.
"""

from __future__ import annotations

from app.core.exceptions import AtlasError


class MarketDataError(AtlasError):
    code = "market_data_error"


class ProviderConfigurationError(MarketDataError):
    """The provider cannot be used with the current configuration."""

    code = "provider_configuration_error"


class UnknownProviderError(ProviderConfigurationError):
    code = "unknown_provider"


class CredentialsNotConfiguredError(ProviderConfigurationError):
    code = "credentials_not_configured"


class UnsupportedCapabilityError(MarketDataError):
    """A requested feed, timeframe, data type or market is not supported or not entitled."""

    code = "unsupported_capability"
    http_status = 422


class InvalidProviderSymbolError(MarketDataError):
    code = "invalid_provider_symbol"
    http_status = 422


class ProviderAuthenticationError(MarketDataError):
    """Credentials were rejected. Not retryable without a configuration change."""

    code = "provider_authentication_failed"
    http_status = 502


class ProviderRateLimitError(MarketDataError):
    code = "provider_rate_limited"
    http_status = 503


class ProviderUnavailableError(MarketDataError):
    """Transient provider or network failure; retryable with backoff."""

    code = "provider_unavailable"
    http_status = 503


class ProviderOperationNotImplementedError(MarketDataError):
    """The operation is part of a later Phase 2 deliverable and not built yet."""

    code = "provider_operation_not_implemented"
    http_status = 501
