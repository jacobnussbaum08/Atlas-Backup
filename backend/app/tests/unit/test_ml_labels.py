"""Phase 5A label generation (pure). Prices here are hand-made TEST FIXTURES."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ml_engine.datasets.labels import LabelInput, PathBar, make_label
from app.ml_engine.datasets.spec import BarrierSpec, TargetSpec
from app.ml_engine.enums import (
    BarrierSource,
    EntryReference,
    ExitReason,
    LabelStatus,
    TargetKind,
)
from app.strategies.enums import Direction

T0 = datetime(2026, 3, 2, 21, tzinfo=UTC)
KNOWN = datetime(2026, 4, 1, tzinfo=UTC)


def bar(day: int, o: float, h: float, low: float, c: float) -> PathBar:
    return PathBar(T0 + timedelta(days=day), o, h, low, c)


def inputs(path, direction=Direction.LONG, **kw) -> LabelInput:
    base = {
        "direction": direction,
        "horizon_periods": len(path),
        "decision_close": 100.0,
        "path": path,
        "label_end_at": T0 + timedelta(days=len(path), hours=1),
        "data_known_at": KNOWN,
        "atr_pct": 0.02,
    }
    base.update(kw)
    return LabelInput(**base)


PATH = [bar(1, 100, 101, 99, 100.5), bar(2, 100.5, 103, 100, 102)]


def test_forward_return_is_net_of_costs_and_direction_adjusted():
    target = TargetSpec(kind=TargetKind.FORWARD_RETURN, horizon_periods=2, round_trip_cost_bps=10)
    long = make_label(target, inputs(PATH))
    short = make_label(target, inputs(PATH, Direction.SHORT))
    assert long.status is LabelStatus.MATURED
    assert long.value == pytest.approx(102 / 100 - 1 - 0.001)
    assert short.value == pytest.approx(-(102 / 100 - 1) - 0.001)
    assert long.outcome["entry_price"] == 100  # NEXT_OPEN: the first period's open


def test_direction_label_means_profitable_after_costs_not_price_up():
    up_by_less_than_costs = [bar(1, 100, 100.1, 99.9, 100.05)]
    target = TargetSpec(kind=TargetKind.DIRECTION, horizon_periods=1, round_trip_cost_bps=10)
    label = make_label(target, inputs(up_by_less_than_costs))
    assert label.outcome["gross_return"] > 0
    assert label.cls == 0


def test_decision_close_entry():
    target = TargetSpec(
        kind=TargetKind.FORWARD_RETURN,
        horizon_periods=2,
        entry=EntryReference.DECISION_CLOSE,
        round_trip_cost_bps=0,
    )
    label = make_label(target, inputs(PATH, decision_close=98.0))
    assert label.value == pytest.approx(102 / 98 - 1)


def test_immature_when_the_horizon_has_not_settled():
    target = TargetSpec(kind=TargetKind.DIRECTION, horizon_periods=2)
    label = make_label(target, inputs(PATH, data_known_at=T0 + timedelta(days=1)))
    assert label.status is LabelStatus.IMMATURE
    assert label.value is None
    assert label.cls is None


def test_missing_bar_on_the_path_is_undefined():
    target = TargetSpec(kind=TargetKind.DIRECTION, horizon_periods=2)
    label = make_label(target, inputs([PATH[0], None]))
    assert label.status is LabelStatus.UNDEFINED


def test_vol_adjusted_return_uses_the_ex_ante_atr():
    target = TargetSpec(
        kind=TargetKind.VOL_ADJUSTED_RETURN, horizon_periods=2, round_trip_cost_bps=0
    )
    label = make_label(target, inputs(PATH, atr_pct=0.01))
    assert label.value == pytest.approx(0.02 / (0.01 * 2**0.5))
    assert make_label(target, inputs(PATH, atr_pct=None)).status is LabelStatus.UNDEFINED


def barrier(source=BarrierSource.ATR_MULTIPLES) -> TargetSpec:
    return TargetSpec(
        kind=TargetKind.BARRIER,
        horizon_periods=3,
        round_trip_cost_bps=0,
        barrier=BarrierSpec(source=source, profit_multiple=2, stop_multiple=1),
    )


def test_barrier_profit_first():
    # entry 100, ATR 2% -> profit 104, stop 98
    path = [bar(1, 100, 101, 99, 100), bar(2, 100, 104.5, 99.5, 104)]
    label = make_label(barrier(), inputs(path))
    assert label.cls == 1
    assert label.outcome["exit_reason"] == ExitReason.PROFIT_BARRIER.value
    assert label.outcome["exit_price"] == pytest.approx(104)


def test_barrier_same_bar_counts_the_stop_first():
    path = [bar(1, 100, 105, 97, 101)]
    label = make_label(barrier(), inputs(path))
    assert label.cls == 0
    assert label.outcome["exit_reason"] == ExitReason.STOP_BARRIER.value
    assert label.outcome["exit_price"] == pytest.approx(98)


def test_barrier_gap_exits_at_the_open():
    path = [bar(1, 100, 100.5, 99.5, 100), bar(2, 95, 96, 94, 95.5)]
    label = make_label(barrier(), inputs(path))
    assert label.outcome["exit_price"] == 95
    assert label.outcome["net_return"] == pytest.approx(-0.05)


def test_barrier_short_mirrors_long():
    path = [bar(1, 100, 101, 95.5, 96)]
    label = make_label(barrier(), inputs(path, Direction.SHORT))
    assert label.cls == 1  # profit at 96 for a short
    assert label.outcome["net_return"] == pytest.approx(0.04)


def test_strategy_levels_must_be_on_the_correct_sides():
    target = barrier(BarrierSource.STRATEGY_LEVELS)
    ok = make_label(target, inputs(PATH, stop_level=97.0, target_level=102.5))
    assert ok.status is LabelStatus.MATURED
    wrong = make_label(target, inputs(PATH, stop_level=103.0, target_level=97.0))
    assert wrong.status is LabelStatus.UNDEFINED
