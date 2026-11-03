"""Article symbol -> Atlas asset mapping (Phase 3C, D-079), method ``effective-dated-symbol-v1``.

Article symbols are provider symbols in the namespace of a market-data provider (Alpaca
news uses Alpaca's symbols). Each symbol is resolved with the effective-dated provider
mappings of 3A (``asset_identifiers``) **at the article's publication time**, never "today":

* ``MAPPED``: exactly one mapping was valid at publication. Inactive or delisted assets are
  still mapped (the article was about them then); their status is recorded.
* ``AMBIGUOUS``: a mapping was valid, but the symbol passed between different assets within
  ``NEWS_MAPPING_AMBIGUITY_WINDOW_HOURS`` of publication (a reused or changed ticker). The
  article could be about either; it is not forced onto one.
* ``NOT_EFFECTIVE``: Atlas knows the symbol, but no mapping covers the publication time
  (e.g. a mapping only observed later, D-053). Not forced onto today's asset.
* ``UNKNOWN_SYMBOL``: Atlas has never mapped the symbol (e.g. crypto pairs, foreign
  listings, instruments outside the discovered catalog).
* ``INVALID_SYMBOL``: not a symbol form of the mapping namespace. Observed on real Alpaca
  articles (2026-11-02): exchange-prefixed foreign listings (``TSX:CPX``), preferred-share
  notations (``PCG$A``) and trailing dots (``AT.``). Never guessed into a US listing.

Every outcome is stored with its reason and the candidate mappings considered. Articles
without any mapped asset are kept (market-wide or unlinked research).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clocks import ensure_utc
from app.data_engine.errors import InvalidProviderSymbolError
from app.database.models import Asset, AssetIdentifier
from app.news.classification import is_crypto_pair
from app.news.enums import MappingStatus

MAPPING_VERSION = "effective-dated-symbol-v1"


@dataclass(frozen=True)
class SymbolMapping:
    provider_symbol: str
    status: MappingStatus
    reason: str
    asset_id: uuid.UUID | None = None
    asset_identifier_id: uuid.UUID | None = None
    asset_symbol: str | None = None
    asset_name: str | None = None
    asset_status: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _Candidate:
    identifier: AssetIdentifier
    asset: Asset

    def covers(self, at: datetime) -> bool:
        ident = self.identifier
        return ident.valid_from <= at and (ident.valid_to is None or at < ident.valid_to)

    def describe(self) -> dict[str, Any]:
        ident = self.identifier
        return {
            "asset_id": str(self.asset.id),
            "asset_symbol": self.asset.symbol,
            "asset_status": self.asset.status.value,
            "mapping_id": str(ident.id),
            "valid_from": ident.valid_from.isoformat(),
            "valid_to": ident.valid_to.isoformat() if ident.valid_to else None,
            "valid_from_basis": ident.valid_from_basis.value,
        }


class SymbolMapper:
    def __init__(
        self,
        session: Session,
        *,
        namespace: str,
        normalize: Callable[[str], str],
        ambiguity_window: timedelta,
    ) -> None:
        self._session = session
        self._namespace = namespace
        self._normalize = normalize
        self._window = ambiguity_window

    def map(self, symbols: Sequence[str], published_at: datetime) -> list[SymbolMapping]:
        at = ensure_utc(published_at)
        valid: dict[str, str] = {}
        results: dict[str, SymbolMapping] = {}
        for symbol in symbols:
            try:
                valid[symbol] = self._normalize(symbol)
            except InvalidProviderSymbolError:
                results[symbol] = SymbolMapping(
                    symbol,
                    MappingStatus.INVALID_SYMBOL,
                    f"not a symbol form of the '{self._namespace}' namespace (e.g. an exchange "
                    "prefix or preferred-share notation); not mapped",
                )
        history = self._history(sorted(set(valid.values())))
        for symbol, normalized in valid.items():
            results[symbol] = self._resolve(symbol, history.get(normalized, []), at)
        return [results[s] for s in symbols]

    def _history(self, symbols: list[str]) -> dict[str, list[_Candidate]]:
        if not symbols:
            return {}
        rows = self._session.execute(
            select(AssetIdentifier, Asset)
            .join(Asset, Asset.id == AssetIdentifier.asset_id)
            .where(
                AssetIdentifier.provider == self._namespace,
                AssetIdentifier.provider_symbol.in_(symbols),
            )
            .order_by(AssetIdentifier.valid_from)
        ).all()
        history: dict[str, list[_Candidate]] = {}
        for ident, asset in rows:
            history.setdefault(ident.provider_symbol, []).append(_Candidate(ident, asset))
        return history

    def _resolve(self, symbol: str, candidates: list[_Candidate], at: datetime) -> SymbolMapping:
        described = [c.describe() for c in candidates]
        if not candidates:
            reason = (
                "a crypto pair: Atlas tracks no crypto assets yet"
                if is_crypto_pair(symbol)
                else f"Atlas has never mapped this symbol at '{self._namespace}'"
            )
            return SymbolMapping(symbol, MappingStatus.UNKNOWN_SYMBOL, reason)
        covering = [c for c in candidates if c.covers(at)]
        if not covering:
            return SymbolMapping(
                symbol,
                MappingStatus.NOT_EFFECTIVE,
                "mappings exist, but none was valid at the publication time; not assigned to "
                "a later or earlier holder of the symbol",
                candidates=described,
            )
        # The exclusion constraints of 0002 allow one covering mapping per symbol.
        chosen = covering[0]
        neighbours = [
            c for c in candidates if c.asset.id != chosen.asset.id and self._near(c.identifier, at)
        ]
        if neighbours:
            return SymbolMapping(
                symbol,
                MappingStatus.AMBIGUOUS,
                f"the symbol changed hands within {self._window} of publication; the article "
                "may concern either instrument",
                candidates=described,
            )
        status = chosen.asset.status.value
        reason = "mapping valid at publication"
        if status != "ACTIVE":
            reason += f"; the asset is {status.lower()} now"
        return SymbolMapping(
            symbol,
            MappingStatus.MAPPED,
            reason,
            asset_id=chosen.asset.id,
            asset_identifier_id=chosen.identifier.id,
            asset_symbol=chosen.asset.symbol,
            asset_name=chosen.asset.name,
            asset_status=status,
            candidates=described,
        )

    def _near(self, identifier: AssetIdentifier, at: datetime) -> bool:
        edges = [identifier.valid_from]
        if identifier.valid_to is not None:
            edges.append(identifier.valid_to)
        return any(abs(edge - at) <= self._window for edge in edges)


__all__ = ["MAPPING_VERSION", "SymbolMapper", "SymbolMapping"]
