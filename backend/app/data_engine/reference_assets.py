"""Load instrument reference data and register assets + provider mappings idempotently.

The reference file (``config/market_data/reference_assets.toml`` by default) is the
operator-maintained source of instrument identity. Since Phase 3A asset discovery catalogs
instruments from the provider directory; it adopts assets registered here (same current
provider symbol) instead of duplicating them.
Registration never re-maps silently: if an asset already has a different current
symbol at a provider, registration fails and the operator decides.
"""

from __future__ import annotations

import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config.settings import describe_validation_error
from app.data_engine.errors import MarketDataError
from app.data_engine.schemas import UtcDatetime
from app.database.models import ActorType, Asset, AssetClass, AssetStatus
from app.database.repositories import AssetMappingRepository, AuditLogRepository

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REFERENCE_FILE = REPO_ROOT / "config" / "market_data" / "reference_assets.toml"


class ReferenceDataError(MarketDataError):
    code = "reference_data_error"
    http_status = 422


class ProviderListing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    exchange: str | None = None
    valid_from: UtcDatetime


class ReferenceAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9./-]{0,31}$")
    name: str | None = None
    asset_class: AssetClass
    exchange_code: str | None = Field(default=None, pattern=r"^[A-Z0-9]{4}$")
    quote_currency: str = Field(pattern=r"^[A-Z]{3}$")
    providers: tuple[ProviderListing, ...] = ()

    @field_validator("providers")
    @classmethod
    def _one_listing_per_provider(
        cls, value: tuple[ProviderListing, ...]
    ) -> tuple[ProviderListing, ...]:
        providers = [listing.provider for listing in value]
        if len(providers) != len(set(providers)):
            raise ValueError("at most one current listing per provider")
        return value

    def listing(self, provider: str) -> ProviderListing | None:
        return next((p for p in self.providers if p.provider == provider), None)


def load_reference_assets(path: Path = DEFAULT_REFERENCE_FILE) -> dict[str, ReferenceAsset]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReferenceDataError(f"reference data file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ReferenceDataError(f"reference data file is not valid TOML: {exc}") from None
    assets: dict[str, ReferenceAsset] = {}
    for index, entry in enumerate(raw.get("asset", [])):
        try:
            asset = ReferenceAsset.model_validate(entry)
        except ValidationError as exc:
            raise ReferenceDataError(
                describe_validation_error(exc, f"reference asset #{index + 1}")
            ) from None
        if asset.symbol in assets:
            raise ReferenceDataError(f"duplicate reference asset '{asset.symbol}'")
        assets[asset.symbol] = asset
    return assets


@dataclass(frozen=True)
class RegisteredAsset:
    symbol: str
    asset_id: uuid.UUID
    provider: str
    provider_symbol: str
    asset_created: bool
    mapping_created: bool


def register_assets(
    session: Session,
    symbols: list[str] | tuple[str, ...],
    provider: str,
    reference: dict[str, ReferenceAsset],
) -> list[RegisteredAsset]:
    """Ensure each symbol has an ``assets`` row and a current mapping at ``provider``."""
    mappings = AssetMappingRepository(session)
    audit = AuditLogRepository(session)
    results: list[RegisteredAsset] = []
    for symbol in symbols:
        ref = reference.get(symbol)
        if ref is None:
            raise ReferenceDataError(f"'{symbol}' is not in the reference data file")
        listing = ref.listing(provider)
        if listing is None:
            raise ReferenceDataError(f"'{symbol}' has no '{provider}' listing in reference data")

        # Only active assets: providers reuse symbols, so an inactive instrument with the
        # same symbol is a different asset (Phase 3A).
        asset = session.scalars(
            select(Asset).where(
                Asset.symbol == ref.symbol,
                Asset.asset_class == ref.asset_class,
                Asset.exchange_code.is_not_distinct_from(ref.exchange_code),
                Asset.status == AssetStatus.ACTIVE,
            )
        ).one_or_none()
        asset_created = asset is None
        if asset is None:
            asset = Asset(
                symbol=ref.symbol,
                name=ref.name,
                asset_class=ref.asset_class,
                exchange_code=ref.exchange_code,
                quote_currency=ref.quote_currency,
            )
            session.add(asset)
            session.flush()
            audit.append(
                actor_type=ActorType.SYSTEM,
                actor_id="asset_registry",
                action="asset.registered",
                entity_type="asset",
                entity_id=str(asset.id),
                reason="registered from reference data",
                new_state=ref.model_dump(mode="json", exclude={"providers"}),
            )

        current = mappings.current_for_asset(asset.id, provider)
        mapping_created = False
        if current is None:
            try:
                with session.begin_nested():
                    mappings.register(
                        asset_id=asset.id,
                        provider=provider,
                        provider_symbol=listing.symbol,
                        valid_from=listing.valid_from,
                        provider_exchange=listing.exchange,
                    )
            except IntegrityError:
                raise ReferenceDataError(
                    f"'{listing.symbol}' at {provider} overlaps an existing mapping; "
                    "resolve it before registering"
                ) from None
            mapping_created = True
            audit.append(
                actor_type=ActorType.SYSTEM,
                actor_id="asset_registry",
                action="asset_mapping.registered",
                entity_type="asset",
                entity_id=str(asset.id),
                reason="registered from reference data",
                new_state=listing.model_dump(mode="json"),
            )
        elif current.provider_symbol != listing.symbol:
            raise ReferenceDataError(
                f"'{symbol}' is currently mapped to '{current.provider_symbol}' at {provider}, "
                f"but reference data says '{listing.symbol}'; Atlas does not re-map silently"
            )
        results.append(
            RegisteredAsset(
                symbol=ref.symbol,
                asset_id=asset.id,
                provider=provider,
                provider_symbol=listing.symbol,
                asset_created=asset_created,
                mapping_created=mapping_created,
            )
        )
    return results
