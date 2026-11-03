"""Detection of validated updates that require re-evaluation (Phase 4A).

* **Bar close**: the newest daily session that has completed and settled; one update per
  qualified-universe member whose stored daily bar for that session is VALID and was
  available by now. When that session also completes an ISO week, a weekly update follows.
* **News event**: news events that became available since the previous check and are
  directly related to a qualified-universe member.

Detection is idempotent by construction: an update names its period or news event, and an
evaluation's idempotency key includes it, so detecting the same update again (each worker
cycle, or after a restart) never evaluates twice.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.asset_discovery import membership
from app.asset_discovery.enums import UniverseLayer
from app.asset_discovery.sessions import ExchangeCalendar, next_trading_day
from app.database.models import (
    EventAssetRelationshipRecord,
    MarketBarRecord,
    NewsEventRecord,
)
from app.feature_engine.registry import DAILY, WEEKLY
from app.market_intelligence.engine import session_midnight
from app.strategies.engine import MarketUpdate
from app.strategies.enums import TriggerKind
from app.strategies.inputs import BarSeries
from app.strategies.periods import completed_daily, week_key


def scope(session: Session, at: datetime) -> list[uuid.UUID]:
    """Assets strategies are evaluated for: the qualified universe as recorded by ``at``."""
    return sorted(membership.members_at(session, UniverseLayer.QUALIFIED_UNIVERSE, at))


def bar_close_updates(
    session: Session, now: datetime, series: BarSeries, assets: Sequence[uuid.UUID]
) -> list[MarketUpdate]:
    if not assets:
        return []
    calendar: ExchangeCalendar = series.calendar
    latest = completed_daily(calendar, now, 1, series.settle)[0]
    b = MarketBarRecord
    ready = set(
        session.scalars(
            select(b.asset_id)
            .where(
                b.asset_id.in_(list(assets)),
                b.timeframe == DAILY,
                b.provider == series.provider,
                b.feed == series.feed,
                b.adjustment == series.adjustment,
                b.event_time == session_midnight(calendar, latest.day),
                b.quality_status == "VALID",
                b.available_at <= now,
                b.ingested_at <= now,
            )
            .distinct()
        )
    )
    week_done = week_key(next_trading_day(calendar, latest.day).day) != week_key(latest.day)
    year, week = week_key(latest.day)
    updates = []
    for asset_id in sorted(ready):
        daily_ref = f"{DAILY}:{latest.day}"
        updates.append(MarketUpdate(asset_id, TriggerKind.BAR_CLOSE, daily_ref, DAILY))
        if week_done:
            updates.append(
                MarketUpdate(
                    asset_id, TriggerKind.BAR_CLOSE, f"{WEEKLY}:{year}-W{week:02d}", WEEKLY
                )
            )
    return updates


def news_updates(
    session: Session, since: datetime, now: datetime, assets: Sequence[uuid.UUID]
) -> list[MarketUpdate]:
    if not assets:
        return []
    e, rel = NewsEventRecord, EventAssetRelationshipRecord
    rows = session.execute(
        select(rel.asset_id, e.article_id, e.id)
        .join(e, e.id == rel.event_id)
        .where(
            rel.asset_id.in_(list(assets)),
            e.available_at > since,
            e.available_at <= now,
        )
        .order_by(e.available_at, e.id)
    ).all()
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    updates = []
    for row in rows:
        if (row.asset_id, row.article_id) in seen:
            continue  # one update per article and asset per check
        seen.add((row.asset_id, row.article_id))
        updates.append(
            MarketUpdate(row.asset_id, TriggerKind.NEWS_EVENT, f"news:{row.article_id}:{row.id}")
        )
    return updates


__all__ = ["bar_close_updates", "news_updates", "scope"]
