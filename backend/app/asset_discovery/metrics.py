"""Qualification measurements from stored daily bars (Phase 3A), computed with Polars.

Input is one row per asset and expected session: the newest revision of the session's
daily bar **as Atlas had it at ``as_of``** (rows ingested later are never passed in), plus
completeness evidence. Every expected session is first classified, so that

* a bar with volume 0 (``ZERO_VOLUME``: genuinely no volume) is not confused with
* a bar whose volume field is missing (``VOLUME_MISSING``: excluded, never treated as 0),
* no bar inside time covered by a complete retrieval (``NO_REPORTED_ACTIVITY``: the feed
  saw no trades; a halt or thin trading, which Atlas cannot tell apart without halt data),
* a recorded provider/stream gap (``PROVIDER_GAP``), and
* no bar and no evidence at all (``MISSING``).

Market closures never appear: expected sessions come from the venue's calendar, so
weekends, holidays and early closes are not counted as missing.

Floats are used inside Polars (analytics); values are persisted as ``Decimal`` rounded to
a stated number of places (``app.asset_discovery.qualification``). ``FEATURE_VERSION``
changes whenever a definition below changes.

Input bars carry one price adjustment (``ASSET_DISCOVERY_PRICE_ADJUSTMENT``, SPLIT by
default). Returns, true range and momentum are only meaningful on a series without split
discontinuities; the stored version names the adjustment (``feature_version``).
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import polars as pl

from app.asset_discovery.enums import MetricStatus, SessionDayStatus
from app.asset_discovery.sessions import SessionDay
from app.data_engine.enums import PriceAdjustment

# v2: measured on daily bars of a stated price adjustment (D-063); v1 used RAW bars, whose
# returns, ATR and volatility were wrong across splits.
FEATURE_VERSION = "daily-liquidity-v2"


def feature_version(adjustment: PriceAdjustment) -> str:
    """The stored version names the input series too, e.g. ``daily-liquidity-v2-split``."""
    return f"{FEATURE_VERSION}-{adjustment.value.lower()}"


TRADING_DAYS_PER_YEAR = 252
VALID = "VALID"
_HAS_VALID_BAR = (
    SessionDayStatus.TRADED.value,
    SessionDayStatus.ZERO_VOLUME.value,
    SessionDayStatus.VOLUME_MISSING.value,
)
# Sessions whose volume is known: a valid bar with volume, or evidence of no activity (0).
_VOLUME_KNOWN = (
    SessionDayStatus.TRADED.value,
    SessionDayStatus.ZERO_VOLUME.value,
    SessionDayStatus.NO_REPORTED_ACTIVITY.value,
)


@dataclass(frozen=True)
class MetricWindows:
    history: int = 60
    liquidity: int = 20
    atr: int = 14
    volatility: int = 20
    momentum: int = 20
    recent_volume: int = 5
    # Share of a window's sessions that must have an observation for a value to be OK.
    min_fraction: float = 0.9


@dataclass(frozen=True)
class DailyBar:
    asset_id: uuid.UUID
    session: date
    event_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    vwap: float | None
    quality_status: str
    # When the bar (this revision) became available to Atlas; used by point-in-time readers.
    available_at: datetime | None = None


@dataclass(frozen=True)
class Measurement:
    metric: str
    window: int
    value: float | None
    status: MetricStatus
    observations: int
    expected: int
    source_start: datetime | None = None
    source_end: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssetMeasurements:
    asset_id: uuid.UUID
    sessions: dict[date, SessionDayStatus]
    values: dict[str, Measurement]

    def value(self, metric: str) -> float | None:
        found = self.values.get(metric)
        return None if found is None else found.value

    def status_counts(self, last: int | None = None) -> dict[str, int]:
        days = sorted(self.sessions)[-last:] if last else sorted(self.sessions)
        counts: dict[str, int] = {}
        for day in days:
            key = self.sessions[day].value
            counts[key] = counts.get(key, 0) + 1
        return counts


def _frame(bars: Iterable[DailyBar]) -> pl.DataFrame:
    rows = [
        {
            "asset_id": str(b.asset_id),
            "session": b.session,
            "event_time": b.event_time,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "vwap": b.vwap,
            "quality_status": b.quality_status,
        }
        for b in bars
    ]
    schema: dict[str, Any] = {
        "asset_id": pl.Utf8,
        "session": pl.Date,
        "event_time": pl.Datetime("us", "UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Float64,
        "vwap": pl.Float64,
        "quality_status": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema)


def classify(
    bars: Iterable[DailyBar],
    assets: Iterable[uuid.UUID],
    sessions: list[SessionDay],
    covered: Mapping[uuid.UUID, set[date]],
    gaps: Mapping[uuid.UUID, set[date]],
) -> pl.DataFrame:
    """One row per (asset, expected session) with the session's status and bar values."""
    grid = pl.DataFrame({"asset_id": [str(a) for a in assets]}, schema={"asset_id": pl.Utf8}).join(
        pl.DataFrame({"session": [s.day for s in sessions]}, schema={"session": pl.Date}),
        how="cross",
    )
    evidence = (
        pl.DataFrame(
            [
                {"asset_id": str(a), "session": d, "covered": True, "gap": False}
                for a, days in covered.items()
                for d in days
            ]
            + [
                {"asset_id": str(a), "session": d, "covered": False, "gap": True}
                for a, days in gaps.items()
                for d in days
            ],
            schema={
                "asset_id": pl.Utf8,
                "session": pl.Date,
                "covered": pl.Boolean,
                "gap": pl.Boolean,
            },
        )
        .group_by("asset_id", "session")
        .agg(pl.col("covered").any(), pl.col("gap").any())
    )
    frame = (
        grid.join(_frame(bars), on=["asset_id", "session"], how="left")
        .join(evidence, on=["asset_id", "session"], how="left")
        .with_columns(
            pl.col("covered").fill_null(False),
            pl.col("gap").fill_null(False),
        )
    )
    has_bar = pl.col("quality_status").is_not_null()
    status = (
        pl.when(has_bar & (pl.col("quality_status") == "QUARANTINED"))
        .then(pl.lit(SessionDayStatus.QUARANTINED.value))
        # An INCOMPLETE bar is not final; for a finished session it counts as missing.
        .when(has_bar & (pl.col("quality_status") != VALID))
        .then(pl.lit(SessionDayStatus.MISSING.value))
        .when(has_bar & pl.col("volume").is_null())
        .then(pl.lit(SessionDayStatus.VOLUME_MISSING.value))
        .when(has_bar & (pl.col("volume") == 0))
        .then(pl.lit(SessionDayStatus.ZERO_VOLUME.value))
        .when(has_bar)
        .then(pl.lit(SessionDayStatus.TRADED.value))
        .when(pl.col("gap"))
        .then(pl.lit(SessionDayStatus.PROVIDER_GAP.value))
        .when(pl.col("covered"))
        .then(pl.lit(SessionDayStatus.NO_REPORTED_ACTIVITY.value))
        .otherwise(pl.lit(SessionDayStatus.MISSING.value))
    )
    valid = pl.col("status").is_in(_HAS_VALID_BAR)
    no_activity = pl.col("status") == SessionDayStatus.NO_REPORTED_ACTIVITY.value
    return (
        frame.with_columns(status.alias("status"))
        .with_columns(
            # Volume used for liquidity: reported volume, or 0 where evidence says no trades.
            pl.when(no_activity)
            .then(pl.lit(0.0))
            .when(pl.col("status").is_in(_VOLUME_KNOWN))
            .then(pl.col("volume"))
            .otherwise(None)
            .alias("known_volume"),
            pl.when(valid).then(pl.col("close")).otherwise(None).alias("valid_close"),
        )
        .with_columns(
            pl.when(no_activity)
            .then(pl.lit(0.0))
            .when(pl.col("known_volume").is_not_null())
            .then(pl.col("known_volume") * pl.coalesce("vwap", "close"))
            .otherwise(None)
            .alias("dollar_volume"),
        )
        .sort("asset_id", "session")
        .with_columns(
            pl.col("valid_close").shift(1).over("asset_id").alias("prev_close"),
        )
        .with_columns(
            pl.when(valid & pl.col("prev_close").is_not_null())
            .then(
                pl.max_horizontal(
                    pl.col("high") - pl.col("low"),
                    (pl.col("high") - pl.col("prev_close")).abs(),
                    (pl.col("low") - pl.col("prev_close")).abs(),
                )
            )
            .otherwise(None)
            .alias("true_range"),
            pl.when(valid & pl.col("prev_close").is_not_null() & (pl.col("prev_close") > 0))
            .then((pl.col("close") / pl.col("prev_close")).log())
            .otherwise(None)
            .alias("log_return"),
        )
    )


