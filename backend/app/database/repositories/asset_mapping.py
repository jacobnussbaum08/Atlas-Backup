"""Asset <-> provider symbol mappings with point-in-time resolution.

A mapping is valid over ``[valid_from, valid_to)``. Resolution always takes an
explicit timestamp, so historical data is attributed to the asset that the
symbol identified *at that time*, even after symbols are changed or reused.
Mappings are never deleted; ending one sets ``valid_to`` and ``RETIRED``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.orm import Session

from app.core.clocks import ensure_utc
from app.database.models.asset import Asset, AssetIdentifier, MappingStatus


class MappingError(ValueError):
    pass


def _valid_at(at: datetime) -> list[ColumnElement[bool]]:
    return [
        AssetIdentifier.valid_from <= at,
        or_(AssetIdentifier.valid_to.is_(None), AssetIdentifier.valid_to > at),
    ]


class AssetMappingRepository:
    """Callers own the transaction; this repository never commits."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def register(
        self,
        *,
        asset_id: uuid.UUID,
        provider: str,
        provider_symbol: str,
        valid_from: datetime,
        provider_exchange: str | None = None,
    ) -> AssetIdentifier:
        """Add a current mapping. Overlaps are rejected by database exclusion constraints."""
        if not provider_symbol or provider_symbol != provider_symbol.strip():
            raise MappingError("provider_symbol must be non-empty without surrounding whitespace")
        mapping = AssetIdentifier(
            asset_id=asset_id,
            provider=provider,
            provider_symbol=provider_symbol,
            provider_exchange=provider_exchange,
            status=MappingStatus.ACTIVE,
            valid_from=ensure_utc(valid_from),
        )
        self._session.add(mapping)
        self._session.flush()
        return mapping

    def retire(self, mapping_id: uuid.UUID, valid_to: datetime) -> AssetIdentifier:
        """End a mapping's validity. History before ``valid_to`` stays resolvable."""
        mapping = self._session.get(AssetIdentifier, mapping_id, with_for_update=True)
        if mapping is None:
            raise MappingError(f"mapping {mapping_id} does not exist")
        if mapping.valid_to is not None:
            raise MappingError(f"mapping {mapping_id} is already retired")
        valid_to = ensure_utc(valid_to)
        if valid_to <= mapping.valid_from:
            raise MappingError("valid_to must be after valid_from")
        mapping.valid_to = valid_to
        mapping.status = MappingStatus.RETIRED
        self._session.flush()
        return mapping

    def resolve(self, provider: str, provider_symbol: str, at: datetime) -> AssetIdentifier | None:
        """The mapping that ``provider_symbol`` denoted at ``at`` (any status), or ``None``."""
        stmt = select(AssetIdentifier).where(
            AssetIdentifier.provider == provider,
            AssetIdentifier.provider_symbol == provider_symbol,
            *_valid_at(ensure_utc(at)),
        )
        return self._session.scalars(stmt).one_or_none()

    def resolve_many(
        self, provider: str, provider_symbols: Iterable[str], at: datetime
    ) -> dict[str, uuid.UUID]:
        """Asset IDs for the symbols that were mapped at ``at``. Unmapped symbols are absent."""
        symbols = sorted(set(provider_symbols))
        if not symbols:
            return {}
        stmt = select(AssetIdentifier.provider_symbol, AssetIdentifier.asset_id).where(
            AssetIdentifier.provider == provider,
            AssetIdentifier.provider_symbol.in_(symbols),
            *_valid_at(ensure_utc(at)),
        )
        return {symbol: asset_id for symbol, asset_id in self._session.execute(stmt)}

    def current_for_asset(self, asset_id: uuid.UUID, provider: str) -> AssetIdentifier | None:
        stmt = select(AssetIdentifier).where(
            AssetIdentifier.asset_id == asset_id,
            AssetIdentifier.provider == provider,
            AssetIdentifier.valid_to.is_(None),
        )
        return self._session.scalars(stmt).one_or_none()

    def history(self, provider: str, provider_symbol: str) -> Sequence[AssetIdentifier]:
        stmt = (
            select(AssetIdentifier)
            .where(
                AssetIdentifier.provider == provider,
                AssetIdentifier.provider_symbol == provider_symbol,
            )
            .order_by(AssetIdentifier.valid_from)
        )
        return self._session.scalars(stmt).all()

    def current_assets(
        self,
        provider: str,
        *,
        symbols: Iterable[str] | None = None,
        provider_symbols: Iterable[str] | None = None,
    ) -> Sequence[tuple[Asset, AssetIdentifier]]:
        """Assets with a current, active mapping at ``provider``.

        With ``symbols`` (Atlas symbols) and/or ``provider_symbols``, only assets matching
        either are returned; passing only empty filters returns nothing. Without filters,
        every mapped asset is returned (the whole discovered catalog since Phase 3A).
        """
        stmt = (
            select(Asset, AssetIdentifier)
            .join(AssetIdentifier, and_(AssetIdentifier.asset_id == Asset.id))
            .where(
                AssetIdentifier.provider == provider,
                AssetIdentifier.valid_to.is_(None),
                AssetIdentifier.status == MappingStatus.ACTIVE,
            )
            .order_by(Asset.symbol)
        )
        if symbols is not None or provider_symbols is not None:
            wanted = sorted(set(symbols or ()))
            wanted_provider = sorted(set(provider_symbols or ()))
            if not wanted and not wanted_provider:
                return []
            stmt = stmt.where(
                or_(
                    Asset.symbol.in_(wanted),
                    AssetIdentifier.provider_symbol.in_(wanted_provider),
                )
            )
        return [(asset, mapping) for asset, mapping in self._session.execute(stmt)]
