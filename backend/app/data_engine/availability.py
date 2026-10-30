"""Information-availability policy for look-ahead prevention (spec §7.4, §18.1).

A bar's ``event_time`` is when its interval *started*; its information cannot be
known before the interval ends. For data backfilled through REST, the original
publication time is unknown (``original_available_at`` is NULL), and the retrieval
time (``available_at``) only says when *Atlas* obtained it.

Backtests over backfilled bars therefore use this conservative, versioned policy:

    usable_from = interval end + feed publication delay + settlement margin

* interval end: ``bar_interval_end`` (midnight New York for daily bars, i.e. well
  after the close and after-hours session).
* feed delay: e.g. 15 minutes for delayed SIP; 0 for IEX/SIP.
* settlement margin: 60 seconds for intraday bars. Alpaca emits minute bars at the
  minute mark and can update them at the following half-minute for late trades, so
  a bar is not settled until at least ~30 s after its interval ends.

Bars with a known ``original_available_at`` (live data, Deliverable 2C) use it.
Bars without a determinable interval end (monthly) return ``None`` and must not be
used by time-sensitive simulations.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.data_engine.schemas import ProviderBar
from app.data_engine.timeframes import bar_interval_end

POLICY_VERSION = "bar-end-plus-settlement-v1"
INTRADAY_SETTLEMENT_MARGIN = timedelta(seconds=60)


def conservative_available_at(
    bar: ProviderBar, feed_delay: timedelta = timedelta(0)
) -> datetime | None:
    """Earliest time a simulation may treat ``bar`` as known; ``None`` if undeterminable."""
    if bar.original_available_at is not None:
        return bar.original_available_at
    end = bar_interval_end(bar.event_time, bar.timeframe)
    if end is None:
        return None
    margin = INTRADAY_SETTLEMENT_MARGIN if bar.timeframe.is_intraday else timedelta(0)
    return end + feed_delay + margin
