"""LLM provider adapters (Phase 5B).

One interface, :class:`LLMProvider`: send a system prompt, one user message and an output
schema; get back the structured object, token usage and identifiers. Adapters:

* :class:`AnthropicProvider`: the Messages API (``POST /v1/messages``) with one tool whose
  ``input_schema`` is the output schema and ``tool_choice`` forcing it.
* :class:`OpenAICompatibleProvider`: ``POST {base}/chat/completions`` with
  ``response_format = json_schema`` (strict).
* :class:`FixtureProvider`: canned responses for tests, labelled TEST FIXTURE. It can never
  be configured through settings.

Adapters use ``httpx`` (already a dependency; no vendor SDK is added). They never log or
return credentials; error messages carry the status code and the provider's error type,
never request headers. Retrying, budgets, caching and storage live in the service.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from app.ai_engine.config import AISettings
from app.ai_engine.enums import ProviderKind

ANTHROPIC_VERSION = "2023-06-01"
ErrorKind = Literal[
    "TIMEOUT", "RATE_LIMITED", "UNAVAILABLE", "AUTH", "BAD_REQUEST", "INVALID_RESPONSE"
]
RETRYABLE: frozenset[str] = frozenset({"TIMEOUT", "RATE_LIMITED", "UNAVAILABLE"})


class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    system: str
    user: str
    tool_name: str
    tool_description: str
    schema: dict[str, Any]
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class ProviderResponse:
    output: dict[str, Any] | None  # the structured object, before validation
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider_request_id: str | None = None
    provider_model: str | None = None
    stop_reason: str | None = None
    latency_ms: int = 0
    note: str | None = None  # e.g. why ``output`` is missing


class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return None if value is None else max(0.0, float(value))
    except ValueError:
        return None


def _error_type(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "unparseable error body"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("type") or error.get("code") or "error")[:64]
    return "error"


def _raise_for(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    detail = f"HTTP {status} ({_error_type(response)})"
    if status == 429:
        raise ProviderError("RATE_LIMITED", detail, retry_after=_retry_after(response))
    if status in (401, 403):
        raise ProviderError("AUTH", detail)
    if status in (408, 409) or status >= 500:
        raise ProviderError("UNAVAILABLE", detail, retry_after=_retry_after(response))
    raise ProviderError("BAD_REQUEST", detail)


async def _post(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[httpx.Response, int]:
    started = time.monotonic()
    try:
        response = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise ProviderError("TIMEOUT", f"no response within {timeout:g} s") from exc
    except httpx.TransportError as exc:
        raise ProviderError("UNAVAILABLE", f"transport error ({type(exc).__name__})") from exc
    latency = int((time.monotonic() - started) * 1000)
    _raise_for(response)
    return response, latency


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderError("INVALID_RESPONSE", "the response body is not JSON") from exc
    if not isinstance(body, dict):
        raise ProviderError("INVALID_RESPONSE", "the response body is not an object")
    return body


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class AnthropicProvider:
    name = ProviderKind.ANTHROPIC.value

    def __init__(
        self, *, model: str, api_key: str, base_url: str, client: httpx.AsyncClient
    ) -> None:
        self.model = model
        self._key = api_key
        self._url = f"{base_url.rstrip('/')}/v1/messages"
        self._client = client

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        body = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "tools": [
                {
                    "name": request.tool_name,
                    "description": request.tool_description,
                    "input_schema": request.schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": request.tool_name},
        }
        headers = {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        response, latency = await _post(
            self._client, self._url, headers, body, request.timeout_seconds
        )
        data = _json(response)
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        output = None
        note = None
        for block in data.get("content") or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == request.tool_name
            ):
                candidate = block.get("input")
                output = candidate if isinstance(candidate, dict) else None
                break
        if output is None:
            note = "no tool_use block with the requested tool"
        return ProviderResponse(
            output=output,
            input_tokens=_int(usage.get("input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
            cache_write_tokens=_int(usage.get("cache_creation_input_tokens")),
            provider_request_id=str(data.get("id"))[:128] if data.get("id") else None,
            provider_model=str(data.get("model"))[:128] if data.get("model") else None,
            stop_reason=str(data.get("stop_reason"))[:64] if data.get("stop_reason") else None,
            latency_ms=latency,
            note=note,
        )


class OpenAICompatibleProvider:
    name = ProviderKind.OPENAI_COMPATIBLE.value

    def __init__(
        self, *, model: str, api_key: str, base_url: str, client: httpx.AsyncClient
    ) -> None:
        self.model = model
        self._key = api_key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._client = client

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        body = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.tool_name,
                    "schema": request.schema,
                    "strict": True,
                },
            },
        }
        headers = {"authorization": f"Bearer {self._key}", "content-type": "application/json"}
        response, latency = await _post(
            self._client, self._url, headers, body, request.timeout_seconds
        )
        data = _json(response)
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        output = None
        note = None
        choices = data.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        finish = (
            choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
        )
        if isinstance(message, dict):
            if message.get("refusal"):
                note = "the model refused"
            content = message.get("content")
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    output = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    note = "the content is not JSON"
        if output is None and note is None:
            note = "no structured content"
        details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
        return ProviderResponse(
            output=output,
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            cache_read_tokens=_int(details.get("cached_tokens"))
            if isinstance(details, dict)
            else 0,
            provider_request_id=str(data.get("id"))[:128] if data.get("id") else None,
            provider_model=str(data.get("model"))[:128] if data.get("model") else None,
            stop_reason=str(finish)[:64] if finish else None,
            latency_ms=latency,
            note=note,
        )


Responder = Callable[[ProviderRequest], Awaitable[ProviderResponse]]


class FixtureProvider:
    """TEST FIXTURE provider: answers with whatever the test's responder returns or raises.
    Its responses are not analyses of anything."""

    name = ProviderKind.FIXTURE.value

    def __init__(self, responder: Responder, *, model: str = "fixture-model") -> None:
        self.model = model
        self._responder = responder
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return await self._responder(request)


def build_provider(settings: AISettings, client: httpx.AsyncClient) -> LLMProvider | None:
    """The configured provider, or None when LLM analysis is not configured."""
    if settings.llm_provider is ProviderKind.NONE:
        return None
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    if settings.llm_provider is ProviderKind.ANTHROPIC:
        return AnthropicProvider(
            model=settings.llm_model, api_key=key, base_url=settings.base_url(), client=client
        )
    if settings.llm_provider is ProviderKind.OPENAI_COMPATIBLE:
        return OpenAICompatibleProvider(
            model=settings.llm_model, api_key=key, base_url=settings.base_url(), client=client
        )
    raise ValueError(f"provider {settings.llm_provider.value} cannot be built from settings")


__all__ = [
    "ANTHROPIC_VERSION",
    "AnthropicProvider",
    "FixtureProvider",
    "LLMProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderRequest",
    "ProviderResponse",
    "build_provider",
]
