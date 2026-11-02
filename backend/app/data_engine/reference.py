"""Standardized instrument reference data (Phase 3A).

Provider adapters translate their asset directories into these records; the discovery
engine (``app.asset_discovery``) consumes only these and never sees a provider payload's
structure. ``raw_json`` carries the provider's record verbatim (canonical key order,
numbers exactly as sent) so that the original stays available for audit.

Missing provider fields stay ``None``. A boolean the provider did not report is not
``False``: "not reported" and "not shortable" mean different things.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.asset_discovery.enums import InstrumentType, ProviderAssetStatus
from app.data_engine.providers.base import MalformedRecord
from app.data_engine.schemas import UtcDatetime
from app.database.models.asset import AssetClass


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ``Decimal`` written exactly."""
    if isinstance(value, dict):
        items = (f"{json.dumps(str(k))}:{canonical_json(v)}" for k, v in sorted(value.items()))
        return "{" + ",".join(items) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, Decimal | float) and not math.isfinite(value):
        raise ValueError("non-finite number in provider payload")
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReferenceDataCapabilities(BaseModel):
    """What an adapter's instrument directory can deliver (independent of the account)."""

    model_config = ConfigDict(frozen=True)

    provider: str
    # Atlas asset classes this adapter can discover (and also serve market data for).
    asset_classes: frozenset[AssetClass]
    # Provider exchange codes the directory documents.
    exchanges: tuple[str, ...]
    # One request returns the complete directory of an asset class (no pagination).
    full_listing_in_one_request: bool
    status_filter: bool
    single_lookup: bool
    # Facts the provider does not supply, so Atlas must not pretend to know them.
    not_supplied: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    documentation: tuple[str, ...] = ()
    verified_on: date


class InstrumentDirectoryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_class: AssetClass
    # None lists every status (active and inactive).
    status: ProviderAssetStatus | None = None


class ReferenceInstrument(BaseModel):
    """One instrument as a provider's directory describes it, normalized."""

    model_config = ConfigDict(frozen=True)

    provider: str
    # The provider's own permanent identifier for the instrument (e.g. Alpaca's asset UUID).
    provider_asset_id: str = Field(min_length=1, max_length=64)
    # The symbol as the provider writes it in this record (may carry provider decorations).
    provider_symbol: str = Field(min_length=1, max_length=128)
    # The instrument's trading symbol, without provider decorations.
    symbol: str = Field(min_length=1, max_length=64)
    name: str | None
    asset_class: AssetClass
    provider_asset_class: str
    instrument_type: InstrumentType = InstrumentType.UNCLASSIFIED
    provider_exchange: str | None
    quote_currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: ProviderAssetStatus
    tradable: bool | None = None
    marginable: bool | None = None
    shortable: bool | None = None
    easy_to_borrow: bool | None = None
    borrow_status: str | None = None
    fractionable: bool | None = None
    margin_requirement_long: Decimal | None = None
    margin_requirement_short: Decimal | None = None
    min_order_size: Decimal | None = None
    min_trade_increment: Decimal | None = None
    price_increment: Decimal | None = None
    attributes: tuple[str, ...] = ()
    # The provider signalled that the instrument was delisted (and how), if it did.
    delisting_evidence: str | None = None
    raw_json: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.symbol != self.symbol.upper() or self.symbol != self.symbol.strip():
            raise ValueError("symbol must be upper case without surrounding whitespace")
        if sha256_text(self.raw_json) != self.content_sha256:
            raise ValueError("content_sha256 does not match raw_json")
        if self.delisting_evidence is not None and self.status is ProviderAssetStatus.ACTIVE:
            raise ValueError("an active instrument cannot carry delisting evidence")
        return self


class InstrumentDirectory(BaseModel):
    """A complete directory response: every record is either normalized or malformed."""

    model_config = ConfigDict(frozen=True)

    provider: str
    request: InstrumentDirectoryRequest
    instruments: tuple[ReferenceInstrument, ...]
    malformed: tuple[MalformedRecord, ...] = ()
    requested_at: UtcDatetime
    received_at: UtcDatetime
    response_bytes: int
    response_sha256: str
    request_params: dict[str, str] = Field(default_factory=dict)

    @property
    def record_count(self) -> int:
        return len(self.instruments) + len(self.malformed)


__all__ = [
    "InstrumentDirectory",
    "InstrumentDirectoryRequest",
    "ReferenceDataCapabilities",
    "ReferenceInstrument",
    "canonical_json",
    "sha256_text",
]
