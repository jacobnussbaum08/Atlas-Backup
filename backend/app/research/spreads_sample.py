"""Coverage sample of 2017-2019 quoted spreads for research members (D-239). Simulation
database only; GET-only provider requests.

One 60-second window at the open (+1 min) and one before the close (-15 min) on the first
session of each month, for the members of each current universe in effect that session
(and not yet delisted), measured with the D-234 sampler into ``asset_metric_values`` under
each member's own asset row. A monthly sample measures the spread *distribution* (for a
pre-registration's assumed spread and its stress); it is not a replay input: the replay
accepts a measurement no older than ``spreads.max_age_hours`` (at most 96 hours), so a
reserved run's own symbols need daily sampling before it (a readiness step).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.data import sessions_between
from app.backtesting.spreads import CLOSE_WINDOW, OPEN_WINDOW, sample
from app.research import store
from app.research.bars import identity_of, members
from app.research.universe import CURRENT_UNIVERSES

FIRST, LAST = date(2017, 1, 3), date(2019, 12, 31)


def monthly_sessions() -> list[date]:
    out: dict[tuple[int, int], date] = {}
    for s in sessions_between(FIRST, LAST):
        out.setdefault((s.day.year, s.day.month), s.day)
    return sorted(out.values())


def run(factory: sessionmaker[Session], progress: Any = print) -> dict[str, Any]:
    """Members are requested by their own ticker. The data adapter cannot pin the
    provider's symbol mapping, so quotes follow the ticker's current holder: a
    formation-era security whose ticker now belongs to another (identity
    ``same_as_current_holder`` false) is not sampled, and is listed."""
    now = datetime.now(UTC)
    with factory() as session:
        spans: dict[uuid.UUID, list[store.Span]] = {}
        for code in CURRENT_UNIVERSES:
            for asset_id, found in store.universe_members(
                session, code, known_at=now, apply_exclusions=False
            ).items():
                spans.setdefault(asset_id, []).extend(found)
        delisted = store.delistings(session, list(spans), known_at=now)
        ticker_of: dict[uuid.UUID, str] = {}
        unsampled: list[str] = []
        for a, s in members(session, current=True):
            ident = identity_of(session, a)
            details = (ident.details or {}) if ident is not None else {}
            if details.get("same_as_current_holder") is False:
                unsampled.append(f"{s} (as of {details.get('asof')})")
                continue
            ticker_of[a] = details.get("request_symbol", s)
    totals: dict[str, int] = {}
    for day in monthly_sessions():
        ids = {
            ticker_of[a]: a
            for a, found in spans.items()
            if a in ticker_of
            and any(sp.contains(day) for sp in found)
            and (a not in delisted or day <= delisted[a])
        }
        for window in (OPEN_WINDOW, CLOSE_WINDOW):
            counts = asyncio.run(
                sample(
                    factory,
                    feed="sip",
                    symbols=sorted(ids),
                    start=day,
                    end=day,
                    window=window,
                    asset_ids=ids,
                )
            )
            for k, v in counts.items():
                totals[f"{window.key}_{k}"] = totals.get(f"{window.key}_{k}", 0) + v
        progress(f"{day} members {len(ids)} {totals}")
    return {
        "sessions": len(monthly_sessions()),
        **totals,
        "not_sampled_formation_era": sorted(set(unsampled)),
    }


__all__ = ["monthly_sessions", "run"]
