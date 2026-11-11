"""Phase 8B: chronological walk-forward layouts, embargo, purging and holdout isolation.

Deterministic test vectors of exchange sessions (no market data).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from app.backtesting.data import sessions_between
from app.learning.enums import WindowMode
from app.learning.windows import (
    FoldWindows,
    Window,
    WindowSpec,
    build_layout,
    check_layout,
    purge_trades,
    required_embargo,
)
from app.ml_engine.validation.splits import LeakageError

SESSIONS = [s.day for s in sessions_between(date(2026, 1, 2), date(2026, 8, 31))]


def test_required_embargo_is_the_longest_label_in_sessions() -> None:
    assert required_embargo([("1Day", 5, 1), ("1Day", 10, 1)]) == 11
    # A weekly label of 2 periods + 1 period to live spans 15 sessions.
    assert required_embargo([("1Day", 5, 1), ("1Week", 2, 1)]) == 15
    with pytest.raises(ValueError, match="unknown timeframe"):
        required_embargo([("1Hour", 1, 1)])


@pytest.mark.parametrize("mode", [WindowMode.ROLLING, WindowMode.EXPANDING])
def test_every_test_window_follows_its_training_and_validation_by_the_embargo(
    mode: WindowMode,
) -> None:
    spec = WindowSpec(mode=mode, train_sessions=30, validation_sessions=10, test_sessions=10)
    layout = build_layout(SESSIONS, spec, minimum_embargo=6)
    assert layout.embargo == 6
    assert len(layout.folds) >= 3
    previous_end = -1
    for f in layout.folds:
        assert f.validation is not None
        assert f.validation.start - f.train.end - 1 >= 6
        assert f.test.start - f.validation.end - 1 >= 6
        # Dates, not just indices, are strictly increasing.
        assert SESSIONS[f.train.end] < SESSIONS[f.validation.start] < SESSIONS[f.test.start]
        assert f.test.start > previous_end  # test windows never overlap
        previous_end = f.test.end
        if mode is WindowMode.EXPANDING:
            assert f.train.start == 0
        else:
            assert f.train.length == 30
    if mode is WindowMode.ROLLING:
        starts = [f.train.start for f in layout.folds]
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts)


def test_the_final_holdout_is_never_touched_by_any_fold() -> None:
    layout = build_layout(SESSIONS, WindowSpec(train_sessions=30, test_sessions=10), 5)
    assert layout.holdout is not None
    assert layout.holdout.end == len(SESSIONS) - 1
    # Default: 20% of the sessions, separated from development by the embargo.
    assert layout.holdout.length == round(len(SESSIONS) * 0.2)
    assert layout.holdout.start - layout.development.end - 1 >= layout.embargo
    for f in layout.folds:
        for w in (f.train, f.test):
            assert w.end <= layout.development.end < layout.holdout.start


def test_an_embargo_shorter_than_the_label_horizon_is_refused() -> None:
    with pytest.raises(ValueError, match="shorter than the longest label horizon"):
        build_layout(SESSIONS, WindowSpec(embargo_sessions=3), minimum_embargo=6)


def test_overlapping_test_windows_are_refused() -> None:
    with pytest.raises(ValueError, match="overlap"):
        WindowSpec(test_sessions=20, step_sessions=10)


def test_too_little_data_for_one_fold_is_explained() -> None:
    with pytest.raises(ValueError, match="one fold needs"):
        build_layout(SESSIONS[:60], WindowSpec(train_sessions=40, test_sessions=20), 5)


def test_refit_schedule_marks_which_folds_reselect() -> None:
    layout = build_layout(
        SESSIONS, WindowSpec(train_sessions=20, test_sessions=10, refit_every=2), 3
    )
    assert [f.refit for f in layout.folds][:4] == [True, False, True, False]


def test_check_layout_detects_every_violation_independently() -> None:
    layout = build_layout(SESSIONS, WindowSpec(train_sessions=30, test_sessions=10), 5)
    check_layout(layout)
    first = layout.folds[0]
    inside_embargo = replace(
        layout,
        folds=[replace(first, test=Window(first.train.end + 2, first.train.end + 11))],
    )
    with pytest.raises(LeakageError, match="embargo"):
        check_layout(inside_embargo)
    assert layout.holdout is not None
    touches_holdout = replace(
        layout,
        folds=[
            FoldWindows(
                0, Window(0, 10), None, Window(layout.holdout.start, layout.holdout.end), True
            )
        ],
    )
    with pytest.raises(LeakageError):
        check_layout(touches_holdout)
    overlapping = replace(layout, folds=[layout.folds[1], layout.folds[0]])
    with pytest.raises(LeakageError, match="overlap or go back"):
        check_layout(overlapping)
    short = replace(layout, embargo=layout.required_embargo - 1)
    with pytest.raises(LeakageError, match="embargo"):
        check_layout(short)


def _trade(first: date, last: date | None, status: str = "CLOSED") -> SimpleNamespace:
    return SimpleNamespace(first_session=first, last_session=last, status=status)


def test_purge_keeps_only_trades_opened_and_closed_inside_the_window() -> None:
    first, last = date(2026, 3, 2), date(2026, 3, 31)
    trades = [
        _trade(date(2026, 2, 27), date(2026, 3, 5)),  # opened before: purged
        _trade(date(2026, 3, 3), date(2026, 3, 10)),  # kept
        _trade(date(2026, 3, 25), date(2026, 4, 2)),  # its label ends after the window
        _trade(date(2026, 3, 30), None, "OPEN_AT_END"),  # still open
        _trade(date(2026, 4, 1), date(2026, 4, 3)),  # after the window
    ]
    result = purge_trades(trades, first, last)
    assert result.kept == [trades[1]]
    assert (result.before_window, result.open_at_end, result.after_window) == (1, 2, 1)
