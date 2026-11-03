"""Label generation (Phase 5A): pure functions of the bars after a decision.

The caller passes the daily bars of the sessions after the decision (the *path*), the
regular close of the horizon's last session plus the settle delay (``label_end_at``) and
the dataset's ``data_known_at``. Nothing here reads the database or the clock.

Rules (see ``TARGET_DOCUMENTATION`` in ``spec.py``):

* A label is **IMMATURE** when its horizon had not completed and settled by
  ``data_known_at``, and **UNDEFINED** when a bar on the path is missing or the inputs
  cannot define it. Neither is ever used for training or evaluation.
* Returns are direction-adjusted (SHORT gains when the price falls) and net of the stated
  round-trip cost assumption.
* Barrier paths are followed on daily bars; a bar crossing both barriers counts as the
  stop (conservative, spec 18.1); a gap through a barrier exits at the open.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.ml_engine.datasets.spec import TargetSpec
from app.ml_engine.enums import (
    BarrierSource,
    EntryReference,
    ExitReason,
    LabelStatus,
    TargetKind,
)
from app.strategies.enums import Direction


@dataclass(frozen=True)
class PathBar:
    event_time: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class LabelInput:
    direction: Direction
    horizon_periods: int
    decision_close: float | None
    # Daily bars of the path sessions, oldest first; None where a session has no usable bar.
    path: Sequence[PathBar | None]
    label_end_at: datetime | None  # None: the path's sessions are not all known yet
    data_known_at: datetime
    atr_pct: float | None = None  # ATR / close known at the decision
    # Strategy observations: the proposal's invalidation and target levels.
    stop_level: float | None = None
    target_level: float | None = None


@dataclass(frozen=True)
class Label:
    status: LabelStatus
    value: float | None
    cls: int | None
    end_at: datetime | None
    outcome: dict[str, Any]


def _undefined(reason: str, end_at: datetime | None) -> Label:
    return Label(LabelStatus.UNDEFINED, None, None, end_at, {"reason": reason})


def _finite_positive(*values: float | None) -> bool:
    return all(v is not None and math.isfinite(v) and v > 0 for v in values)


def _barrier_exit(
    sign: int, path: Sequence[PathBar], profit: float, stop: float
) -> tuple[ExitReason, float, int]:
    """Follow the path: (reason, exit price, index of the exit bar)."""
    for i, bar in enumerate(path):
        if sign > 0:
            if bar.open <= stop:
                return ExitReason.STOP_BARRIER, bar.open, i
            if bar.open >= profit:
                return ExitReason.PROFIT_BARRIER, bar.open, i
            hit_stop, hit_profit = bar.low <= stop, bar.high >= profit
        else:
            if bar.open >= stop:
                return ExitReason.STOP_BARRIER, bar.open, i
            if bar.open <= profit:
                return ExitReason.PROFIT_BARRIER, bar.open, i
            hit_stop, hit_profit = bar.high >= stop, bar.low <= profit
        if hit_stop:  # also when both were crossed: stop first
            return ExitReason.STOP_BARRIER, stop, i
        if hit_profit:
            return ExitReason.PROFIT_BARRIER, profit, i
    return ExitReason.HORIZON, path[-1].close, len(path) - 1


def make_label(target: TargetSpec, data: LabelInput) -> Label:
    end_at = data.label_end_at
    if end_at is None or end_at > data.data_known_at:
        return Label(
            LabelStatus.IMMATURE,
            None,
            None,
            end_at,
            {"reason": "the horizon had not completed and settled by data_known_at"},
        )
    if not data.path or any(bar is None for bar in data.path):
        return _undefined("a session on the path has no usable bar", end_at)
    path = [bar for bar in data.path if bar is not None]
    if not all(_finite_positive(b.open, b.high, b.low, b.close) for b in path):
        return _undefined("a bar on the path has a non-positive or non-finite price", end_at)
    if target.entry is EntryReference.NEXT_OPEN:
        entry = path[0].open
    else:
        if data.decision_close is None or not _finite_positive(data.decision_close):
            return _undefined("the decision period's close is unknown", end_at)
        entry = data.decision_close
    sign = 1 if data.direction is Direction.LONG else -1
    cost = target.round_trip_cost_bps / 10_000
    outcome: dict[str, Any] = {
        "entry_reference": target.entry.value,
        "entry_price": entry,
        "entry_at": path[0].event_time.isoformat(),
        "round_trip_cost": cost,
    }
    needs_vol = target.kind in (
        TargetKind.VOL_ADJUSTED_RETURN,
        TargetKind.VOL_ADJUSTED_DIRECTION,
    ) or (target.barrier is not None and target.barrier.source is BarrierSource.ATR_MULTIPLES)
    if needs_vol and not _finite_positive(data.atr_pct):
        return _undefined("the ATR known at the decision is unavailable", end_at)

    reason, exit_price, exit_index = ExitReason.HORIZON, path[-1].close, len(path) - 1
    if target.kind is TargetKind.BARRIER and target.barrier is not None:
        barrier = target.barrier
        if barrier.source is BarrierSource.ATR_MULTIPLES:
            atr = float(data.atr_pct or 0.0)
            profit = entry * (1 + sign * barrier.profit_multiple * atr)
            stop = entry * (1 - sign * barrier.stop_multiple * atr)
        else:
            if data.stop_level is None or data.target_level is None:
                return _undefined("the proposal has no invalidation or target level", end_at)
            profit, stop = data.target_level, data.stop_level
            if sign * (profit - stop) <= 0:
                return _undefined("the proposal's levels are on the wrong sides", end_at)
        reason, exit_price, exit_index = _barrier_exit(sign, path, profit, stop)
        outcome.update({"profit_level": profit, "stop_level": stop})

    gross = sign * (exit_price / entry - 1)
    net = gross - cost
    outcome.update(
        {
            "exit_price": exit_price,
            "exit_reason": reason.value,
            "exit_at": path[exit_index].event_time.isoformat(),
            "holding_sessions": exit_index + 1,
            "gross_return": gross,
            "net_return": net,
        }
    )
    value: float = net
    if target.kind in (TargetKind.VOL_ADJUSTED_RETURN, TargetKind.VOL_ADJUSTED_DIRECTION):
        scale = float(data.atr_pct or 0.0) * math.sqrt(data.horizon_periods)
        value = net / scale
        outcome["volatility_scale"] = scale
        outcome["vol_adjusted_return"] = value
    cls: int | None = None
    if target.kind is TargetKind.BARRIER:
        cls = int(reason is ExitReason.PROFIT_BARRIER)
    elif target.kind in (TargetKind.DIRECTION, TargetKind.VOL_ADJUSTED_DIRECTION):
        cls = int(value > target.threshold)
    if not math.isfinite(value):
        return _undefined("the label is not finite", end_at)
    return Label(LabelStatus.MATURED, value, cls, end_at, outcome)


__all__ = ["Label", "LabelInput", "PathBar", "make_label"]
