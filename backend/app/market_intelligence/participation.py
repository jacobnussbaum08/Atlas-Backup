"""Participation among the qualified universe (Phase 3B).

Computed only over assets that were qualified-universe members **at the decision time**
(point-in-time membership, Phase 3A) and have a usable daily feature set then. This is
*universe participation*, not market breadth: the universe is a bounded subset of what
Alpaca lists, prices and volumes come from the configured feed (on IEX a single venue),
and members without prepared history are excluded. Every result says how many members it
covers and lists these limitations.

Advancers and decliners count only members whose newest bar is the newest session any
member has: a stale bar is neither advancing nor declining.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.feature_engine.features import FeatureQuality, FeatureSet
from app.market_intelligence.config import ParticipationConfig
from app.market_intelligence.trend import BEARISH, BULLISH

ALGORITHM = "participation-v1"


@dataclass(frozen=True)
class MemberView:
    asset_id: uuid.UUID
    symbol: str
    current: FeatureSet
    previous: FeatureSet | None
    trend_label: str | None


@dataclass(frozen=True)
class Participation:
    status: str  # OK | INSUFFICIENT_DATA
    members_total: int
    members_evaluated: int
    metrics: dict[str, Any]
    limitations: list[str]
    reasons: list[str] = field(default_factory=list)


def _share(values: Sequence[bool]) -> float | None:
    return None if not values else sum(values) / len(values)


def _above(features: FeatureSet, name: str) -> bool | None:
    value = features.get(name)
    return None if value is None else value > 0


def participation(
    members: Sequence[MemberView],
    members_total: int,
    cfg: ParticipationConfig,
    *,
    feed: str,
    single_venue: bool,
) -> Participation:
    usable = [m for m in members if m.current.quality is not FeatureQuality.INSUFFICIENT_DATA]
    limitations = [
        f"Universe participation over {len(usable)} of {members_total} qualified-universe "
        "members with usable daily history; not market-wide breadth.",
        "The qualified universe is a bounded subset of the instruments the provider lists.",
    ]
    if single_venue:
        limitations.append(
            f"Feed '{feed}' is a single venue: prices and volumes are not consolidated "
            "US market activity."
        )
    if len(usable) < cfg.min_members:
        return Participation(
            status="INSUFFICIENT_DATA",
            members_total=members_total,
            members_evaluated=len(usable),
            metrics={},
            limitations=limitations,
            reasons=[f"{len(usable)} usable members; at least {cfg.min_members} needed"],
        )

    def shares(views: Sequence[FeatureSet], name: str) -> float | None:
        flags = [f for v in views if (f := _above(v, name)) is not None]
        return _share(flags)

    current = [m.current for m in usable]
    latest = max(m.current.last_event_at for m in usable if m.current.last_event_at)
    fresh = [m.current for m in usable if m.current.last_event_at == latest]
    moves = [r for f in fresh if (r := f.get("return_1")) is not None]
    advancers = sum(1 for r in moves if r > 0)
    decliners = sum(1 for r in moves if r < 0)
    labels = [m.trend_label for m in usable if m.trend_label is not None]
    changes = [abs(r) for f in current if (r := f.get("roc")) is not None]
    top = max(1, round(len(changes) * cfg.concentration_top_share))
    concentration = (
        sum(sorted(changes, reverse=True)[:top]) / sum(changes) if sum(changes) > 0 else None
    )
    strength = [rs for f in current if (rs := f.get("relative_strength")) is not None]
    previous = [
        m.previous
        for m in usable
        if m.previous is not None and m.previous.quality is not FeatureQuality.INSUFFICIENT_DATA
    ]
    above_slow_now = shares(current, "close_vs_sma_slow")
    above_slow_before = shares(previous, "close_vs_sma_slow") if previous else None
    metrics: dict[str, Any] = {
        "above_fast_ma": shares(current, "close_vs_sma_fast"),
        "above_slow_ma": above_slow_now,
        "above_slow_ma_previous": above_slow_before,
        "above_slow_ma_change": None
        if above_slow_now is None or above_slow_before is None
        else above_slow_now - above_slow_before,
        "positive_momentum": shares(current, "roc"),
        "latest_session_event_at": latest.isoformat() if isinstance(latest, datetime) else None,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": len(moves) - advancers - decliners,
        "advance_decline_ratio": None if decliners == 0 else advancers / decliners,
        "stale_excluded": len(usable) - len(fresh),
        "new_highs": sum(1 for f in current if f.get("new_high") == 1.0),
        "new_lows": sum(1 for f in current if f.get("new_low") == 1.0),
        "bullish_share": _share([label == BULLISH for label in labels]),
        "bearish_share": _share([label == BEARISH for label in labels]),
        "median_relative_strength": statistics.median(strength) if strength else None,
        "concentration_top_share": concentration,
        "concentration_top_count": top,
    }
    return Participation(
        status="OK",
        members_total=members_total,
        members_evaluated=len(usable),
        metrics=metrics,
        limitations=limitations,
    )


__all__ = ["ALGORITHM", "MemberView", "Participation", "participation"]
