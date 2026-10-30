"""Bar timeframes as validated value objects.

Atlas's canonical text form is ``<amount><unit>`` with units ``Min``, ``Hour``,
``Day``, ``Week`` and ``Month`` (e.g. ``5Min``, ``1Day``). Providers translate this
form to their own syntax inside their adapters.
"""

from __future__ import annotations

import re
from datetime import timedelta
from enum import StrEnum
from functools import total_ordering
from typing import Annotated, Any

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
