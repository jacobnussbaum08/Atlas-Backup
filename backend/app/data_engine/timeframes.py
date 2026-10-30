"""Bar timeframes as validated value objects.

Atlas's canonical text form is ``<amount><unit>`` with units ``Min``, ``Hour``,
``Day``, ``Week`` and ``Month`` (e.g. ``5Min``, ``1Day``). Providers translate this
form to their own syntax inside their adapters.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import total_ordering
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer


class TimeframeUnit(StrEnum):
    MINUTE = "Min"
    HOUR = "Hour"
    DAY = "Day"
    WEEK = "Week"
    MONTH = "Month"


# Approximate ordering weight in minutes; used for sorting only, never for arithmetic.
_UNIT_ORDER = {
    TimeframeUnit.MINUTE: 1,
    TimeframeUnit.HOUR: 60,
    TimeframeUnit.DAY: 60 * 24,
    TimeframeUnit.WEEK: 60 * 24 * 7,
    TimeframeUnit.MONTH: 60 * 24 * 31,
}
_PATTERN = re.compile(r"^(?P<amount>[1-9][0-9]{0,2})(?P<unit>Min|Hour|Day|Week|Month)$")


@total_ordering
class Timeframe(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: int = Field(ge=1, le=999)
    unit: TimeframeUnit

    @classmethod
    def parse(cls, text: str) -> Timeframe:
        match = _PATTERN.match(text.strip())
        if not match:
            raise ValueError(f"invalid timeframe {text!r}; expected e.g. 1Min, 15Min, 1Hour, 1Day")
        return cls(amount=int(match["amount"]), unit=TimeframeUnit(match["unit"]))

    def __str__(self) -> str:
        return f"{self.amount}{self.unit.value}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Timeframe):
            return NotImplemented
        return self._weight < other._weight

    @property
    def _weight(self) -> int:
        return self.amount * _UNIT_ORDER[self.unit]

    @property
    def is_intraday(self) -> bool:
        return self.unit in (TimeframeUnit.MINUTE, TimeframeUnit.HOUR)

    @property
    def fixed_duration(self) -> timedelta | None:
        """Exact length for intraday bars.

        Day and longer bars follow exchange sessions and calendars, so they have no
        fixed length and return ``None`` rather than a misleading 24h-multiple.
        """
        if self.unit is TimeframeUnit.MINUTE:
            return timedelta(minutes=self.amount)
        if self.unit is TimeframeUnit.HOUR:
            return timedelta(hours=self.amount)
        return None


US_EQUITY_TIMEZONE = ZoneInfo("America/New_York")


def bar_interval_end(
    event_time: datetime, timeframe: Timeframe, session_timezone: ZoneInfo = US_EQUITY_TIMEZONE
) -> datetime | None:
    """End of the bar that starts at ``event_time``; ``None`` if it has no exact end.

    Intraday bars have a fixed length. Day and week bars start at midnight in the
    exchange's time zone (New York for US equities), so their end is computed in local
    time to stay correct across daylight-saving changes. Month bars return ``None``.
    """
    duration = timeframe.fixed_duration
    if duration is not None:
        return event_time + duration
    local = event_time.astimezone(session_timezone)
    if timeframe.unit is TimeframeUnit.DAY:
        return (local + timedelta(days=timeframe.amount)).astimezone(UTC)
    if timeframe.unit is TimeframeUnit.WEEK:
        return (local + timedelta(weeks=timeframe.amount)).astimezone(UTC)
    return None


def is_aligned(
    event_time: datetime, timeframe: Timeframe, session_timezone: ZoneInfo = US_EQUITY_TIMEZONE
) -> bool | None:
    """Whether a bar start lies on the timeframe's grid; ``None`` if not checkable.

    Intraday bars start on multiples of their length from the Unix epoch (UTC); day
    bars start at local midnight. Week/month grids are not checked.
    """
    duration = timeframe.fixed_duration
    if duration is not None:
        offset = event_time - datetime(1970, 1, 1, tzinfo=UTC)
        return offset % duration == timedelta(0)
    if timeframe.unit is TimeframeUnit.DAY and timeframe.amount == 1:
        local = event_time.astimezone(session_timezone)
        return (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)
    return None


def _coerce(value: Any) -> Any:
    return Timeframe.parse(value) if isinstance(value, str) else value


# Field type that accepts "5Min" or a Timeframe and serializes back to "5Min".
TimeframeField = Annotated[
    Timeframe, BeforeValidator(_coerce), PlainSerializer(str, return_type=str)
]

MINUTE_1 = Timeframe(amount=1, unit=TimeframeUnit.MINUTE)
MINUTE_5 = Timeframe(amount=5, unit=TimeframeUnit.MINUTE)
MINUTE_15 = Timeframe(amount=15, unit=TimeframeUnit.MINUTE)
HOUR_1 = Timeframe(amount=1, unit=TimeframeUnit.HOUR)
DAY_1 = Timeframe(amount=1, unit=TimeframeUnit.DAY)

# Timeframes Atlas ingests in Phase 2. Adding one is a configuration change, not a redesign.
INITIAL_TIMEFRAMES: tuple[Timeframe, ...] = (MINUTE_1, MINUTE_5, MINUTE_15, HOUR_1, DAY_1)
