"""Market intelligence at one decision time, from already-selected inputs (Phase 3B).

Pure computation: the caller (``service``) chooses which bars, members and sessions were
available at ``as_of``; nothing here reads the database or the clock. Given the same
inputs it returns the same results, which is what makes stored runs reproducible.

Per instrument and timeframe: a feature set now and ``change_lookback`` bars earlier, then
trend and volatility classifications. For the market: the benchmark's trend and volatility
(MARKET scope), participation among qualified members, cross-asset relationships and a
stability classification combining them.

Weekly bars are aggregated from daily bars of *complete* weeks only (a week whose last
session has not completed yet is left out), dated at the week's first session.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time

from app.asset_discovery.sessions import (
    ExchangeCalendar,
    SessionDay,
    next_trading_day,
    previous_trading_day,
)
from app.feature_engine.features import (
    Bar,
    FeatureSet,
    TimeframeSpec,
    compute_features,
    weekly_bars,
)
from app.market_intelligence.classification import Classification
from app.market_intelligence.config import IntelligenceConfig
from app.market_intelligence.correlation import CrossAssetResult, cross_asset
from app.market_intelligence.participation import MemberView, Participation, participation
from app.market_intelligence.stability import classify_stability, stress_evidence
from app.market_intelligence.trend import classify_trend
from app.market_intelligence.volatility import classify_volatility

DAILY, WEEKLY = "1Day", "1Week"


@dataclass(frozen=True)
class AssetInput:
    asset_id: uuid.UUID
    symbol: str
    # Completed, VALID daily bars known at the decision time, keyed by session date.
    bars: Mapping[date, Bar]


@dataclass(frozen=True)
class AssetResult:
    asset_id: uuid.UUID
    symbol: str
    timeframe: str
    features: FeatureSet
    previous: FeatureSet | None
    trend: Classification
    volatility: Classification


@dataclass
class IntelligenceResult:
    as_of: datetime
    benchmark_id: uuid.UUID | None
    assets: list[AssetResult] = field(default_factory=list)
    market: dict[str, list[Classification]] = field(default_factory=dict)
    participation: Participation | None = None
    cross_asset: CrossAssetResult | None = None
    members_total: int = 0

    def asset(self, asset_id: uuid.UUID, timeframe: str) -> AssetResult | None:
        return next(
            (a for a in self.assets if a.asset_id == asset_id and a.timeframe == timeframe), None
        )


def session_midnight(calendar: ExchangeCalendar, day: date) -> datetime:
    """Event time of a session's daily bar: local midnight (the provider's convention)."""
    return datetime.combine(day, time(0), tzinfo=calendar.zone).astimezone(UTC)


def complete_weeks(calendar: ExchangeCalendar, days: Sequence[date]) -> dict[tuple[int, int], date]:
    """ISO weeks fully contained in ``days`` -> the week's first session date.

    ``days`` are every completed session of a span, oldest first, without holes. A week is
    complete when the trading day before its first listed session and the one after its
    last listed session both fall in other weeks: the newest week, still in progress, and
    the oldest, cut by the span's start, are left out.
    """
    weeks: dict[tuple[int, int], list[date]] = {}
    for day in days:
        iso = day.isocalendar()
        weeks.setdefault((iso.year, iso.week), []).append(day)
    complete: dict[tuple[int, int], date] = {}
    for key, members in weeks.items():
        before = previous_trading_day(calendar, members[0]).day.isocalendar()
        after = next_trading_day(calendar, members[-1]).day.isocalendar()
        if (before.year, before.week) != key and (after.year, after.week) != key:
            complete[key] = members[0]
    return complete


def _daily_series(series: Mapping[date, Bar], days: Sequence[date]) -> list[Bar]:
    return [series[d] for d in days if d in series]


def _weekly_series(
    calendar: ExchangeCalendar,
    series: Mapping[date, Bar],
    days: Sequence[date],
    weeks: Mapping[tuple[int, int], date],
) -> list[Bar]:
    present = [d for d in days if d in series]
    built = weekly_bars([series[d] for d in present], present, set(weeks))
    # Date every weekly bar at its week's first *expected* session, so instruments missing
    # that day still align with the benchmark.
    keyed: dict[tuple[int, int], date] = {}
    for d in present:
        iso = d.isocalendar()
        keyed.setdefault((iso.year, iso.week), d)
    starts = [session_midnight(calendar, weeks[k]) for k in sorted(weeks) if k in keyed]
    return [replace(bar, event_time=start) for bar, start in zip(built, starts, strict=True)]


def _features(
    bars: Sequence[Bar],
    spec: TimeframeSpec,
    as_of: datetime,
    expected: int,
    benchmark: Mapping[datetime, float] | None,
    cutoff: datetime | None = None,
) -> FeatureSet:
    used = bars if cutoff is None else [b for b in bars if b.event_time <= cutoff]
    return compute_features(used, spec, as_of=as_of, expected_bars=expected, benchmark=benchmark)


def compute(
    *,
    as_of: datetime,
    calendar: ExchangeCalendar,
    sessions: Sequence[SessionDay],
    inputs: Mapping[uuid.UUID, AssetInput],
    benchmark_id: uuid.UUID | None,
    members: Sequence[uuid.UUID],
    correlation_assets: Sequence[uuid.UUID],
    config: IntelligenceConfig,
    feed: str,
    single_venue: bool,
) -> IntelligenceResult:
    """Everything at ``as_of``. ``sessions`` are the sessions completed by ``as_of`` (oldest
    first, enough for every window); ``members`` the qualified-universe members then."""
    days = [s.day for s in sessions]
    weeks = complete_weeks(calendar, days)
    result = IntelligenceResult(as_of=as_of, benchmark_id=benchmark_id, members_total=len(members))
    benchmark_input = inputs.get(benchmark_id) if benchmark_id is not None else None

    series: dict[str, dict[uuid.UUID, list[Bar]]] = {DAILY: {}, WEEKLY: {}}
    for asset_id, item in inputs.items():
        series[DAILY][asset_id] = _daily_series(item.bars, days)
        series[WEEKLY][asset_id] = _weekly_series(calendar, item.bars, days, weeks)
    week_starts = [session_midnight(calendar, weeks[k]) for k in sorted(weeks)]

    for spec in config.timeframes:
        timeframe = spec.timeframe
        slots = [session_midnight(calendar, d) for d in days] if timeframe == DAILY else week_starts
        expected = min(spec.window, len(slots))
        cutoff = slots[-spec.change_lookback - 1] if len(slots) > spec.change_lookback else None
        bench = (
            {b.event_time: b.close for b in series[timeframe][benchmark_id]}
            if benchmark_id is not None and benchmark_input is not None
            else None
        )
        for asset_id, item in inputs.items():
            bars = series[timeframe][asset_id]
            current = _features(bars, spec, as_of, expected, bench)
            previous = (
                None
                if cutoff is None
                else _features(bars, spec, as_of, expected, bench, cutoff=cutoff)
            )
            result.assets.append(
                AssetResult(
                    asset_id=asset_id,
                    symbol=item.symbol,
                    timeframe=timeframe,
                    features=current,
                    previous=previous,
                    trend=classify_trend(current, previous, spec, config.trend),
                    volatility=classify_volatility(current, previous, spec, config.volatility),
                )
            )

    member_views = []
    for asset_id in members:
        daily = result.asset(asset_id, DAILY)
        if daily is not None:
            member_views.append(
                MemberView(
                    asset_id, daily.symbol, daily.features, daily.previous, daily.trend.label
                )
            )
    result.participation = participation(
        member_views, len(members), config.participation, feed=feed, single_venue=single_venue
    )

    chosen = [a for a in correlation_assets if a in inputs][: config.correlation.max_assets]
    if benchmark_id is not None and benchmark_input is not None and benchmark_id not in chosen:
        chosen.append(benchmark_id)
    if len(chosen) >= 2:
        slots = [session_midnight(calendar, d) for d in days]
        closes = {a: {b.event_time: b.close for b in series[DAILY][a]} for a in chosen}
        result.cross_asset = cross_asset(closes, slots, config.correlation, benchmark=benchmark_id)

    for spec in config.timeframes:
        bench_result = result.asset(benchmark_id, spec.timeframe) if benchmark_id else None
        if bench_result is not None:
            result.market[spec.timeframe] = [bench_result.trend, bench_result.volatility]
    daily_bench = result.asset(benchmark_id, DAILY) if benchmark_id else None
    evidence = stress_evidence(
        benchmark_volatility=None if daily_bench is None else daily_bench.volatility,
        range_ratio=None if daily_bench is None else daily_bench.features.get("range_ratio"),
        drawdown=None if daily_bench is None else daily_bench.features.get("drawdown_from_high"),
        average_correlation=None
        if result.cross_asset is None
        else result.cross_asset.average_correlation,
        above_slow_ma=result.participation.metrics.get("above_slow_ma")
        if result.participation.status == "OK"
        else None,
        cfg=config.stability,
        volatility_cfg=config.volatility,
    )
    result.market.setdefault(DAILY, []).append(classify_stability(evidence, config.stability))
    return result


__all__ = [
    "DAILY",
    "WEEKLY",
    "AssetInput",
    "AssetResult",
    "IntelligenceResult",
    "complete_weeks",
    "compute",
    "session_midnight",
]
