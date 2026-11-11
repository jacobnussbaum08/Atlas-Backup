"""Phase 8A: point-in-time bars, split inference and look-ahead refusals (TEST FIXTURE bars)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.backtesting.data import (
    CALENDAR,
    DayBar,
    HistoricalDataset,
    PointInTimeBars,
    _factors,
    sessions_between,
    snap_ratio,
)
from app.data_engine.enums import PriceAdjustment
from app.strategies.inputs import BarSeries

D = Decimal
SETTLE = timedelta(hours=1)
ASSET = uuid.uuid4()
KNOWN = datetime(2026, 11, 1, tzinfo=UTC)
DAYS = sessions_between(date(2026, 9, 1), date(2026, 10, 30))
SPLIT_DAY = DAYS[30].day  # a 2-for-1 split takes effect at this session's open


def _bar(day_index: int, close: Decimal, bar_id: int) -> DayBar:
    s = DAYS[day_index]
    return DayBar(
        bar_id=bar_id,
        asset_id=ASSET,
        session=s.day,
        start=s.regular_open,
        end=s.regular_close,
        available_at=s.regular_close + SETTLE,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=D(1000),
        vwap=None,
        valid=True,
        revision=0,
    )


def _dataset() -> HistoricalDataset:
    """FIXTURE: RAW trades at 200 until the split, then at ~100; the provider's SPLIT
    series (retrieved after the split) shows ~100 throughout."""
    raw, split = {}, {}
    for i, _s in enumerate(DAYS):
        price = D(200) + i if i < 30 else D(100) + D(i) / 2
        raw[DAYS[i].day] = _bar(i, price, i)
        split[DAYS[i].day] = _bar(i, price / 2 if i < 30 else price, 1000 + i)
    ds = HistoricalDataset(KNOWN, SETTLE, "alpaca", "iex", DAYS[0].day)
    ds.raw[ASSET], ds.split[ASSET] = raw, split
    factors, _changes = _factors(raw, split)
    ds.factors[ASSET] = factors
    return ds


NO_SESSION = cast(Session, None)  # the source never touches the database
SERIES = BarSeries("alpaca", "iex", PriceAdjustment.SPLIT, CALENDAR, SETTLE)


def _cutoff(index: int) -> datetime:
    return DAYS[index].regular_close + SETTLE


def test_split_factors_and_events_are_inferred_from_the_two_series() -> None:
    ds = _dataset()
    factors, changes = _factors(ds.raw[ASSET], ds.split[ASSET])
    assert factors[DAYS[0].day] == D(2)
    assert factors[DAYS[40].day] == D(1)
    assert len(changes) == 1
    day, before, after = changes[0]
    assert day == SPLIT_DAY
    assert snap_ratio(before / after) == D(2)


def test_history_before_a_split_is_served_at_the_level_known_then() -> None:
    ds = _dataset()
    source = PointInTimeBars(ds, SERIES, split_adjusted=True)
    before = DAYS[20:30]  # decisions before the split see raw levels (~200), not ~100
    served = source(
        NO_SESSION, ASSET, before, data_cutoff=_cutoff(29), known_at=KNOWN, series=SERIES
    )
    assert [round(b.close, 6) for b in served] == [float(D(200) + i) for i in range(20, 30)]
    after = DAYS[25:35]  # after the split: history rescaled to the post-split level
    served = source(
        NO_SESSION, ASSET, after, data_cutoff=_cutoff(34), known_at=KNOWN, series=SERIES
    )
    assert round(served[0].close, 6) == float((D(200) + 25) / 2)
    assert round(served[-1].close, 6) == float(D(100) + D(34) / 2)


def test_raw_features_ignore_the_split_series_entirely() -> None:
    ds = _dataset()
    source = PointInTimeBars(ds, SERIES, split_adjusted=False)
    served = source(
        NO_SESSION, ASSET, DAYS[25:35], data_cutoff=_cutoff(34), known_at=KNOWN, series=SERIES
    )
    assert round(served[0].close, 6) == 225.0


def test_the_source_refuses_sessions_not_completed_by_the_cutoff() -> None:
    ds = _dataset()
    source = PointInTimeBars(ds, SERIES, split_adjusted=True)
    with pytest.raises(ValueError, match="not completed"):
        source(
            NO_SESSION, ASSET, DAYS[20:31], data_cutoff=_cutoff(29), known_at=KNOWN, series=SERIES
        )
    # The close itself is not enough: the settle delay must also have passed.
    with pytest.raises(ValueError, match="not completed"):
        source(
            NO_SESSION,
            ASSET,
            DAYS[20:30],
            data_cutoff=DAYS[29].regular_close,
            known_at=KNOWN,
            series=SERIES,
        )


def test_the_source_refuses_another_knowledge_time_or_series() -> None:
    ds = _dataset()
    source = PointInTimeBars(ds, SERIES, split_adjusted=True)
    with pytest.raises(ValueError, match="one knowledge time"):
        source(
            NO_SESSION,
            ASSET,
            DAYS[:5],
            data_cutoff=_cutoff(4),
            known_at=KNOWN + timedelta(1),
            series=SERIES,
        )


def test_without_a_raw_bar_on_the_decision_session_no_adjusted_history_is_served() -> None:
    ds = _dataset()
    del ds.factors[ASSET][DAYS[29].day]
    source = PointInTimeBars(ds, SERIES, split_adjusted=True)
    served = source(
        NO_SESSION, ASSET, DAYS[20:30], data_cutoff=_cutoff(29), known_at=KNOWN, series=SERIES
    )
    assert served == []


def test_reference_data_is_point_in_time_else_the_earliest_known_version() -> None:
    class Ref:
        def __init__(self, observed: datetime, version: int, tradable: bool) -> None:
            self.id, self.version, self.observed_at = version, version, observed
            self.status = type("S", (), {"value": "ACTIVE"})()
            self.tradable, self.shortable, self.easy_to_borrow = tradable, True, True
            self.margin_requirement_short = None
            self.price_increment = None
            self.min_trade_increment, self.fractionable, self.min_order_size = None, False, None

    ds = _dataset()
    ds.references[ASSET] = [
        Ref(datetime(2026, 10, 10, tzinfo=UTC), 1, True),  # type: ignore[list-item]
        Ref(datetime(2026, 10, 20, tzinfo=UTC), 2, False),  # type: ignore[list-item]
    ]
    early = ds.reference(ASSET, datetime(2026, 9, 15, tzinfo=UTC))
    assert early.basis == "RECONSTRUCTED_REFERENCE"
    assert early.version == 1
    mid = ds.reference(ASSET, datetime(2026, 10, 15, tzinfo=UTC))
    assert mid.basis == "POINT_IN_TIME"
    assert mid.version == 1
    assert mid.tradable is True
    late = ds.reference(ASSET, datetime(2026, 10, 25, tzinfo=UTC))
    assert late.version == 2
    assert late.tradable is False
    assert mid.quantity_increment == D(1)  # not fractionable: whole units (6A rule)


def test_snap_ratio_removes_rounding_noise_only() -> None:
    assert snap_ratio(D("0.33329338")) == D(1) / D(3) or abs(
        snap_ratio(D("0.33329338")) - D(1) / D(3)
    ) < D("1e-11")
    assert snap_ratio(D("4.0002")) == D(4)
    assert snap_ratio(D("1.37")) == D("1.37")
