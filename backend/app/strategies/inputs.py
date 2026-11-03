"""Point-in-time inputs of an evaluation and their feature snapshot (Phase 4A).

``compute_features`` gathers, for one asset and timeframe at a data cutoff:

* the periods that had **completed and settled** by the cutoff (exchange calendar,
  ``settle_seconds`` after the close); the period in progress is never an input;
* the newest revision of each of those periods' daily bars that Atlas had stored by the
  knowledge time (``reads.daily_bars``, the Phase 3B query); only VALID bars count;
* stored Phase 3B regimes and Phase 3C news events available by then
  (``app.feature_engine.stored``);
* for benchmark features (Phase 4B), the benchmark asset's bars for exactly the same
  periods under the same rules.

Knowledge basis (``KnowledgeBasis``, as Phase 3B): RECORDED evaluations use what Atlas had
stored by the cutoff itself (``known_at = data_cutoff``), the strict point-in-time view a
live strategy had. RECONSTRUCTED ones (historical re-evaluation) use data retrieved later
for periods that had completed by the cutoff, and are labelled as such.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.asset_discovery.metrics import DailyBar
from app.asset_discovery.sessions import ExchangeCalendar, SessionDay
from app.data_engine.enums import PriceAdjustment
from app.database.models import Asset, AssetClass, AssetStatus, StrategyFeatureSnapshotRecord
from app.database.repositories import universe as reads
from app.feature_engine.bar_features import BarHistory, compute_bar_features
from app.feature_engine.benchmark import Benchmark, compute_benchmark_feature
from app.feature_engine.features import Bar, weekly_bars
from app.feature_engine.registry import (
    DAILY,
    FEATURE_REGISTRY_VERSION,
    FeatureSource,
    FeatureValue,
    ResolvedFeature,
    sha256_json,
)
from app.feature_engine.stored import StoredContext, compute_stored_features
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.periods import completed_daily, completed_weeks, sessions_for


@dataclass(frozen=True)
class BarSeries:
    """Which stored bar series feeds the bar features."""

    provider: str
    feed: str
    adjustment: PriceAdjustment
    calendar: ExchangeCalendar
    settle: timedelta


@dataclass(frozen=True)
class FeatureInputs:
    asset_id: uuid.UUID
    timeframe: str
    data_cutoff: datetime
    known_at: datetime
    basis: KnowledgeBasis
    series: BarSeries
    values: dict[str, FeatureValue]
    last_period_event_at: datetime | None

    @property
    def inputs_available_at(self) -> datetime | None:
        return max((v.available_at for v in self.values.values() if v.available_at), default=None)

    def snapshot_json(self) -> dict[str, Any]:
        return {key: self.values[key].to_json() for key in sorted(self.values)}

    def content_sha256(self) -> str:
        return sha256_json(
            {
                "asset_id": str(self.asset_id),
                "timeframe": self.timeframe,
                "data_cutoff": self.data_cutoff.isoformat(),
                "known_at": self.known_at.isoformat(),
                "basis": self.basis.value,
                "series": [self.series.provider, self.series.feed, self.series.adjustment.value],
                "registry": FEATURE_REGISTRY_VERSION,
                "features": self.snapshot_json(),
            }
        )


# Session closes handed to stored features (period_news_count: up to 5 sessions back).
SESSION_CLOSES = 6


def knowledge_time(basis: KnowledgeBasis, data_cutoff: datetime, now: datetime) -> datetime:
    return data_cutoff if basis is KnowledgeBasis.RECORDED else max(now, data_cutoff)


class DailyBarSource(Protocol):
    """Reads the daily bars of ``sessions`` as ``reads.daily_bars`` does (newest revision
    known at ``known_at``). The default queries the database; the ML dataset builder
    (Phase 5A) passes a source that serves the same rows from one preloaded read."""

    def __call__(
        self,
        session: Session,
        asset_id: uuid.UUID,
        sessions: Sequence[SessionDay],
        *,
        data_cutoff: datetime,
        known_at: datetime,
        series: BarSeries,
    ) -> Sequence[DailyBar]: ...


def query_daily_bars(
    session: Session,
    asset_id: uuid.UUID,
    sessions: Sequence[SessionDay],
    *,
    data_cutoff: datetime,
    known_at: datetime,
    series: BarSeries,
) -> Sequence[DailyBar]:
    return reads.daily_bars(
        session,
        [asset_id],
        provider=series.provider,
        feed=series.feed,
        adjustment=series.adjustment,
        sessions=sessions,
        calendar=series.calendar,
        as_of=data_cutoff,
        known_at=known_at,
    )


def _history(
    session: Session,
    asset_id: uuid.UUID,
    timeframe: str,
    periods: int,
    *,
    data_cutoff: datetime,
    known_at: datetime,
    series: BarSeries,
    source: DailyBarSource = query_daily_bars,
) -> BarHistory:
    sessions = completed_daily(
        series.calendar, data_cutoff, sessions_for(timeframe, periods), series.settle
    )
    rows = source(
        session, asset_id, sessions, data_cutoff=data_cutoff, known_at=known_at, series=series
    )
    daily: dict[date, Bar] = {}
    for row in rows:
        if row.quality_status != "VALID" or row.available_at is None:
            continue  # quarantined or incomplete bars are missing data, never inputs
        daily[row.session] = Bar(
            event_time=row.event_time,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            available_at=row.available_at,
        )
    if timeframe == DAILY:
        keys: list[Hashable] = [s.day for s in sessions]
        by_day: dict[Hashable, Bar] = {day: bar for day, bar in daily.items()}
        return BarHistory(timeframe, keys, by_day)
    weeks = completed_weeks(series.calendar, sessions)
    bars: dict[Hashable, Bar] = {}
    for key, days in weeks.items():
        if all(d in daily for d in days):  # a week missing a session is a missing period
            built = weekly_bars([daily[d] for d in days], days, {key})
            if built:
                bars[key] = built[0]
    return BarHistory(timeframe, list(weeks), bars)


def _benchmark(session: Session, symbol: str, load: Callable[[uuid.UUID], BarHistory]) -> Benchmark:
    """The benchmark asset by symbol (an active US equity or ETF) and its history."""
    found = session.scalars(
        select(Asset.id).where(
            Asset.symbol == symbol,
            Asset.status == AssetStatus.ACTIVE,
            Asset.asset_class.in_([AssetClass.EQUITY, AssetClass.ETF]),
        )
    ).all()
    if not found:
        return Benchmark(symbol, None, None, f"benchmark {symbol} is not an Atlas asset")
    if len(found) > 1:
        return Benchmark(symbol, None, None, f"benchmark symbol {symbol} is ambiguous")
    return Benchmark(symbol, found[0], load(found[0]))


def compute_features(
    session: Session,
    features: Sequence[ResolvedFeature],
    *,
    asset_id: uuid.UUID,
    timeframe: str,
    data_cutoff: datetime,
    known_at: datetime,
    basis: KnowledgeBasis,
    series: BarSeries,
    bar_source: DailyBarSource = query_daily_bars,
) -> FeatureInputs:
    if known_at < data_cutoff:
        raise ValueError("the knowledge time cannot precede the data cutoff")
    if basis is KnowledgeBasis.RECORDED and known_at != data_cutoff:
        raise ValueError("RECORDED inputs are those known at the cutoff itself")
    bar_features = [f for f in features if f.definition.source is FeatureSource.BARS]
    pair_features = [f for f in features if f.definition.source is FeatureSource.BENCHMARK]
    stored_features = [
        f
        for f in features
        if f.definition.source not in (FeatureSource.BARS, FeatureSource.BENCHMARK)
    ]
    for f in features:
        if f.timeframe != timeframe:
            raise ValueError(f"{f.key} is not on the evaluation timeframe {timeframe}")
    values: dict[str, FeatureValue] = {}
    last_event: datetime | None = None
    window = max((f.window for f in (*bar_features, *pair_features)), default=1)

    def load(target: uuid.UUID) -> BarHistory:
        return _history(
            session,
            target,
            timeframe,
            window,
            data_cutoff=data_cutoff,
            known_at=known_at,
            series=series,
            source=bar_source,
        )

    history = load(asset_id)
    if history.periods and history.periods[-1] in history.bars:
        last_event = history.bars[history.periods[-1]].event_time
    if bar_features:
        values.update(compute_bar_features(bar_features, history))
    if pair_features:
        benchmarks: dict[str, Benchmark] = {}
        for f in pair_features:
            symbol = str(f.param_map["benchmark"])
            if symbol not in benchmarks:
                benchmarks[symbol] = _benchmark(session, symbol, load)
            values[f.key] = compute_benchmark_feature(f, history, benchmarks[symbol], asset_id)
    if stored_features:
        newest = completed_daily(series.calendar, data_cutoff, SESSION_CLOSES, series.settle)
        closes = tuple(s.regular_close for s in newest)
        ctx = StoredContext(asset_id, data_cutoff, known_at, basis, closes)
        values.update(compute_stored_features(session, stored_features, ctx))
    return FeatureInputs(
        asset_id=asset_id,
        timeframe=timeframe,
        data_cutoff=data_cutoff,
        known_at=known_at,
        basis=basis,
        series=series,
        values=values,
        last_period_event_at=last_event,
    )


def store_snapshot(session: Session, inputs: FeatureInputs) -> uuid.UUID:
    """Insert the snapshot, or return the identical one stored before (content hash)."""
    sha = inputs.content_sha256()
    values = inputs.values.values()
    stmt = (
        insert(StrategyFeatureSnapshotRecord)
        .values(
            id=uuid.uuid4(),
            asset_id=inputs.asset_id,
            timeframe=inputs.timeframe,
            data_cutoff=inputs.data_cutoff,
            known_at=inputs.known_at,
            basis=inputs.basis,
            provider=inputs.series.provider,
            feed=inputs.series.feed,
            adjustment=inputs.series.adjustment,
            feature_registry_version=FEATURE_REGISTRY_VERSION,
            features=inputs.snapshot_json(),
            feature_count=len(inputs.values),
            valid_count=sum(1 for v in values if v.valid),
            inputs_available_at=inputs.inputs_available_at,
            last_period_event_at=inputs.last_period_event_at,
            content_sha256=sha,
        )
        .on_conflict_do_nothing(index_elements=["content_sha256"])
        .returning(StrategyFeatureSnapshotRecord.id)
    )
    inserted = session.scalar(stmt)
    if inserted is not None:
        return inserted
    existing = session.scalar(
        select(StrategyFeatureSnapshotRecord.id).where(
            StrategyFeatureSnapshotRecord.content_sha256 == sha
        )
    )
    if existing is None:
        raise RuntimeError("snapshot neither inserted nor found")
    return existing


__all__ = [
    "BarSeries",
    "DailyBarSource",
    "FeatureInputs",
    "compute_features",
    "knowledge_time",
    "query_daily_bars",
    "store_snapshot",
]
