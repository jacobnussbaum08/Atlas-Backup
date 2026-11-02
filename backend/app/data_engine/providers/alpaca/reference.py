"""Alpaca instrument directory (Phase 3A): ``GET /v2/assets`` and ``GET /v2/assets/{id}``.

Alpaca publishes its asset directory only on the Trading API host. Decision D-050 allows
exactly this, and nothing more:

* the host is the **paper** Trading API, a constant (``paper-api.alpaca.markets``); the
  live host is never contacted and there is no setting that could point elsewhere;
* only ``GET`` is possible, and only to the two asset paths below. The check runs before
  every request *and* again in an httpx request hook, so a future code change cannot
  route an order, account or position request through this client;
* responses are parsed with exact decimals; malformed records are reported, never
  repaired or dropped silently.

Documented behaviour used here (https://docs.alpaca.markets/us/reference/get-v2-assets-1,
checked 2026-11-01): ``status`` filter ``active``/``inactive`` (all when omitted),
``asset_class`` default ``us_equity``, the whole directory in one array (no pagination),
fields ``id, class, exchange, symbol, name, status, tradable, marginable, shortable,
easy_to_borrow, fractionable, attributes`` plus optional ``borrow_status``,
``margin_requirement_long/short`` and crypto-only increments.

Observed on 2026-11-01 with this account (not documented, treated as provider facts):
REDACTED_FOR_GITHUB
by more than one instrument (typically one inactive and one active, same exchange); some
inactive symbols carry a ``_DELISTED`` suffix.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.asset_discovery.enums import ProviderAssetStatus
from app.core.clocks import Clock
from app.data_engine.errors import (
    ProviderRequestError,
    ProviderResponseError,
    UnsupportedCapabilityError,
)
from app.data_engine.providers.alpaca.http import AlpacaHttp
from app.data_engine.providers.base import MalformedRecord
from app.data_engine.reference import (
    InstrumentDirectory,
    InstrumentDirectoryRequest,
    ReferenceDataCapabilities,
    ReferenceInstrument,
    canonical_json,
    sha256_text,
)
from app.database.models.asset import AssetClass

PAPER_TRADING_API_HOST = "paper-api.alpaca.markets"
PAPER_TRADING_API_BASE_URL = f"https://{PAPER_TRADING_API_HOST}"
ASSETS_PATH = "/v2/assets"
# The only requests this client can make: the directory, and one asset by UUID or symbol.
ALLOWED_PATH = re.compile(r"^/v2/assets(/[A-Za-z0-9._-]{1,64})?$")
# The full directory is one ~15 MB response.
DIRECTORY_TIMEOUT_SECONDS = 120.0

DOCUMENTATION = (
    "https://docs.alpaca.markets/us/reference/get-v2-assets-1",
    "https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id",
    "https://docs.alpaca.markets/us/docs/working-with-assets",
)

# Atlas asset class -> Alpaca asset_class. Only classes the market-data adapter can also
# serve are discoverable: discovering crypto without crypto market data would be useless.
_PROVIDER_CLASSES = {AssetClass.EQUITY: "us_equity"}
_STATUS = {"active": ProviderAssetStatus.ACTIVE, "inactive": ProviderAssetStatus.INACTIVE}
_DELISTED_SUFFIX = "_DELISTED"
_BOOLEAN_FIELDS = ("tradable", "marginable", "shortable", "easy_to_borrow", "fractionable")
_DECIMAL_FIELDS = (
    "margin_requirement_long",
    "margin_requirement_short",
    "min_order_size",
    "min_trade_increment",
    "price_increment",
)

REFERENCE_CAPABILITIES = ReferenceDataCapabilities(
    provider="alpaca",
    asset_classes=frozenset(_PROVIDER_CLASSES),
    exchanges=("AMEX", "ARCA", "BATS", "NYSE", "NASDAQ", "NYSEARCA", "OTC", "TXSE"),
    full_listing_in_one_request=True,
    status_filter=True,
    single_lookup=True,
    not_supplied=(
        "instrument type (ETFs, preferreds, warrants, units and rights are all us_equity)",
        "issuer country",
        "listing, delisting and symbol-change dates",
        "tick size and lot size for US equities (price_increment is crypto-only)",
        "trading halts",
    ),
    notes=(
        "Read from Alpaca's paper Trading API host (decision D-050): GET only, asset paths "
        "only. US equities are quoted in USD.",
        "tradable=false marks data-only assets or instruments Alpaca does not trade.",
        "Shortability and borrow status are reference data at the time of retrieval; they "
        "must be revalidated before any short is proposed.",
    ),
    documentation=DOCUMENTATION,
    verified_on=date(2026, 11, 1),
)


class ReferencePathNotAllowedError(ProviderRequestError):
    code = "reference_path_not_allowed"


def check_reference_request(method: str, url: httpx.URL) -> None:
    """Refuse anything but GET to the paper host's asset paths."""
    if method != "GET":
        raise ReferencePathNotAllowedError(f"reference client refuses {method} requests")
    if url.scheme != "https" or url.host != PAPER_TRADING_API_HOST:
        raise ReferencePathNotAllowedError("reference client only contacts the paper asset API")
    if not ALLOWED_PATH.match(url.path):
        raise ReferencePathNotAllowedError(f"reference client refuses path {url.path}")


