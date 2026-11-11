"""Point-in-time market-regime labels for segmenting results (Phase 8B).

Phase 3B regime classifications were not recorded historically (Atlas began storing them in
November 2026), so validation derives a simple, documented label for each session from the
benchmark's own daily closes **as known at that session's close** (no later bar is read):

* **Trend:** ``UP`` if the benchmark close is above its close ``trend_sessions`` sessions
  earlier, else ``DOWN`` (``UNKNOWN`` without that much history).
* **Volatility:** the annualized standard deviation of the last ``vol_sessions`` daily log
  returns, compared with the median of that same measure over the preceding sessions
  (at most ``vol_reference`` of them, at least ``vol_min_reference``): ``HIGH`` above
  ``high_ratio`` x the median, ``LOW`` below ``low_ratio`` x, else ``NORMAL``.

A session's *return* is attributed to the regime known at the **previous** session's close
(the regime in force while the positions were held), and a trade to the regime known at
the session before its first fill. These are segmentation labels, not trading signals;
a split-adjusted benchmark series is used (ratios only, so the adjustment level does not
matter, but the series as stored may include later revisions).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any

TREND_SESSIONS = 50
VOL_SESSIONS = 20
VOL_REFERENCE = 252
VOL_MIN_REFERENCE = 60
HIGH_RATIO = 1.25
LOW_RATIO = 0.8


@dataclass(frozen=True)
class Regime:
    trend: str
    volatility: str
    realized_volatility: float | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "trend": self.trend,
            "volatility": self.volatility,
            "realized_volatility": self.realized_volatility,
        }


UNKNOWN = Regime("UNKNOWN", "UNKNOWN")

DEFINITION = {
    "source": "benchmark daily closes as known at each session's close (split-adjusted)",
    "trend": f"UP if close > close {TREND_SESSIONS} sessions earlier, else DOWN",
    "volatility": (
        f"annualized sd of the last {VOL_SESSIONS} log returns vs the median of that measure "
        f"over up to {VOL_REFERENCE} prior sessions (min {VOL_MIN_REFERENCE}): HIGH > "
        f"{HIGH_RATIO}x, LOW < {LOW_RATIO}x, else NORMAL"
    ),
    "attribution": "a session's return uses the regime known at the previous close; a trade "
    "uses the regime known at the session before its first fill",
}


def classify(closes: list[tuple[date, float]]) -> dict[date, Regime]:
    """Regime known at each session's close, from that session's and earlier closes."""
    ordered = sorted(closes)
    days = [d for d, _ in ordered]
    px = [c for _, c in ordered]
    out: dict[date, Regime] = {}
    vols: list[float | None] = []
    for i, day in enumerate(days):
        trend = "UNKNOWN"
        if i >= TREND_SESSIONS and px[i - TREND_SESSIONS] > 0:
            trend = "UP" if px[i] > px[i - TREND_SESSIONS] else "DOWN"
        vol: float | None = None
        if i >= VOL_SESSIONS:
            rets = [
                math.log(px[j] / px[j - 1])
                for j in range(i - VOL_SESSIONS + 1, i + 1)
                if px[j - 1] > 0 and px[j] > 0
            ]
            if len(rets) >= VOL_SESSIONS - 1:
                vol = statistics.stdev(rets) * math.sqrt(252)
        previous = [v for v in vols[max(0, i - VOL_REFERENCE) : i] if v is not None]
        vols.append(vol)
        label = "UNKNOWN"
        if vol is not None and len(previous) >= VOL_MIN_REFERENCE:
            median = statistics.median(previous)
            if median > 0:
                ratio = vol / median
                label = "HIGH" if ratio > HIGH_RATIO else "LOW" if ratio < LOW_RATIO else "NORMAL"
        out[day] = Regime(trend, label, vol)
    return out


def known_before(regimes: dict[date, Regime], day: date) -> Regime:
    """The regime known at the last session strictly before ``day``."""
    earlier = [d for d in regimes if d < day]
    return regimes[max(earlier)] if earlier else UNKNOWN


__all__ = ["DEFINITION", "UNKNOWN", "Regime", "classify", "known_before"]
