"""Read side of market intelligence (Phase 3B): what was known at a given time.

* The **current view** at ``at`` is the newest finished LIVE run (RECORDED basis) that had
  completed by ``at``. Reconstructed results never stand in for what Atlas knew then.
* **History** lists MARKET classifications per decision time in a range; when several runs
  computed the same decision time, the one available last (by ``at``) is shown, with its
  basis, so recorded and reconstructed observations stay distinguishable.

Callers own the transaction; nothing here writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.database.models import (
    Asset,
    CrossAssetRelationshipRecord,
    IntelligenceRunRecord,
    MarketFeatureSnapshotRecord,
    MarketSummaryRecord,
    RegimeClassificationRecord,
)
from app.market_intelligence.enums import KnowledgeBasis, RunKind, RunStatus, Scope

FINISHED = (RunStatus.COMPLETED, RunStatus.PARTIALLY_COMPLETED)


def latest_live_run(
    session: Session, provider: str, feed: str, at: datetime
) -> IntelligenceRunRecord | None:
    return session.scalars(
        select(IntelligenceRunRecord)
        .where(
            IntelligenceRunRecord.provider == provider,
            IntelligenceRunRecord.feed == feed,
            IntelligenceRunRecord.kind == RunKind.LIVE,
            IntelligenceRunRecord.status.in_(FINISHED),
            IntelligenceRunRecord.completed_at <= at,
        )
        .order_by(IntelligenceRunRecord.as_of_end.desc(), IntelligenceRunRecord.completed_at.desc())
        .limit(1)
    ).first()


def market_rows(session: Session, run_id: uuid.UUID) -> list[RegimeClassificationRecord]:
    return list(
        session.scalars(
            select(RegimeClassificationRecord)
            .where(
                RegimeClassificationRecord.run_id == run_id,
                RegimeClassificationRecord.scope == Scope.MARKET,
            )
            .order_by(RegimeClassificationRecord.timeframe, RegimeClassificationRecord.dimension)
        )
    )


def summary(session: Session, run_id: uuid.UUID) -> MarketSummaryRecord | None:
    return session.scalars(
        select(MarketSummaryRecord)
        .where(MarketSummaryRecord.run_id == run_id)
        .order_by(MarketSummaryRecord.as_of.desc())
        .limit(1)
    ).first()


def regime_history(
    session: Session,
    *,
    timeframe: str,
    start: datetime,
    end: datetime,
    at: datetime,
    basis: KnowledgeBasis | None = None,
) -> list[RegimeClassificationRecord]:
    """MARKET classifications per (decision time, dimension), newest available by ``at``."""
    r = RegimeClassificationRecord
    stmt = (
        select(r)
        .where(
            r.scope == Scope.MARKET,
            r.timeframe == timeframe,
            r.as_of >= start,
            r.as_of <= end,
            r.available_at <= at,
        )
        .ext(distinct_on(r.as_of, r.dimension))
        .order_by(r.as_of, r.dimension, r.available_at.desc(), r.id.desc())
    )
    if basis is not None:
        stmt = stmt.where(r.basis == basis)
    return list(session.scalars(stmt))


def asset_rows(
    session: Session, run_id: uuid.UUID, timeframe: str
) -> list[tuple[MarketFeatureSnapshotRecord, str, dict[str, RegimeClassificationRecord]]]:
    """(snapshot, symbol, {dimension: classification}) per instrument of a run."""
    snapshots = session.execute(
        select(MarketFeatureSnapshotRecord, Asset.symbol)
        .join(Asset, Asset.id == MarketFeatureSnapshotRecord.asset_id)
        .where(
            MarketFeatureSnapshotRecord.run_id == run_id,
            MarketFeatureSnapshotRecord.timeframe == timeframe,
        )
        .order_by(Asset.symbol)
    ).all()
    classes: dict[uuid.UUID, dict[str, RegimeClassificationRecord]] = {}
    for row in session.scalars(
        select(RegimeClassificationRecord).where(
            RegimeClassificationRecord.run_id == run_id,
            RegimeClassificationRecord.scope == Scope.ASSET,
            RegimeClassificationRecord.timeframe == timeframe,
        )
    ):
        assert row.asset_id is not None  # noqa: S101  # ASSET scope rows have an asset
        classes.setdefault(row.asset_id, {})[row.dimension.value] = row
    return [(snap, symbol, classes.get(snap.asset_id, {})) for snap, symbol in snapshots]


def asset_history(
    session: Session, asset_id: uuid.UUID, *, timeframe: str, at: datetime, limit: int
) -> list[RegimeClassificationRecord]:
    r = RegimeClassificationRecord
    return list(
        session.scalars(
            select(r)
            .where(r.asset_id == asset_id, r.timeframe == timeframe, r.available_at <= at)
            .ext(distinct_on(r.as_of, r.dimension))
            .order_by(r.as_of.desc(), r.dimension, r.available_at.desc(), r.id.desc())
            .limit(limit * 2)
        )
    )


def relationships(session: Session, run_id: uuid.UUID) -> list[CrossAssetRelationshipRecord]:
    return list(
        session.scalars(
            select(CrossAssetRelationshipRecord).where(
                CrossAssetRelationshipRecord.run_id == run_id
            )
        )
    )


def symbols(session: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = session.execute(select(Asset.id, Asset.symbol).where(Asset.id.in_(list(ids))))
    return {row.id: row.symbol for row in rows}


def decimal_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "asset_history",
    "asset_rows",
    "latest_live_run",
    "market_rows",
    "regime_history",
    "relationships",
    "summary",
    "symbols",
]
