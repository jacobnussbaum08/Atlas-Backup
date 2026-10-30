"""Paper-trading isolation (spec section 24.1).

Atlas has exactly one trading mode. It is a constant rather than a setting so
that no environment variable, database row, or API call can switch it.
"""

from enum import StrEnum
from typing import Final


class TradingMode(StrEnum):
    PAPER = "PAPER"


TRADING_MODE: Final = TradingMode.PAPER
