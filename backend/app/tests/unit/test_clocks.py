from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.clocks import FixedClock, SystemClock, ensure_utc


def test_system_clock_returns_aware_utc():
    now = SystemClock().now()
    assert now.tzinfo is UTC


def test_fixed_clock_is_deterministic_and_advances():
    clock = FixedClock(datetime(2026, 3, 1, 9, 30, tzinfo=UTC))
    assert clock.now() == clock.now()
    clock.advance(timedelta(minutes=5))
    assert clock.now() == datetime(2026, 3, 1, 9, 35, tzinfo=UTC)


def test_fixed_clock_cannot_move_backwards():
    clock = FixedClock(datetime(2026, 3, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))


def test_non_utc_offsets_are_converted():
    eastern = timezone(timedelta(hours=-5))
    converted = ensure_utc(datetime(2026, 3, 1, 9, 30, tzinfo=eastern))
    assert converted == datetime(2026, 3, 1, 14, 30, tzinfo=UTC)
    assert converted.tzinfo is UTC


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 3, 1, 9, 30))  # noqa: DTZ001
    with pytest.raises(ValueError, match="naive"):
        FixedClock(datetime(2026, 3, 1))  # noqa: DTZ001
