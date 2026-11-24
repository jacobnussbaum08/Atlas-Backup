"""Measured quoted spreads for historical simulation (D-234).

Atlas holds no historical quotes for its backtests: the 8A replay assumes a half spread for
marketable fills, and its risk engine, with no fresh quote, assumes the 6B fallback spread,
which never exceeds the 50 bps limit. Live paper trading measures the spread from the
quote it streams, and on the IEX feed many names fail that check.

This module measures the spread from real historical quotes:

* :func:`summarize` reduces the two-sided quotes of one short window to a time-weighted
  median spread and keeps the quote observed at that median (an observed bid and ask, never
  a constructed one).
* :func:`sample` retrieves one window per session and feed through the Phase 2 Alpaca
  adapter (GET only) and stores each measurement as an ``asset_metric_values`` row
  (metric ``quoted_spread_bps``; ``feed`` says which feed). Nothing is stored for a
  session that has no two-sided quote: a NO_DATA row says so.
* :class:`PointInTimeSpreads` serves the newest measurement whose window ended by a given
  time and is not older than a maximum age.

The window is ``WINDOW_SECONDS`` starting ``WINDOW_BEFORE_CLOSE`` before the regular close
(early closes included). It is a sample, not the quote at a decision's instant: a
limitation stated wherever the measurements are used.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import statistics
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.asset_discovery.enums import MetricStatus
from app.backtesting.data import sessions_between
from app.database.models import Asset, AssetMetricValueRecord

METRIC = "quoted_spread_bps"
FEATURE_VERSION = "spread-sample-v1"
WINDOW_SECONDS = 60
WINDOW_BEFORE_CLOSE = timedelta(minutes=15)
WINDOW = f"{WINDOW_SECONDS}s@close-15m"
TIMEFRAME = "quote"
PROVIDER = "alpaca"
BPS = Decimal("10000")


@dataclass(frozen=True)
class QuoteObs:
    """One provider quote as the summary needs it."""

    at: datetime
    bid: Decimal | None
    ask: Decimal | None


@dataclass(frozen=True)
class SpreadSample:
    status: MetricStatus
    observations: int  # quotes in the window
    two_sided: int  # quotes with bid > 0 and ask >= bid
    median_bps: Decimal | None  # time weighted
    tick_median_bps: Decimal | None
    mean_bps: Decimal | None
    p90_bps: Decimal | None
    bid: Decimal | None  # the observed quote at the time-weighted median
    ask: Decimal | None
    quote_at: datetime | None

    def details(self) -> dict[str, Any]:
        return {
            "two_sided_quotes": self.two_sided,
            "tick_median_bps": None if self.tick_median_bps is None else str(self.tick_median_bps),
            "mean_bps": None if self.mean_bps is None else str(self.mean_bps),
            "p90_bps": None if self.p90_bps is None else str(self.p90_bps),
            "bid": None if self.bid is None else str(self.bid),
            "ask": None if self.ask is None else str(self.ask),
            "quote_at": None if self.quote_at is None else self.quote_at.isoformat(),
            "weighting": "time weighted from each quote to the next (last: to the window end)",
        }


def _bps(bid: Decimal, ask: Decimal) -> Decimal:
    return ((ask - bid) / ((ask + bid) / 2) * BPS).quantize(Decimal("0.0001"))


def summarize(quotes: Sequence[QuoteObs], end: datetime) -> SpreadSample:
    """Time-weighted median spread of the two-sided quotes in a window ending at ``end``.

    Each quote is weighted by how long it stood (to the next quote, the last to ``end``).
    One-sided, zero and crossed quotes are excluded (counted in ``observations`` only)."""
    ordered = sorted(quotes, key=lambda q: q.at)
    valid = [
        q
        for q in ordered
        if q.bid is not None and q.ask is not None and q.bid > 0 and q.ask >= q.bid
    ]
    if not valid:
        return SpreadSample(
            MetricStatus.NO_DATA, len(ordered), 0, None, None, None, None, None, None, None
        )
    weighted: list[tuple[Decimal, float, QuoteObs]] = []
    for i, q in enumerate(valid):
        nxt = valid[i + 1].at if i + 1 < len(valid) else end
        weight = max((nxt - q.at).total_seconds(), 0.0)
        assert q.bid is not None  # noqa: S101  # filtered above
        assert q.ask is not None  # noqa: S101  # filtered above
        weighted.append((_bps(q.bid, q.ask), weight, q))
    total = sum(w for _, w, _ in weighted)
    by_spread = sorted(weighted, key=lambda x: (x[0], x[2].at))
    if total <= 0:  # every quote at the same instant: fall back to the tick median
        chosen = by_spread[len(by_spread) // 2]
    else:
        running = 0.0
        chosen = by_spread[-1]
        for item in by_spread:
            running += item[1]
            if running >= total / 2:
                chosen = item
                break
    ticks = sorted(s for s, _, _ in weighted)
    p90 = ticks[min(len(ticks) - 1, int(0.9 * len(ticks)))]
    return SpreadSample(
        MetricStatus.OK,
        len(ordered),
        len(valid),
        chosen[0],
        Decimal(str(statistics.median(ticks))).quantize(Decimal("0.0001")),
        (sum(ticks, Decimal(0)) / len(ticks)).quantize(Decimal("0.0001")),
        p90,
        chosen[2].bid,
        chosen[2].ask,
        chosen[2].at,
    )


@dataclass(frozen=True)
class SampleWindow:
    """Where in a session a sample is taken. Each kind is stored under its own
    ``feature_version`` and ``window`` label, so the kinds never mix."""

    key: str
    feature_version: str
    label: str
    anchor: str  # "close" or "open"
    offset: timedelta  # added to the anchor (negative: before it)

    def bounds(self, day: date) -> tuple[datetime, datetime] | None:
        found = sessions_between(day, day)
        if not found:
            return None
        base = found[0].regular_close if self.anchor == "close" else found[0].regular_open
        start = base + self.offset
        return start, start + timedelta(seconds=WINDOW_SECONDS)


# 15 minutes before the close: the quote standing in for the one live risk reads (D-234).
CLOSE_WINDOW = SampleWindow("close", FEATURE_VERSION, WINDOW, "close", -WINDOW_BEFORE_CLOSE)
# The first full minute after the open (09:31-09:32 ET on a normal day): the cost of an
# opening entry fill (E1). Never an input to a decision, which is made the evening before.
OPEN_WINDOW = SampleWindow(
    "open", "spread-sample-open-v1", f"{WINDOW_SECONDS}s@open+1m", "open", timedelta(minutes=1)
)
WINDOWS = {w.key: w for w in (CLOSE_WINDOW, OPEN_WINDOW)}


def window_for(day: date, kind: SampleWindow = CLOSE_WINDOW) -> tuple[datetime, datetime] | None:
    """The sample window of a session, or None when ``day`` is not a session."""
    return kind.bounds(day)


# ---------------------------------------------------------------------- point in time
@dataclass(frozen=True)
class SpreadPoint:
    as_of: datetime
    status: MetricStatus
    bps: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    quote_at: datetime | None
    metric_id: int


class PointInTimeSpreads:
    """Stored measurements of one feed, served point in time."""

    def __init__(self, feed: str, rows: dict[uuid.UUID, list[SpreadPoint]]):
        self.feed = feed
        self._rows = rows
        self._times = {a: [p.as_of for p in pts] for a, pts in rows.items()}
        self.used = 0
        self.stale_or_missing = 0
        self.no_two_sided = 0

    @classmethod
    def load(
        cls,
        session: Session,
        asset_ids: Iterable[uuid.UUID],
        *,
        feed: str,
        known_at: datetime,
        window: SampleWindow = CLOSE_WINDOW,
    ) -> PointInTimeSpreads:
        m = AssetMetricValueRecord
        rows: dict[uuid.UUID, list[SpreadPoint]] = {}
        for rec in session.scalars(
            select(m)
            .where(
                m.asset_id.in_(list(asset_ids)),
                m.metric == METRIC,
                m.feature_version == window.feature_version,
                m.feed == feed,
                m.created_at <= known_at,
            )
            .order_by(m.asset_id, m.as_of, m.id)
        ):
            d = rec.details or {}
            rows.setdefault(rec.asset_id, []).append(
                SpreadPoint(
                    as_of=rec.as_of,
                    status=MetricStatus(rec.status),
                    bps=rec.value,
                    bid=None if d.get("bid") is None else Decimal(d["bid"]),
                    ask=None if d.get("ask") is None else Decimal(d["ask"]),
                    quote_at=None
                    if d.get("quote_at") is None
                    else datetime.fromisoformat(d["quote_at"]),
                    metric_id=rec.id,
                )
            )
        return cls(feed, rows)

    def at(self, asset_id: uuid.UUID, t: datetime, max_age: timedelta) -> SpreadPoint | None:
        """The newest measurement with ``as_of <= t`` and no older than ``max_age`` (an OK
        one, or the NO_DATA row saying the window had no two-sided quote); else None."""
        times = self._times.get(asset_id)
        if not times:
            self.stale_or_missing += 1
            return None
        i = bisect.bisect_right(times, t) - 1
        if i < 0 or t - times[i] > max_age:
            self.stale_or_missing += 1
            return None
        point = self._rows[asset_id][i]
        if point.status is not MetricStatus.OK:
            self.no_two_sided += 1
            return point
        self.used += 1
        return point

    def coverage(self) -> dict[str, int]:
        return {
            "measured": self.used,
            "window_without_two_sided_quote": self.no_two_sided,
            "no_measurement_within_max_age": self.stale_or_missing,
        }


# ---------------------------------------------------------------------- sampling
async def sample(
    session_factory: Any,
    *,
    feed: str,
    symbols: Sequence[str],
    start: date,
    end: date,
    progress: Any = None,
    window: SampleWindow = CLOSE_WINDOW,
    asset_ids: dict[str, uuid.UUID] | None = None,
) -> dict[str, int]:
    """Measure one window per session in [start, end] for ``symbols`` on ``feed`` and
    store the measurements (sessions already measured for every symbol are skipped).

    ``asset_ids`` names the asset row of each symbol explicitly (D-239: a research member
    whose ticker was later reused has its own row beside the current holder's)."""
    from app.data_engine.capabilities import DataType
    from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
    from app.data_engine.providers.alpaca.config import AlpacaFeed, get_alpaca_settings
    from app.data_engine.providers.base import HistoricalObservationsRequest

    settings = get_alpaca_settings().model_copy(update={"data_feed": AlpacaFeed(feed)})
    provider = AlpacaMarketDataProvider(settings)
    with session_factory() as session:
        if asset_ids is not None:
            assets = {s: asset_ids[s] for s in symbols if s in asset_ids}
        else:
            assets = {
                a.symbol: a.id
                for a in session.scalars(select(Asset).where(Asset.symbol.in_(list(symbols))))
            }
        m = AssetMetricValueRecord
        done: dict[datetime, set[uuid.UUID]] = {}
        for as_of, asset_id in session.execute(
            select(m.as_of, m.asset_id).where(
                m.metric == METRIC, m.feature_version == window.feature_version, m.feed == feed
            )
        ):
            done.setdefault(as_of, set()).add(asset_id)
    missing = sorted(set(symbols) - set(assets))
    if missing:
        raise ValueError(f"unknown symbols: {', '.join(missing)}")
    counts = {"sessions": 0, "skipped": 0, "ok": 0, "no_data": 0, "pages": 0}
    for day in sessions_between(start, end):
        bounds = window_for(day.day, window)
        assert bounds is not None  # noqa: S101  # a session
        w_start, w_end = bounds
        if done.get(w_end, set()) >= set(assets.values()):
            counts["skipped"] += 1
            continue
        request = HistoricalObservationsRequest(
            provider_symbols=tuple(sorted(assets)),
            data_type=DataType.QUOTE,
            start=w_start,
            end=w_end,
            page_size=10000,
        )
        by_symbol: dict[str, list[QuoteObs]] = {s: [] for s in assets}
        async for page in provider.fetch_observations(request):
            counts["pages"] += 1
            for q in page.quotes:
                if q.provider_symbol in by_symbol and w_start <= q.event_time <= w_end:
                    by_symbol[q.provider_symbol].append(
                        QuoteObs(q.event_time, q.bid_price, q.ask_price)
                    )
        rows = []
        now = datetime.now(UTC)
        for symbol, quotes in sorted(by_symbol.items()):
            s = summarize(quotes, w_end)
            counts["ok" if s.status is MetricStatus.OK else "no_data"] += 1
            rows.append(
                AssetMetricValueRecord(
                    asset_id=assets[symbol],
                    metric=METRIC,
                    window=window.label,
                    timeframe=TIMEFRAME,
                    provider=PROVIDER,
                    feed=feed,
                    feature_version=window.feature_version,
                    as_of=w_end,
                    source_start=w_start,
                    source_end=w_end,
                    observations=s.observations,
                    expected_observations=0,
                    value=s.median_bps,
                    status=s.status.value,
                    details=s.details(),
                    created_at=now,
                )
            )
        with session_factory() as session, session.begin():
            session.add_all(rows)
        counts["sessions"] += 1
        if progress is not None:
            progress(day.day, counts)
    return counts


def main(argv: Sequence[str] | None = None) -> None:
    from app.core.config import get_settings
    from app.database.sessions import build_engine, build_session_factory

    parser = argparse.ArgumentParser(
        prog="python -m app.backtesting.spreads",
        description="Measure historical quoted spreads (GET-only provider requests).",
    )
    parser.add_argument("--feed", required=True, choices=("iex", "sip"))
    parser.add_argument("--symbols", required=True, help="comma-separated")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--window", default="close", choices=sorted(WINDOWS))
    args = parser.parse_args(argv)
    engine = build_engine(get_settings())
    factory = build_session_factory(engine)

    def report(day: date, counts: dict[str, int]) -> None:
        if counts["sessions"] % 50 == 0:
            print(day.isoformat(), counts, flush=True)

    counts = asyncio.run(
        sample(
            factory,
            feed=args.feed,
            symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()],
            start=args.start,
            end=args.end,
            progress=report,
            window=WINDOWS[args.window],
        )
    )
    print("done", counts, flush=True)


if __name__ == "__main__":
    main()


__all__ = [
    "CLOSE_WINDOW",
    "FEATURE_VERSION",
    "METRIC",
    "OPEN_WINDOW",
    "WINDOW",
    "WINDOWS",
    "PointInTimeSpreads",
    "QuoteObs",
    "SampleWindow",
    "SpreadPoint",
    "SpreadSample",
    "sample",
    "summarize",
    "window_for",
]
