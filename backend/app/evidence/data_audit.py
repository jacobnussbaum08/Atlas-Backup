"""Audit of the historical dataset behind an evaluation (Phase 11A).

The audit reads the same bars a backtest would (``app.backtesting.data.load_dataset``, as
known at ``known_at``) and checks, per symbol and for the benchmark:

* **Calendar** - every XNYS session of the period has a benchmark bar, and no bar is stamped
  on a closed day (an independent check of the rule-derived calendar, which is verified
  against NYSE only for 2025-2027).
* **Coverage** - RAW and SPLIT bars per expected session (from the later of the period start
  and the symbol's first bar), missing sessions, invalid (quarantined or incomplete) bars.
* **Listing** - symbols whose first bar is after the period start (listed later): they
  cannot have been traded before, and the evaluation has fewer assets early on.
* **Corporate actions** - split events implied by the RAW/SPLIT price ratio, with ratios
  that are not simple fractions flagged for review. Dividends are not modelled anywhere in
  Atlas (price returns only).
* **Outliers** - split-adjusted daily moves larger than 25%, listed for review (they may be
  real; they are never corrected).
* **Survivorship** - an EXPLICIT universe chosen from today's listings contains only assets
  that survived to today. The audit says so, counts the database's delisted/inactive
  assets, and never claims the bias is absent.
* **Point in time** - backfilled (HISTORICAL_REST) bars were not observed live; a replay
  assumes each became available at its session close plus the settle delay.

Status: FAIL when the data cannot support conclusions (benchmark or a symbol missing more
than the tolerated share of sessions, or unreadable); WARN when there are limitations to
report; PASS otherwise. Nothing is repaired or invented.
"""

from __future__ import annotations

import itertools
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.asset_discovery.sessions import local_date, trading_day
from app.backtesting.data import CALENDAR, load_dataset, sessions_between
from app.data_engine import calendar as nyse
from app.database.models import Asset, AssetStatus, MarketBarRecord
from app.evidence.enums import AuditStatus

DAILY = "1Day"
MAX_MISSING_SHARE = 0.02  # a symbol missing more than 2% of its sessions fails the audit
MAX_BENCHMARK_MISSING_SHARE = 0.005
OUTLIER_MOVE = 0.25
LATE_LISTING_SESSIONS = 5


@dataclass(frozen=True)
class AuditScope:
    symbols: tuple[str, ...]
    start: date
    end: date
    benchmark: str | None
    provider: str
    feed: str
    known_at: datetime
    settle_seconds: int = 3600
    universe_mode: str = "EXPLICIT"
    universe_selected_on: date | None = None  # when the symbol list was chosen

    def as_json(self) -> dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "benchmark": self.benchmark,
            "provider": self.provider,
            "feed": self.feed,
            "timeframe": DAILY,
            "known_at": self.known_at.isoformat(),
            "settle_seconds": self.settle_seconds,
            "universe_mode": self.universe_mode,
            "universe_selected_on": None
            if self.universe_selected_on is None
            else self.universe_selected_on.isoformat(),
        }


def _simple_ratio(ratio: Decimal) -> bool:
    """A split ratio such as 2, 3, 1/5 or 3/2 (small numerator and denominator)."""
    f = Fraction(str(ratio)).limit_denominator(20)
    return abs(float(f) - float(ratio)) < 1e-3 and f.numerator <= 50


def _bars_on_closed_days(
    session: Session, asset_ids: list[uuid.UUID], scope: AuditScope
) -> dict[uuid.UUID, list[str]]:
    b = MarketBarRecord
    start = datetime.combine(scope.start, datetime.min.time(), tzinfo=scope.known_at.tzinfo)
    end = datetime.combine(scope.end + timedelta(days=1), datetime.min.time()).replace(
        tzinfo=scope.known_at.tzinfo
    )
    out: dict[uuid.UUID, list[str]] = {}
    rows = session.execute(
        select(b.asset_id, b.event_time)
        .where(
            b.asset_id.in_(asset_ids),
            b.timeframe == DAILY,
            b.provider == scope.provider,
            b.feed == scope.feed,
            b.event_time >= start,
            b.event_time < end,
            b.ingested_at <= scope.known_at,
        )
        .distinct()
    ).all()
    for asset_id, at in rows:
        day = local_date(CALENDAR, at)
        if trading_day(CALENDAR, day) is None:
            out.setdefault(asset_id, []).append(day.isoformat())
    return {k: sorted(set(v)) for k, v in out.items()}