def _required(window: int, windows: MetricWindows) -> int:
    return max(1, math.ceil(window * windows.min_fraction))


def compute(
    bars: Iterable[DailyBar],
    assets: Iterable[uuid.UUID],
    sessions: list[SessionDay],
    *,
    covered: Mapping[uuid.UUID, set[date]] | None = None,
    gaps: Mapping[uuid.UUID, set[date]] | None = None,
    windows: MetricWindows | None = None,
    benchmark: uuid.UUID | None = None,
) -> dict[uuid.UUID, AssetMeasurements]:
    """Measurements per asset over the ``sessions`` window (oldest first)."""
    windows = windows or MetricWindows()
    asset_list = list(dict.fromkeys(assets))
    if not asset_list or not sessions:
        return {}
    if len(sessions) < windows.history:
        raise ValueError(f"need {windows.history} expected sessions, got {len(sessions)}")
    frame = classify(bars, asset_list, sessions[-windows.history :], covered or {}, gaps or {})
    days = [s.day for s in sessions[-windows.history :]]
    liquidity_days = days[-windows.liquidity :]
    recent_days = days[-windows.recent_volume :]

    in_liq = pl.col("session").is_in(liquidity_days)
    in_recent = pl.col("session").is_in(recent_days)
    valid = pl.col("status").is_in(_HAS_VALID_BAR)
    summary = frame.group_by("asset_id").agg(
        valid.sum().alias("valid_bars"),
        pl.col("status")
        .is_in([SessionDayStatus.MISSING.value, SessionDayStatus.PROVIDER_GAP.value])
        .sum()
        .alias("missing"),
        (pl.col("status") == SessionDayStatus.QUARANTINED.value).sum().alias("invalid"),
        pl.col("known_volume").filter(in_liq).count().alias("volume_obs"),
        pl.col("known_volume").filter(in_liq).mean().alias("avg_volume"),
        pl.col("dollar_volume").filter(in_liq).mean().alias("avg_dollar_volume"),
        pl.col("dollar_volume").filter(in_liq).median().alias("median_dollar_volume"),
        (pl.col("known_volume").filter(in_liq) == 0).sum().alias("zero_volume_sessions"),
        pl.col("known_volume").filter(in_recent).mean().alias("recent_volume"),
        pl.col("known_volume").filter(in_recent).count().alias("recent_obs"),
        pl.col("true_range").drop_nulls().tail(windows.atr).mean().alias("atr"),
        pl.col("true_range").drop_nulls().tail(windows.atr).count().alias("atr_obs"),
        pl.col("log_return").drop_nulls().tail(windows.volatility).std().alias("return_std"),
        pl.col("log_return").drop_nulls().tail(windows.volatility).count().alias("return_obs"),
        pl.col("valid_close").drop_nulls().last().alias("last_close"),
        pl.col("session").filter(valid).max().alias("last_valid_session"),
        pl.col("event_time").filter(valid).min().alias("source_start"),
        pl.col("event_time").filter(valid).max().alias("source_end"),
        pl.col("valid_close").filter(pl.col("session") == days[-1]).first().alias("close_end"),
        pl.col("valid_close")
        .filter(pl.col("session") == days[-1 - windows.momentum])
        .first()
        .alias("close_start"),
    )
    statuses = {
        uuid.UUID(asset): dict(
            zip(group["session"].to_list(), group["status"].to_list(), strict=True)
        )
        for (asset,), group in frame.group_by("asset_id")
    }

    results: dict[uuid.UUID, AssetMeasurements] = {}
    returns: dict[uuid.UUID, float | None] = {}
    for row in summary.iter_rows(named=True):
        asset_id = uuid.UUID(row["asset_id"])
        start, end = row["source_start"], row["source_end"]

        def m(
            metric: str,
            window: int,
            value: float | None,
            observations: int,
            required: int,
            details: dict[str, Any] | None = None,
            *,
            start: datetime | None = start,
            end: datetime | None = end,
        ) -> Measurement:
            if observations == 0 or value is None or not math.isfinite(value):
                status = (
                    MetricStatus.NO_DATA if observations == 0 else MetricStatus.INSUFFICIENT_DATA
                )
                return Measurement(
                    metric, window, None, status, observations, required, start, end, details or {}
                )
            if observations < required:
                return Measurement(
                    metric,
                    window,
                    None,
                    MetricStatus.INSUFFICIENT_DATA,
                    observations,
                    required,
                    start,
                    end,
                    {**(details or {}), "partial_value": value},
                )
            return Measurement(
                metric,
                window,
                value,
                MetricStatus.OK,
                observations,
                required,
                start,
                end,
                details or {},
            )

        h, liq = windows.history, windows.liquidity
        valid_bars = int(row["valid_bars"])
        last_valid: date | None = row["last_valid_session"]
        since_last = len(days) if last_valid is None else len(days) - 1 - days.index(last_valid)
        volume_obs = int(row["volume_obs"])
        momentum = None
        if row["close_end"] and row["close_start"]:
            momentum = row["close_end"] / row["close_start"] - 1.0
        returns[asset_id] = momentum
        values = [
            # Shares of the window: always computable (zero observations is a fact here).
            Measurement(
                "history_coverage", h, valid_bars / h, MetricStatus.OK, valid_bars, h, start, end
            ),
            Measurement(
                "missing_session_ratio", h, row["missing"] / h, MetricStatus.OK, h, h, start, end
            ),
            Measurement(
                "invalid_bar_ratio", h, row["invalid"] / h, MetricStatus.OK, h, h, start, end
            ),
            Measurement(
                "sessions_since_last_bar",
                h,
                float(since_last),
                MetricStatus.OK,
                h,
                h,
                start,
                end,
                {"last_valid_session": None if last_valid is None else last_valid.isoformat()},
            ),
            m("last_close", h, row["last_close"], valid_bars, 1),
            m("avg_daily_volume", liq, row["avg_volume"], volume_obs, _required(liq, windows)),
            m(
                "avg_dollar_volume",
                liq,
                row["avg_dollar_volume"],
                volume_obs,
                _required(liq, windows),
                {"price": "vwap, else close"},
            ),
            m(
                "median_dollar_volume",
                liq,
                row["median_dollar_volume"],
                volume_obs,
                _required(liq, windows),
                {"price": "vwap, else close"},
            ),
            m(
                "zero_volume_ratio",
                liq,
                None if volume_obs == 0 else row["zero_volume_sessions"] / volume_obs,
                volume_obs,
                _required(liq, windows),
            ),
            m(
                "relative_volume",
                windows.recent_volume,
                None
                if not row["avg_volume"] or row["recent_volume"] is None
                else row["recent_volume"] / row["avg_volume"],
                int(row["recent_obs"]),
                _required(windows.recent_volume, windows),
                {"recent_sessions": windows.recent_volume, "baseline_sessions": liq},
            ),
            m(
                "atr",
                windows.atr,
                row["atr"],
                int(row["atr_obs"]),
                windows.atr,
                {"method": "simple mean of true range"},
            ),
            m(
                "atr_pct",
                windows.atr,
                None if not row["atr"] or not row["last_close"] else row["atr"] / row["last_close"],
                int(row["atr_obs"]),
                windows.atr,
            ),
            m(
                "realized_volatility",
                windows.volatility,
                None
                if row["return_std"] is None
                else row["return_std"] * math.sqrt(TRADING_DAYS_PER_YEAR),
                int(row["return_obs"]),
                _required(windows.volatility, windows),
                {
                    "annualization_days": TRADING_DAYS_PER_YEAR,
                    "returns": "consecutive-session log returns",
                },
            ),
            m(
                "return_20d",
                windows.momentum,
                momentum,
                2 if momentum is not None else 0,
                2,
                {"sessions": windows.momentum},
            ),
        ]
        results[asset_id] = AssetMeasurements(
            asset_id=asset_id,
            sessions={d: SessionDayStatus(s) for d, s in statuses.get(asset_id, {}).items()},
            values={v.metric: v for v in values},
        )

    bench = returns.get(benchmark) if benchmark is not None else None
    for asset_id, measured in results.items():
        own = returns.get(asset_id)
        value = None if own is None or bench is None or asset_id == benchmark else own - bench
        reason = {} if bench is not None else {"note": "benchmark return unavailable"}
        measured.values["relative_strength_20d"] = Measurement(
            "relative_strength_20d",
            windows.momentum,
            value,
            MetricStatus.OK if value is not None else MetricStatus.NO_DATA,
            1 if value is not None else 0,
            1,
            details={"benchmark_asset_id": None if benchmark is None else str(benchmark), **reason},
        )
    return results


