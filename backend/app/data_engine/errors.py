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


class ProviderEntitlementError(UnsupportedCapabilityError):
    """The provider refused the request for the current subscription (e.g. HTTP 403)."""

    code = "provider_entitlement_denied"


class ProviderRequestError(MarketDataError):
    """The provider rejected the request as invalid (e.g. HTTP 400). Not retryable."""

    code = "provider_request_rejected"
    http_status = 502


class ProviderResponseError(MarketDataError):
    """The provider's response did not match the documented format. Not retried."""

    code = "provider_response_invalid"
    http_status = 502


class ProviderPaginationError(ProviderResponseError):
    """Pagination did not terminate correctly (repeated token or page limit exceeded)."""

    code = "provider_pagination_failed"


class ProviderOperationNotImplementedError(MarketDataError):
    """The operation is part of a later Phase 2 deliverable and not built yet."""

    code = "provider_operation_not_implemented"
    http_status = 501


class StreamError(MarketDataError):
    """A live-stream failure. ``permanent`` failures must not be retried automatically."""

    code = "stream_error"
    http_status = 502
    permanent: bool = False

    def __init__(self, message: str, *, provider_code: int | None = None) -> None:
        super().__init__(message)
        self.provider_code = provider_code


class StreamConnectionError(StreamError):
    """Transport failure: connect timeout, network error, unexpected close. Retryable."""

    code = "stream_connection_failed"


class StreamProtocolError(StreamError):
    """The provider answered out of protocol, or reported a recoverable error. Retryable."""

    code = "stream_protocol_error"


class StreamAuthenticationError(StreamError):
    """Credentials rejected by the stream. Permanent until the configuration changes."""

    code = "stream_authentication_failed"
    permanent = True


class StreamSubscriptionError(StreamError):
    """The provider refused the subscription (entitlement, symbol limit, invalid channel)."""

    code = "stream_subscription_failed"
    permanent = True
