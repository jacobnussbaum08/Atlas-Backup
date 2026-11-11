"""The historical dataset of one backtest, loaded once and served point in time.

Everything a replay reads about the market comes from here, so the look-ahead rules live in
one place:

* **Knowledge time.** Only rows ingested (and available) by the run's ``data_known_at`` are
  loaded, so data ingested after a run was created can never change its replay. Bar
  revisions: the newest revision known by then (RECONSTRUCTED basis, documented).
* **Bar availability.** A daily bar of session *d* is usable from *d*'s regular close plus
  the settle delay. A REST backfill's retrieval time is never read as the time the bar
  first became known (spec 18.1, prompt 2): availability is derived from the calendar.
* **Completed candles only.** :class:`PointInTimeBars` serves
  :func:`app.strategies.inputs.compute_features` the sessions it asks for, which that
  function has already limited to sessions completed and settled by the cutoff, and it
  refuses any other knowledge time or a session after the newest completed one.
* **Corporate actions.** Split factors are inferred from the provider's SPLIT-adjusted and
  RAW series of the same session: ``C(d) = raw_close(d) / split_close(d)`` is the product of
  the split ratios that took effect after *d*. A decision whose newest completed session is
  *T* sees ``split(d) x C(T)`` for every earlier *d*: history rescaled to the level it
  traded at on *T*, exactly what a split-adjusted series retrieved on *T* would show. No
  split after *T* enters it. A change of the factor between consecutive sessions is a split
  that took effect at the later session's open (:class:`SplitEvent`).
* **Reference data.** Provider reference versions (tradable, shortable, lot size) observed
  by the decision time; Atlas observed most assets only recently, so an older decision uses
  the earliest version Atlas has, labelled ``RECONSTRUCTED_REFERENCE``.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.asset_discovery.enums import UniverseLayer
from app.asset_discovery.metrics import DailyBar
from app.asset_discovery.sessions import (
    CALENDARS,
    ExchangeCalendar,
    SessionDay,
    local_date,
    previous_trading_day,
    trading_day,
)
from app.data_engine.enums import DataQualityStatus, PriceAdjustment
from app.database.models import (
    AssetReferenceVersionRecord,
    InstrumentSpecRecord,
    MarketBarRecord,
    UniverseMembershipEventRecord,
)
from app.database.models.risk import RiskAssetClassificationRecord
from app.feature_engine.registry import DAILY
from app.strategies.inputs import BarSeries

CALENDAR: ExchangeCalendar = CALENDARS["XNYS"]
ONE = Decimal(1)
# A factor change smaller than this between two sessions is rounding, not a split.
SPLIT_THRESHOLD = Decimal("0.02")


def sessions_between(first: date, last: date) -> list[SessionDay]:
    out: list[SessionDay] = []
    day = first
    while day <= last:
        found = trading_day(CALENDAR, day)
        if found is not None:
            out.append(found)
        day += timedelta(days=1)
    return out


def sessions_back(day: date, count: int) -> date:
    for _ in range(count):
        day = previous_trading_day(CALENDAR, day).day
    return day


@dataclass(frozen=True)
class DayBar:
    """One stored daily bar (exact decimals), with the replay's availability."""

    bar_id: int
    asset_id: uuid.UUID
    session: date
    start: datetime  # the session's regular open
    end: datetime  # the session's regular close
    available_at: datetime  # end + settle (policy; never the retrieval time)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    vwap: Decimal | None
    valid: bool
    revision: int


@dataclass(frozen=True)
class SplitEvent:
    asset_id: uuid.UUID
    session: date  # the split took effect at this session's open
    effective_at: datetime
    # New shares per old share (2 for a 2-for-1 split, 0.2 for a 1-for-5 reverse split).
    ratio: Decimal
    factor_before: Decimal
    factor_after: Decimal


@dataclass(frozen=True)
class Reference:
    """Provider reference data as the replay uses it at one decision time."""

    version_id: int | None
    version: int | None
    observed_at: datetime | None
    basis: str  # POINT_IN_TIME | RECONSTRUCTED_REFERENCE | NONE
    status: str | None = None
    tradable: bool | None = None
    shortable: bool | None = None
    easy_to_borrow: bool | None = None
    margin_requirement_short: Decimal | None = None
    price_increment: Decimal | None = None
    quantity_increment: Decimal | None = None
    min_quantity: Decimal | None = None
    lot_basis: str | None = None


@dataclass(frozen=True)
class Classification:
    sector: str | None
    cluster: str | None
    record_id: int | None