async def _guard_request(request: httpx.Request) -> None:
    check_reference_request(request.method, request.url)


def normalize_asset(item: object) -> ReferenceInstrument | MalformedRecord:
    """One Alpaca asset object -> a standardized instrument, or the reason it is invalid."""
    if not isinstance(item, dict):
        return MalformedRecord(provider_symbol=None, reason="asset is not an object", item={})
    symbol_value = item.get("symbol")
    provider_symbol = symbol_value if isinstance(symbol_value, str) else None

    def bad(reason: str) -> MalformedRecord:
        return MalformedRecord(provider_symbol=provider_symbol, reason=reason, item=report(item))

    try:
        raw = canonical_json(item)
    except ValueError:
        return bad("record contains a non-finite number")

    asset_id = item.get("id")
    try:
        uuid.UUID(str(asset_id))
    except ValueError:
        return bad("missing or invalid 'id'")
    if not isinstance(asset_id, str):
        return bad("'id' is not a string")
    if (
        provider_symbol is None
        or not provider_symbol
        or provider_symbol != provider_symbol.strip()
        or any(ch.isspace() for ch in provider_symbol)
        or len(provider_symbol) > 128
    ):
        return bad("missing or invalid 'symbol'")
    provider_class = item.get("class")
    if provider_class != "us_equity":
        return bad(f"unsupported or missing 'class' {provider_class!r}")
    status = _STATUS.get(str(item.get("status")))
    if status is None:
        return bad(f"unknown 'status' {item.get('status')!r}")
    exchange = item.get("exchange")
    if exchange is not None and (not isinstance(exchange, str) or not exchange):
        return bad("invalid 'exchange'")
    name = item.get("name")
    if name is not None and not isinstance(name, str):
        return bad("invalid 'name'")

    flags: dict[str, bool | None] = {}
    for field in _BOOLEAN_FIELDS:
        value = item.get(field)
        if value is not None and not isinstance(value, bool):
            return bad(f"'{field}' is not a boolean")
        flags[field] = value
    decimals: dict[str, Decimal | None] = {}
    for field in _DECIMAL_FIELDS:
        value = item.get(field)
        if value is None:
            decimals[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
            return bad(f"'{field}' is not a decimal")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return bad(f"'{field}' is not a decimal")
        if not number.is_finite() or number < 0:
            return bad(f"'{field}' is not a finite, non-negative decimal")
        decimals[field] = number
    attributes = item.get("attributes", [])
    if attributes is None:
        attributes = []
    if not isinstance(attributes, list) or not all(isinstance(a, str) for a in attributes):
        return bad("'attributes' is not a list of strings")
    borrow_status = item.get("borrow_status")
    if borrow_status is not None and not isinstance(borrow_status, str):
        return bad("invalid 'borrow_status'")

    symbol = provider_symbol.upper()
    delisting = None
    if symbol.endswith(_DELISTED_SUFFIX) and len(symbol) > len(_DELISTED_SUFFIX):
        if status is ProviderAssetStatus.ACTIVE:
            return bad("active asset with a _DELISTED symbol")
        symbol = symbol.removesuffix(_DELISTED_SUFFIX)
        delisting = (
            "Alpaca renamed the symbol with a _DELISTED suffix (an observed Alpaca "
            "convention, not documented)"
        )
    if len(symbol) > 64:
        return bad("symbol longer than 64 characters")

    try:
        return ReferenceInstrument(
            provider="alpaca",
            provider_asset_id=asset_id,
            provider_symbol=provider_symbol,
            symbol=symbol,
            name=name or None,
            asset_class=AssetClass.EQUITY,
            provider_asset_class=provider_class,
            provider_exchange=exchange,
            quote_currency="USD",
            status=status,
            borrow_status=borrow_status,
            attributes=tuple(sorted(set(attributes))),
            delisting_evidence=delisting,
            raw_json=raw,
            content_sha256=sha256_text(raw),
            **flags,
            **decimals,
        )
    except ValueError as exc:
        return bad(f"invalid record: {exc}")


def report(item: dict[str, Any]) -> dict[str, Any]:
    """A record as JSON-safe data for error reports (numbers kept as exact text)."""
    try:
        value = json.loads(canonical_json(item), parse_float=str, parse_constant=str)
    except ValueError:
        return {"note": "record could not be serialized"}
    return value if isinstance(value, dict) else {"value": value}


class AlpacaReferenceClient:
    """Reads Alpaca's asset directory. Holds no state besides the request pacing."""

    def __init__(
        self,
        *,
        headers: dict[str, str],
        http: AlpacaHttp,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._headers = headers
        self._http = http
        self._clock = clock
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=PAPER_TRADING_API_BASE_URL,
            headers={**self._headers, "Accept": "application/json"},
            timeout=DIRECTORY_TIMEOUT_SECONDS,
            transport=self._transport,
            follow_redirects=False,
            event_hooks={"request": [_guard_request]},
        )

    async def _get(
        self, path: str, params: dict[str, str], accept: frozenset[int] = frozenset({200})
    ) -> httpx.Response:
        check_reference_request("GET", httpx.URL(PAPER_TRADING_API_BASE_URL + path))
        async with self._client() as client:
            return await self._http.get(client, path, params, accept=accept)

    async def list_instruments(self, request: InstrumentDirectoryRequest) -> InstrumentDirectory:
        provider_class = _PROVIDER_CLASSES.get(request.asset_class)
        if provider_class is None:
            raise UnsupportedCapabilityError(
                f"Alpaca asset discovery does not support {request.asset_class.value}"
            )
        params = {"asset_class": provider_class}
        if request.status is not None:
            params["status"] = request.status.value.lower()
        requested_at = self._clock.now()
        response = await self._get(ASSETS_PATH, params)
        received_at = self._clock.now()
        body = _decode(response)
        if not isinstance(body, list):
            raise ProviderResponseError("Alpaca asset directory is not a JSON array")
        instruments: list[ReferenceInstrument] = []
        malformed: list[MalformedRecord] = []
        seen: set[str] = set()
        for item in body:
            result = normalize_asset(item)
            if isinstance(result, MalformedRecord):
                malformed.append(result)
            elif result.provider_asset_id in seen:
                malformed.append(
                    MalformedRecord(
                        provider_symbol=result.provider_symbol,
                        reason="duplicate asset id in one response",
                        item=json.loads(result.raw_json, parse_float=str),
                    )
                )
            else:
                seen.add(result.provider_asset_id)
                instruments.append(result)
        return InstrumentDirectory(
            provider="alpaca",
            request=request,
            instruments=tuple(instruments),
            malformed=tuple(malformed),
            requested_at=requested_at,
            received_at=received_at,
            response_bytes=len(response.content),
            response_sha256=sha256_text(response.text),
            request_params=params,
        )

    async def get_instrument(self, provider_asset_id: str) -> ReferenceInstrument | None:
        try:
            uuid.UUID(provider_asset_id)
        except ValueError:
            raise ProviderRequestError("Alpaca asset lookups use the asset UUID") from None
        response = await self._get(
            f"{ASSETS_PATH}/{provider_asset_id}", {}, accept=frozenset({200, 404})
        )
        if response.status_code == 404:
            return None
        result = normalize_asset(_decode(response))
        if isinstance(result, MalformedRecord):
            raise ProviderResponseError(f"Alpaca returned an invalid asset: {result.reason}")
        if result.provider_asset_id != provider_asset_id:
            raise ProviderResponseError("Alpaca returned a different asset than requested")
        return result


def _decode(response: httpx.Response) -> Any:
    try:
        return json.loads(response.text, parse_float=Decimal)
    except ValueError:
        raise ProviderResponseError("Alpaca asset response is not valid JSON") from None
