"""Injectable UTC clocks.

All Atlas code obtains the current time through a :class:`Clock` so that tests
can use deterministic time (spec section 25.1) and every timestamp is UTC
(spec section 7.3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A manually controlled clock for tests and simulations."""

    def __init__(self, at: datetime) -> None:
        self._now = ensure_utc(at)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("FixedClock cannot move backwards")
        self._now += delta

    def set(self, at: datetime) -> None:
        self._now = ensure_utc(at)


def ensure_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC. Naive datetimes are rejected, never assumed."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime rejected: all Atlas timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["Clock", "FixedClock", "SystemClock", "ensure_utc"]
