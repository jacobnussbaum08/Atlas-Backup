"""Alpaca asset directory adapter: normalization, the request fence, failure handling.

Uses the TEST FIXTURE directory (synthetic records in Alpaca's documented shape).
"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.asset_discovery.enums import InstrumentType, ProviderAssetStatus
from app.data_engine.errors import (
    ProviderAuthenticationError,
    ProviderResponseError,
    ProviderUnavailableError,
    UnsupportedCapabilityError,
)
from app.data_engine.providers.alpaca.reference import (
    ALLOWED_PATH,
    PAPER_TRADING_API_HOST,
    REFERENCE_CAPABILITIES,
    ReferencePathNotAllowedError,
    check_reference_request,
    normalize_asset,
)
from app.data_engine.providers.base import MalformedRecord
from app.data_engine.reference import (
    InstrumentDirectoryRequest,
    ReferenceInstrument,
    canonical_json,
)
from app.database.models import AssetClass
from app.tests.fixtures.alpaca_assets import (
    FakeAssetDirectory,
    asset_record,
    directory_provider,
    fixture_id,
)

BACKEND = Path(__file__).resolve().parents[2]
EQUITY = InstrumentDirectoryRequest(asset_class=AssetClass.EQUITY)


def _ok(item: dict[str, Any]) -> ReferenceInstrument:
    result = normalize_asset(item)
    assert isinstance(result, ReferenceInstrument), result
    return result


def _bad(item: object) -> MalformedRecord:
    result = normalize_asset(item)
    assert isinstance(result, MalformedRecord), result
    return result


# ------------------------------------------------------------------ normalization
def test_valid_record_is_normalized_exactly():
    item = asset_record("ABC", exchange="ARCA", attributes=("has_options", "fractional_eh_enabled"))
    item["margin_requirement_long"] = "37.5"
    inst = _ok(item)
    assert inst.provider_asset_id == fixture_id("ABC")
    assert inst.symbol == inst.provider_symbol == "ABC"
    assert inst.asset_class is AssetClass.EQUITY
    assert inst.instrument_type is InstrumentType.UNCLASSIFIED  # never guessed from the name
    assert inst.provider_exchange == "ARCA"
    assert inst.status is ProviderAssetStatus.ACTIVE
    assert inst.margin_requirement_long == Decimal("37.5")
    assert inst.attributes == ("fractional_eh_enabled", "has_options")
    assert inst.quote_currency == "USD"
    assert json.loads(inst.raw_json) == item


def test_missing_optional_fields_stay_unknown_not_false():
    item = asset_record("ABC")
    for name in ("shortable", "easy_to_borrow", "borrow_status", "marginable"):
        del item[name]
    inst = _ok(item)
    assert inst.shortable is None
    assert inst.easy_to_borrow is None
    assert inst.borrow_status is None
    assert inst.marginable is None


def test_raw_json_keeps_decimal_text_exactly():
    text = '{"a": 0.1000000000000000055511151231257827, "b": [1, 2.50]}'
    assert canonical_json(json.loads(text, parse_float=Decimal)) == (
        '{"a":0.1000000000000000055511151231257827,"b":[1,2.50]}'
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"id": "not-a-uuid"}, "id"),
        ({"symbol": " ABC"}, "symbol"),
        ({"symbol": ""}, "symbol"),
        ({"class": "crypto"}, "class"),
        ({"status": "halted"}, "status"),
        ({"tradable": "yes"}, "tradable"),
        ({"margin_requirement_long": "thirty"}, "margin_requirement_long"),
        ({"margin_requirement_short": "-5"}, "margin_requirement_short"),
        ({"attributes": "has_options"}, "attributes"),
        ({"exchange": 7}, "exchange"),
    ],
)
def test_invalid_metadata_is_reported_not_repaired(change, reason):
    item = asset_record("ABC") | change
    bad = _bad(item)
    assert reason in bad.reason


def test_non_object_and_non_finite_records_are_reported():
    assert _bad("ABC").reason == "asset is not an object"
    item = asset_record("ABC")
    item["margin_requirement_long"] = float("nan")
    assert "finite" in _bad(item).reason


def test_delisted_suffix_is_evidence_and_is_stripped_from_the_symbol():
    inst = _ok(asset_record("OLDCO_DELISTED", status="inactive", tradable=False))
    assert inst.symbol == "OLDCO"
    assert inst.provider_symbol == "OLDCO_DELISTED"
    assert inst.delisting_evidence is not None
    assert "_DELISTED" in _bad(asset_record("NEWCO_DELISTED")).reason  # active + delisted


# ------------------------------------------------------------------ the request fence
@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "https://paper-api.alpaca.markets/v2/assets"),
        ("GET", "https://paper-api.alpaca.markets/v2/orders"),
        ("GET", "https://paper-api.alpaca.markets/v2/account"),
        ("GET", "https://paper-api.alpaca.markets/v2/positions"),
        ("DELETE", "https://paper-api.alpaca.markets/v2/assets/x"),
        ("GET", "https://api.alpaca.markets/v2/assets"),  # live host
        ("GET", "http://paper-api.alpaca.markets/v2/assets"),
        ("GET", "https://paper-api.alpaca.markets/v2/assets/a/b"),
    ],
)
def test_fence_refuses_everything_but_asset_reads_on_the_paper_host(method, url):
    with pytest.raises(ReferencePathNotAllowedError):
        check_reference_request(method, httpx.URL(url))


def test_fence_allows_the_directory_and_single_lookups():
    check_reference_request("GET", httpx.URL(f"https://{PAPER_TRADING_API_HOST}/v2/assets"))
    check_reference_request("GET", httpx.URL(f"https://{PAPER_TRADING_API_HOST}/v2/assets/AAPL"))
    assert ALLOWED_PATH.pattern.startswith("^/v2/assets")


def test_no_code_outside_the_reference_module_names_a_trading_host():
    """The live Trading API host appears nowhere; the paper host only in reference.py."""
    offenders = []
    for path in (BACKEND / "app").rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?<![.\w-])api\.alpaca\.markets", text):
            offenders.append(str(path))
        if "paper-api.alpaca.markets" in text and path.name != "reference.py":
            offenders.append(str(path))
    assert offenders == []


# ------------------------------------------------------------------ listing and lookups
def test_listing_sends_documented_parameters_and_reports_malformed_records():
    directory = FakeAssetDirectory().set(
        asset_record("AAA"),
        asset_record("BBB", status="inactive", tradable=False),
        asset_record("CCC") | {"tradable": "maybe"},
    )
    provider = directory_provider(directory)
    listing = asyncio.run(provider.list_instruments(EQUITY))
    assert [i.symbol for i in listing.instruments] == ["AAA", "BBB"]
    assert len(listing.malformed) == 1
    assert listing.record_count == 3
    assert directory.requests[0].url.params["asset_class"] == "us_equity"
    assert "status" not in directory.requests[0].url.params
    active = asyncio.run(
        provider.list_instruments(
            InstrumentDirectoryRequest(
                asset_class=AssetClass.EQUITY, status=ProviderAssetStatus.ACTIVE
            )
        )
    )
    assert [i.symbol for i in active.instruments] == ["AAA"]
    assert directory.requests[1].url.params["status"] == "active"


def test_duplicate_ids_in_one_response_are_malformed():
    record = asset_record("AAA")
    directory = FakeAssetDirectory().set(record, record | {"symbol": "AAB"})
    listing = asyncio.run(directory_provider(directory).list_instruments(EQUITY))
    assert len(listing.instruments) == 1
    assert listing.malformed[0].reason == "duplicate asset id in one response"


def test_non_array_or_truncated_body_fails_the_listing():
    directory = FakeAssetDirectory(raw_body='{"message": "not a list"}')
    with pytest.raises(ProviderResponseError):
        asyncio.run(directory_provider(directory).list_instruments(EQUITY))
    directory = FakeAssetDirectory(raw_body='[{"id": "')
    with pytest.raises(ProviderResponseError):
        asyncio.run(directory_provider(directory).list_instruments(EQUITY))


def test_outage_is_retried_then_reported():
    directory = FakeAssetDirectory(fail_with=[503, 503, 503]).set(asset_record("AAA"))
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(directory_provider(directory, max_retries=2).list_instruments(EQUITY))
    assert len(directory.requests) == 3

    directory = FakeAssetDirectory(fail_with=[500]).set(asset_record("AAA"))
    listing = asyncio.run(directory_provider(directory).list_instruments(EQUITY))
    assert [i.symbol for i in listing.instruments] == ["AAA"]


def test_rejected_credentials_are_permanent():
    directory = FakeAssetDirectory(fail_with=[401])
    with pytest.raises(ProviderAuthenticationError):
        asyncio.run(directory_provider(directory).list_instruments(EQUITY))
    assert len(directory.requests) == 1


def test_lookup_by_id_and_not_found():
    directory = FakeAssetDirectory().set(asset_record("AAA"))
    provider = directory_provider(directory)
    found = asyncio.run(provider.get_instrument(fixture_id("AAA")))
    assert found is not None
    assert found.symbol == "AAA"
    assert asyncio.run(provider.get_instrument(fixture_id("missing"))) is None


def test_unsupported_asset_class_is_a_capability_error():
    provider = directory_provider(FakeAssetDirectory())
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(
            provider.list_instruments(InstrumentDirectoryRequest(asset_class=AssetClass.CRYPTO))
        )
    assert REFERENCE_CAPABILITIES.asset_classes == frozenset({AssetClass.EQUITY})
    assert "instrument type" in " ".join(REFERENCE_CAPABILITIES.not_supplied)


def test_the_request_hook_blocks_even_a_direct_client_call():
    """Defence in depth: the httpx hook refuses a path that bypasses the client method."""
    directory = FakeAssetDirectory()
    provider = directory_provider(directory)
    client = provider._reference()._client()

    async def call() -> None:
        async with client:
            await client.post("/v2/orders", json={})

    with pytest.raises(ReferencePathNotAllowedError):
        asyncio.run(call())
    assert directory.requests == []