@dataclass
class HistoricalDataset:
    """Preloaded rows of one run; every accessor is point in time."""

    known_at: datetime
    settle: timedelta
    provider: str
    feed: str
    first_session: date  # first session loaded (lookback included)
    raw: dict[uuid.UUID, dict[date, DayBar]] = field(default_factory=dict)
    split: dict[uuid.UUID, dict[date, DayBar]] = field(default_factory=dict)
    factors: dict[uuid.UUID, dict[date, Decimal]] = field(default_factory=dict)
    splits: dict[uuid.UUID, list[SplitEvent]] = field(default_factory=dict)
    references: dict[uuid.UUID, list[AssetReferenceVersionRecord]] = field(default_factory=dict)
    specs: dict[uuid.UUID, list[InstrumentSpecRecord]] = field(default_factory=dict)
    classifications: dict[uuid.UUID, list[RiskAssetClassificationRecord]] = field(
        default_factory=dict
    )
    fingerprint_parts: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ bars
    def bar(self, asset_id: uuid.UUID, day: date) -> DayBar | None:
        """The RAW bar of a session (execution and valuation prices)."""
        return self.raw.get(asset_id, {}).get(day)

    def last_raw_on_or_before(self, asset_id: uuid.UUID, day: date) -> DayBar | None:
        bars = self.raw.get(asset_id, {})
        days = sorted(d for d, b in bars.items() if d <= day and b.valid)
        return bars[days[-1]] if days else None

    def factor(self, asset_id: uuid.UUID, day: date) -> Decimal | None:
        return self.factors.get(asset_id, {}).get(day)

    def splits_on(self, day: date) -> list[SplitEvent]:
        return sorted(
            (e for events in self.splits.values() for e in events if e.session == day),
            key=lambda e: str(e.asset_id),
        )

    def raw_daily_bars(self, asset_id: uuid.UUID, sessions: Sequence[SessionDay]) -> list[DailyBar]:
        return [_daily(b, ONE) for s in sessions if (b := self.bar(asset_id, s.day)) is not None]

    # ------------------------------------------------------------------ reference data
    def reference(self, asset_id: uuid.UUID, at: datetime) -> Reference:
        rows = self.references.get(asset_id, [])
        if not rows:
            return Reference(None, None, None, "NONE")
        known = [r for r in rows if r.observed_at <= at]
        row, basis = (known[-1], "POINT_IN_TIME") if known else (rows[0], "RECONSTRUCTED_REFERENCE")
        increment, minimum, lot_basis = _lot(row)
        return Reference(
            version_id=row.id,
            version=row.version,
            observed_at=row.observed_at,
            basis=basis,
            status=row.status.value,
            tradable=row.tradable,
            shortable=row.shortable,
            easy_to_borrow=row.easy_to_borrow,
            margin_requirement_short=row.margin_requirement_short,
            price_increment=row.price_increment,
            quantity_increment=increment,
            min_quantity=minimum,
            lot_basis=lot_basis,
        )

    def instrument_spec(self, asset_id: uuid.UUID, at: datetime) -> InstrumentSpecRecord | None:
        found = [s for s in self.specs.get(asset_id, []) if s.valid_from <= at]
        return found[-1] if found else None

    def classification(self, asset_id: uuid.UUID, at: datetime) -> Classification:
        found = [c for c in self.classifications.get(asset_id, []) if c.valid_from <= at]
        if not found:
            return Classification(None, None, None)
        row = found[-1]
        return Classification(row.sector, row.cluster, row.id)

    def sha256(self) -> str:
        digest = hashlib.sha256()
        for part in sorted(self.fingerprint_parts):
            digest.update(part.encode())
            digest.update(b"\n")
        return digest.hexdigest()

    @property
    def bar_count(self) -> int:
        return sum(len(v) for v in self.raw.values()) + sum(len(v) for v in self.split.values())


