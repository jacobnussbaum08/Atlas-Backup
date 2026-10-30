from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel

from app.data_engine.timeframes import (
    DAY_1,
    HOUR_1,
    INITIAL_TIMEFRAMES,
    MINUTE_1,
    MINUTE_5,
    MINUTE_15,
    Timeframe,
    TimeframeField,
    TimeframeUnit,
)


@pytest.mark.parametrize("text", ["1Min", "5Min", "15Min", "1Hour", "1Day", "1Week", "3Month"])
def test_round_trip(text):
    assert str(Timeframe.parse(text)) == text


@pytest.mark.parametrize("text", ["", "0Min", "1min", "1M", "1H", "5 Min", "1000Min", "1Year"])
def test_invalid_text_is_rejected(text):
    with pytest.raises(ValueError, match="invalid timeframe"):
        Timeframe.parse(text)


def test_initial_timeframes_match_phase_2_scope():
    assert [str(t) for t in INITIAL_TIMEFRAMES] == ["1Min", "5Min", "15Min", "1Hour", "1Day"]


def test_ordering_and_equality():
    assert MINUTE_1 < MINUTE_5 < MINUTE_15 < HOUR_1 < DAY_1
    assert Timeframe.parse("60Min") == Timeframe(amount=60, unit=TimeframeUnit.MINUTE)
    assert sorted([DAY_1, MINUTE_5, HOUR_1]) == [MINUTE_5, HOUR_1, DAY_1]


def test_fixed_duration_only_for_intraday():
    assert MINUTE_15.fixed_duration == timedelta(minutes=15)
    assert HOUR_1.fixed_duration == timedelta(hours=1)
    assert DAY_1.fixed_duration is None  # sessions, not 24 hours
    assert MINUTE_5.is_intraday
    assert not DAY_1.is_intraday


def test_field_type_accepts_text_and_serialises_back():
    class Holder(BaseModel):
        timeframe: TimeframeField

    holder = Holder(timeframe="15Min")
    assert holder.timeframe == MINUTE_15
    assert holder.model_dump(mode="json") == {"timeframe": "15Min"}