def _source_counts(
    session: Session, asset_ids: list[uuid.UUID], scope: AuditScope
) -> dict[str, int]:
    b = MarketBarRecord
    start = datetime.combine(scope.start, datetime.min.time(), tzinfo=scope.known_at.tzinfo)
    rows = session.execute(
        select(b.source, func.count())
        .where(
            b.asset_id.in_(asset_ids),
            b.timeframe == DAILY,
            b.provider == scope.provider,
            b.feed == scope.feed,
            b.event_time >= start,
            b.ingested_at <= scope.known_at,
        )
        .group_by(b.source)
    ).all()
    return {str(getattr(s, "value", s)): int(n) for s, n in rows}


def audit(session: Session, scope: AuditScope) -> tuple[AuditStatus, dict[str, Any]]:
    symbols = sorted(set(scope.symbols) | ({scope.benchmark} if scope.benchmark else set()))
    assets = {
        a.symbol: a for a in session.scalars(select(Asset).where(Asset.symbol.in_(symbols))).all()
    }
    missing_assets = [s for s in symbols if s not in assets]
    ids = [a.id for a in assets.values()]
    sessions = [s.day for s in sessions_between(scope.start, scope.end)]
    fails: list[str] = []
    warns: list[str] = []
    if missing_assets:
        fails.append("unknown symbols: " + ", ".join(missing_assets))
    if not sessions:
        fails.append("the period holds no XNYS session")
    dataset = load_dataset(
        session,
        ids,
        provider=scope.provider,
        feed=scope.feed,
        first=scope.start,
        last=scope.end,
        known_at=scope.known_at,
        settle=timedelta(seconds=scope.settle_seconds),
        split_adjusted=True,
    )
    closed_day_bars = _bars_on_closed_days(session, ids, scope) if ids else {}
    unverified_years = sorted({d.year for d in sessions if d.year not in nyse.VERIFIED_YEARS})
    if unverified_years:
        warns.append(
            "calendar years not verified against NYSE (rule-derived): "
            + ", ".join(str(y) for y in unverified_years)
            + "; checked against the benchmark's bars below"
        )

    per_symbol: dict[str, Any] = {}
    late: list[str] = []
    events_all: list[dict[str, Any]] = []
    for symbol in symbols:
        a = assets.get(symbol)
        if a is None:
            continue
        raw = dataset.raw.get(a.id, {})
        split = dataset.split.get(a.id, {})
        if not raw:
            per_symbol[symbol] = {"bars": 0, "status": "NO_DATA"}
            fails.append(f"{symbol}: no RAW daily bars in the period")
            continue
        first = min(raw)
        expected = [d for d in sessions if d >= first]
        missing_raw = [d for d in expected if d not in raw]
        missing_split = [d for d in expected if d not in split]
        invalid = sorted(d for d, bar in raw.items() if not bar.valid)
        zero_volume = sorted(d for d, bar in raw.items() if not bar.volume)
        listed_late = sessions.index(first) >= LATE_LISTING_SESSIONS if first in sessions else False
        if listed_late:
            late.append(symbol)
        outliers = []
        days = sorted(split)
        for before, after in itertools.pairwise(days):
            p0, p1 = split[before].close, split[after].close
            if p0 > 0 and abs(float(p1 / p0) - 1) > OUTLIER_MOVE:
                outliers.append({"session": after.isoformat(), "move": float(p1 / p0 - 1)})
        events = [
            {
                "symbol": symbol,
                "session": e.session.isoformat(),
                "ratio": str(e.ratio),
                "simple_ratio": _simple_ratio(e.ratio),
            }
            for e in dataset.splits.get(a.id, [])
        ]
        events_all += events
        share = len(missing_raw) / len(expected) if expected else 1.0
        tolerance = MAX_BENCHMARK_MISSING_SHARE if symbol == scope.benchmark else MAX_MISSING_SHARE
        if share > tolerance:
            fails.append(
                f"{symbol}: {len(missing_raw)} of {len(expected)} sessions without a RAW bar "
                f"({share:.1%} > {tolerance:.1%})"
            )
        elif missing_raw or missing_split:
            warns.append(
                f"{symbol}: {len(missing_raw)} RAW / {len(missing_split)} SPLIT sessions missing"
            )
        if invalid:
            warns.append(f"{symbol}: {len(invalid)} invalid (quarantined/incomplete) bars")
        if closed_day_bars.get(a.id):
            warns.append(f"{symbol}: bars stamped on closed days {closed_day_bars[a.id][:5]}")
        per_symbol[symbol] = {
            "asset_status": a.status.value,
            "first_bar": first.isoformat(),
            "last_bar": max(raw).isoformat(),
            "expected_sessions": len(expected),
            "raw_bars": len(raw),
            "split_bars": len(split),
            "missing_raw_sessions": [d.isoformat() for d in missing_raw],
            "missing_split_sessions": [d.isoformat() for d in missing_split],
            "missing_share": share,
            "invalid_bars": [d.isoformat() for d in invalid],
            "zero_volume_bars": len(zero_volume),
            "bars_on_closed_days": closed_day_bars.get(a.id, []),
            "listed_after_start": listed_late,
            "implied_splits": events,
            "large_moves": outliers,
        }
    odd = [e for e in events_all if not e["simple_ratio"]]
    if odd:
        warns.append(f"{len(odd)} implied corporate actions with unusual ratios: review them")
    if late:
        warns.append("listed after the period start: " + ", ".join(late))

    # Calendar check against the benchmark (it trades every session).
    calendar: dict[str, Any] = {"sessions": len(sessions)}
    if scope.benchmark and scope.benchmark in assets:
        bench = dataset.raw.get(assets[scope.benchmark].id, {})
        no_bar = [d.isoformat() for d in sessions if d not in bench]
        calendar.update(
            {
                "benchmark": scope.benchmark,
                "sessions_without_benchmark_bar": no_bar,
                "benchmark_bars_on_closed_days": closed_day_bars.get(
                    assets[scope.benchmark].id, []
                ),
            }
        )
        if calendar["benchmark_bars_on_closed_days"]:
            fails.append(
                "the benchmark has bars on days the calendar says were closed: "
                + ", ".join(calendar["benchmark_bars_on_closed_days"][:10])
            )

    # Survivorship.
    statuses = Counter(a.status.value for s, a in assets.items() if s != scope.benchmark)
    delisted_known = int(
        session.scalar(
            select(func.count(Asset.id)).where(
                Asset.status.in_([AssetStatus.DELISTED, AssetStatus.INACTIVE])
            )
        )
        or 0
    )
    survivorship: dict[str, Any] = {
        "universe_mode": scope.universe_mode,
        "universe_selected_on": None
        if scope.universe_selected_on is None
        else scope.universe_selected_on.isoformat(),
        "universe_asset_statuses": dict(statuses),
        "delisted_or_inactive_in_universe": sum(
            n for k, n in statuses.items() if k != AssetStatus.ACTIVE.value
        ),
        "delisted_or_inactive_assets_in_database": delisted_known,
        "bias": "PRESENT"
        if scope.universe_mode == "EXPLICIT"
        and statuses.get(AssetStatus.ACTIVE.value, 0) == sum(statuses.values())
        else "POSSIBLE",
        "statement": (
            "The universe was chosen from assets listed when it was selected; every one of "
            "them survived the whole period. Results exclude assets that were delisted, "
            "acquired or went bankrupt during it, which usually flatters long strategies "
            "and penalizes short ones. The size of the bias is not measured."
        ),
    }
    warns.append(f"survivorship bias: {survivorship['bias']}")

    sources = _source_counts(session, ids, scope) if ids else {}
    point_in_time = {
        "bar_sources": sources,
        "statement": (
            "Backfilled bars (HISTORICAL_REST) were not observed live: a replay assumes each "
            "became available at its session close plus the settle delay. Split-adjusted "
            "bars are the provider's current adjustment; the replay applies split factors "
            "only from each split's effective session (point-in-time)."
        ),
        "dividends": "not modelled: returns are price returns for the portfolio and the "
        "benchmark alike",
    }
    if sources.get("HISTORICAL_REST"):
        warns.append("point-in-time availability of backfilled bars is assumed, not observed")

    status = AuditStatus.FAIL if fails else (AuditStatus.WARN if warns else AuditStatus.PASS)
    result = {
        "status": status.value,
        "failures": fails,
        "warnings": warns,
        "calendar": calendar,
        "unverified_calendar_years": unverified_years,
        "symbols": per_symbol,
        "corporate_actions": events_all,
        "survivorship": survivorship,
        "point_in_time": point_in_time,
        "dataset_fingerprint_parts": len(dataset.fingerprint_parts),
    }
    return status, result


__all__ = ["AuditScope", "audit"]