def _lot(row: AssetReferenceVersionRecord) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Lot size from a provider reference version, by the rules of 6A
    ``spec_from_reference`` (never guessed)."""
    if row.min_trade_increment is not None and row.min_trade_increment > 0:
        increment, basis = row.min_trade_increment, "min_trade_increment"
    elif row.fractionable is False:
        increment, basis = ONE, "not fractionable: whole units"
    else:
        return None, None, None
    minimum = row.min_order_size if row.min_order_size is not None else increment
    return increment, minimum, basis


def _daily(bar: DayBar, factor: Decimal) -> DailyBar:
    """A bar as the feature engine reads it (floats), prices x factor, volume / factor."""

    def px(value: Decimal) -> float:
        return float(value * factor)

    return DailyBar(
        asset_id=bar.asset_id,
        session=bar.session,
        event_time=bar.start,
        open=px(bar.open),
        high=px(bar.high),
        low=px(bar.low),
        close=px(bar.close),
        volume=None if bar.volume is None else float(bar.volume / factor),
        vwap=None if bar.vwap is None else px(bar.vwap),
        quality_status="VALID" if bar.valid else "QUARANTINED",
        available_at=bar.available_at,
    )


def snap_ratio(value: Decimal) -> Decimal:
    """A measured split ratio snapped to the nearest simple fraction (rounding noise)."""
    frac = Fraction(value).limit_denominator(1000)
    snapped = Decimal(frac.numerator) / Decimal(frac.denominator)
    if value != 0 and abs(snapped - value) / value < Decimal("0.005"):
        return snapped.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN).normalize()
    return value


class PointInTimeBars:
    """A :class:`~app.strategies.inputs.DailyBarSource` over the preloaded dataset.

    POINT_IN_TIME_SPLIT: split-adjusted history rescaled by the split factor of the newest
    session requested (the newest completed session at the cutoff). RAW: unadjusted bars.
    It serves one knowledge time and one series; any other request is refused, so it can
    never widen what a feature sees."""

    def __init__(self, dataset: HistoricalDataset, series: BarSeries, *, split_adjusted: bool):
        self.dataset = dataset
        self.series = series
        self.split_adjusted = split_adjusted
        self.requests = 0

    def __call__(
        self,
        session: Session,
        asset_id: uuid.UUID,
        sessions: Sequence[SessionDay],
        *,
        data_cutoff: datetime,
        known_at: datetime,
        series: BarSeries,
    ) -> Sequence[DailyBar]:
        if known_at != self.dataset.known_at or series != self.series:
            raise ValueError("the backtest bar source serves one knowledge time and series only")
        if not sessions:
            return []
        newest = sessions[-1]
        if newest.regular_close + self.dataset.settle > data_cutoff:
            raise ValueError(f"session {newest.day} was not completed by the cutoff")
        if sessions[0].day < self.dataset.first_session:
            raise ValueError(f"session {sessions[0].day} is before the loaded range")
        self.requests += 1
        if not self.split_adjusted:
            return self.dataset.raw_daily_bars(asset_id, sessions)
        factor = self.dataset.factor(asset_id, newest.day)
        if factor is None:
            # No RAW and SPLIT bar on the newest session: the split factor at the decision
            # is unknown, so no adjusted history is served (missing data, never guessed).
            return []
        bars = self.dataset.split.get(asset_id, {})
        return [
            _daily(b, factor)
            for s in sessions
            if (b := bars.get(s.day)) is not None and b.available_at <= data_cutoff
        ]


# ---------------------------------------------------------------------- loading
def _load_bars(
    session: Session,
    asset_ids: Sequence[uuid.UUID],
    *,
    provider: str,
    feed: str,
    adjustment: PriceAdjustment,
    first: date,
    last: date,
    known_at: datetime,
    settle: timedelta,
) -> tuple[dict[uuid.UUID, dict[date, DayBar]], list[str]]:
    b = MarketBarRecord
    start = datetime.combine(first - timedelta(days=1), datetime.min.time()).replace(
        tzinfo=known_at.tzinfo
    )
    end_session = trading_day(CALENDAR, last) or previous_trading_day(CALENDAR, last)
    stmt = (
        select(b)
        .where(
            b.asset_id.in_(list(asset_ids)),
            b.timeframe == DAILY,
            b.provider == provider,
            b.feed == feed,
            b.adjustment == adjustment,
            b.event_time >= start,
            b.event_time <= end_session.regular_close,
            b.ingested_at <= known_at,
            b.available_at <= known_at,
        )
        .ext(distinct_on(b.asset_id, b.event_time))
        .order_by(b.asset_id, b.event_time, b.revision.desc())
    )
    out: dict[uuid.UUID, dict[date, DayBar]] = defaultdict(dict)
    parts: list[str] = []
    for row in session.scalars(stmt):
        day = local_date(CALENDAR, row.event_time)
        sday = trading_day(CALENDAR, day)
        if sday is None or day < first or day > last:
            continue
        valid = (
            row.quality_status is DataQualityStatus.VALID
            and row.is_complete
            and row.low > 0
            and row.high >= row.low
        )
        out[row.asset_id][day] = DayBar(
            bar_id=row.id,
            asset_id=row.asset_id,
            session=day,
            start=sday.regular_open,
            end=sday.regular_close,
            available_at=sday.regular_close + settle,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            vwap=row.vwap,
            valid=valid,
            revision=row.revision,
        )
        parts.append(f"bar:{adjustment.value}:{row.asset_id}:{day}:{row.id}:{row.revision}")
    return dict(out), parts


def _factors(
    raw: dict[date, DayBar], split: dict[date, DayBar]
) -> tuple[dict[date, Decimal], list[tuple[date, Decimal, Decimal]]]:
    """C(d) per session with both bars valid, and (session, factor before, factor after)
    at each change larger than the threshold."""
    factors: dict[date, Decimal] = {}
    for day, r in raw.items():
        s = split.get(day)
        if s is None or not r.valid or not s.valid or s.close <= 0:
            continue
        measured = r.close / s.close
        factors[day] = ONE if abs(measured - ONE) < Decimal("0.001") else snap_ratio(measured)
    changes: list[tuple[date, Decimal, Decimal]] = []
    days = sorted(factors)
    for before, after in itertools.pairwise(days):
        a, b = factors[before], factors[after]
        if abs(a / b - ONE) > SPLIT_THRESHOLD:
            changes.append((after, a, b))
    return factors, changes


def load_dataset(
    session: Session,
    asset_ids: Iterable[uuid.UUID],
    *,
    provider: str,
    feed: str,
    first: date,
    last: date,
    known_at: datetime,
    settle: timedelta,
    split_adjusted: bool,
) -> HistoricalDataset:
    ids = sorted(set(asset_ids), key=str)
    dataset = HistoricalDataset(
        known_at=known_at, settle=settle, provider=provider, feed=feed, first_session=first
    )
    if not ids:
        return dataset
    dataset.raw, parts = _load_bars(
        session,
        ids,
        provider=provider,
        feed=feed,
        adjustment=PriceAdjustment.RAW,
        first=first,
        last=last,
        known_at=known_at,
        settle=settle,
    )
    dataset.fingerprint_parts.extend(parts)
    if split_adjusted:
        dataset.split, parts = _load_bars(
            session,
            ids,
            provider=provider,
            feed=feed,
            adjustment=PriceAdjustment.SPLIT,
            first=first,
            last=last,
            known_at=known_at,
            settle=settle,
        )
        dataset.fingerprint_parts.extend(parts)
    for asset_id in ids:
        factors, changes = _factors(dataset.raw.get(asset_id, {}), dataset.split.get(asset_id, {}))
        dataset.factors[asset_id] = factors
        events: list[SplitEvent] = []
        for day, before, after in changes:
            sday = trading_day(CALENDAR, day)
            assert sday is not None  # noqa: S101  # factors exist on sessions only
            events.append(
                SplitEvent(
                    asset_id=asset_id,
                    session=day,
                    effective_at=sday.regular_open,
                    ratio=snap_ratio(before / after),
                    factor_before=before,
                    factor_after=after,
                )
            )
        dataset.splits[asset_id] = events
    v = AssetReferenceVersionRecord
    for row in session.scalars(
        select(v)
        .where(v.asset_id.in_(ids), v.provider == provider, v.observed_at <= known_at)
        .order_by(v.asset_id, v.observed_at, v.version)
    ):
        dataset.references.setdefault(row.asset_id, []).append(row)
        dataset.fingerprint_parts.append(f"ref:{row.id}")
    sp = InstrumentSpecRecord
    for spec in session.scalars(
        select(sp)
        .where(sp.asset_id.in_(ids), sp.created_at <= known_at)
        .order_by(sp.asset_id, sp.valid_from)
    ):
        dataset.specs.setdefault(spec.asset_id, []).append(spec)
        dataset.fingerprint_parts.append(f"spec:{spec.id}")
    c = RiskAssetClassificationRecord
    for crow in session.scalars(
        select(c)
        .where(c.asset_id.in_(ids), c.created_at <= known_at)
        .order_by(c.asset_id, c.valid_from)
    ):
        dataset.classifications.setdefault(crow.asset_id, []).append(crow)
        dataset.fingerprint_parts.append(f"class:{crow.id}")
    detached: list[object] = [
        *(r for rows in dataset.references.values() for r in rows),
        *(r for rows in dataset.specs.values() for r in rows),
        *(r for rows in dataset.classifications.values() for r in rows),
    ]
    for item in detached:
        session.expunge(item)
    return dataset


def layer_candidates(
    session: Session, layer: UniverseLayer, *, until: datetime, known_at: datetime
) -> list[uuid.UUID]:
    """Every asset with a membership event in ``layer`` effective by ``until`` and recorded
    by ``known_at``: the assets a point-in-time universe may contain during the run."""
    e = UniverseMembershipEventRecord
    rows = session.scalars(
        select(e.asset_id)
        .where(e.layer == layer, e.effective_at <= until, e.recorded_at <= known_at)
        .distinct()
    ).all()
    return sorted(rows, key=str)


def index_of(days: Sequence[date], day: date) -> int:
    return bisect.bisect_left(days, day)


__all__ = [
    "CALENDAR",
    "Classification",
    "DayBar",
    "HistoricalDataset",
    "PointInTimeBars",
    "Reference",
    "SplitEvent",
    "layer_candidates",
    "load_dataset",
    "sessions_back",
    "sessions_between",
    "snap_ratio",
]