@dataclass(frozen=True)
class SpreadSummary:
    quotes: int
    median_spread_bps: float | None
    invalid_ratio: float | None
    window_start: datetime
    window_end: datetime


def spread_measurements(
    asset_id: uuid.UUID, summary: SpreadSummary | None, sessions: int
) -> list[Measurement]:
    """Quoted-spread measurements; NO_DATA when Atlas has no quotes for the asset."""
    if summary is None or summary.quotes == 0:
        details = {
            "note": "no stored quotes in the window (quotes are stored only for streamed assets)"
        }
        return [
            Measurement(
                "median_spread_bps", sessions, None, MetricStatus.NO_DATA, 0, 1, details=details
            ),
            Measurement(
                "invalid_quote_ratio", sessions, None, MetricStatus.NO_DATA, 0, 1, details=details
            ),
        ]
    return [
        Measurement(
            "median_spread_bps",
            sessions,
            summary.median_spread_bps,
            MetricStatus.OK if summary.median_spread_bps is not None else MetricStatus.NO_DATA,
            summary.quotes,
            1,
            summary.window_start,
            summary.window_end,
            {"basis": "regular-session two-sided quotes"},
        ),
        Measurement(
            "invalid_quote_ratio",
            sessions,
            summary.invalid_ratio,
            MetricStatus.OK if summary.invalid_ratio is not None else MetricStatus.NO_DATA,
            summary.quotes,
            1,
            summary.window_start,
            summary.window_end,
        ),
    ]


__all__ = [
    "FEATURE_VERSION",
    "AssetMeasurements",
    "DailyBar",
    "Measurement",
    "MetricWindows",
    "SpreadSummary",
    "classify",
    "compute",
    "feature_version",
    "spread_measurements",
]
