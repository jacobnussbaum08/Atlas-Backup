"""Chronological walk-forward windows over trading sessions (Phase 8B, spec 18.2).

The unit is the exchange session. Given the sessions of an experiment, the layout is::

    | development ................................................ | embargo | holdout |
    | train_1 | emb | validation_1 | emb | test_1 |
         | train_2 | emb | validation_2 | emb | test_2 |         (ROLLING)
    | train_2 ...........  | emb | validation_2 | emb | test_2 |  (EXPANDING)

* **Holdout.** The last ``holdout_sessions`` sessions are the untouched final evaluation
  period. No fold, search, selection or robustness method sees them; they are replayed only
  by an explicit holdout evaluation of a frozen configuration, and every such access is
  recorded and counted (``validation_holdout_accesses``).
* **Embargo.** Every boundary where information flows forward (train -> validation,
  validation -> test, development -> holdout) is separated by ``embargo_sessions`` sessions
  that belong to neither side. The minimum is the longest label of the candidates: the
  holding horizon plus the signal's time to live, in sessions (:func:`required_embargo`).
  A shorter embargo is refused.
* **Purge.** Selection statistics of a window count only trades opened in it and closed by
  its last session (:func:`purge_trades`); a trade still open at the window's end is
  removed from the trade statistics (its mark-to-market return up to the end stays in the
  daily returns, which uses no later information).
* **Refit schedule.** Parameters are re-selected every ``refit_every`` folds; in between,
  a fold reuses the previous selection (recorded as such).

:func:`check_layout` re-verifies every invariant independently of how the layout was built.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.learning.enums import WindowMode
from app.ml_engine.validation.splits import LeakageError

SESSIONS_PER_PERIOD = {"1Day": 1, "1Week": 5}


class WindowSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: WindowMode = WindowMode.ROLLING
    train_sessions: int = Field(default=40, ge=10, le=2600)
    # 0: no separate validation window (selection on the training window only).
    validation_sessions: int = Field(default=0, ge=0, le=2600)
    test_sessions: int = Field(default=20, ge=5, le=2600)
    # Sessions between successive folds (None: test_sessions, so test windows tile).
    step_sessions: int | None = Field(default=None, ge=1, le=2600)
    # None: the required minimum (longest label horizon). Lower values are refused.
    embargo_sessions: int | None = Field(default=None, ge=0, le=520)
    # The untouched final period (None: 20% of the sessions, at least test_sessions).
    holdout_sessions: int | None = Field(default=None, ge=0, le=2600)
    refit_every: int = Field(default=1, ge=1, le=52)
    max_folds: int = Field(default=12, ge=1, le=60)

    @model_validator(mode="after")
    def _step(self) -> WindowSpec:
        step = self.step_sessions or self.test_sessions
        if step < self.test_sessions:
            raise ValueError(
                "step_sessions below test_sessions would make test windows overlap; "
                "overlapping out-of-sample windows double-count observations"
            )
        return self


@dataclass(frozen=True)
class Window:
    start: int  # index into the experiment's sessions, inclusive
    end: int  # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def describe(self, sessions: list[date]) -> dict[str, Any]:
        return {
            "start": sessions[self.start].isoformat(),
            "end": sessions[self.end].isoformat(),
            "sessions": self.length,
        }


@dataclass(frozen=True)
class FoldWindows:
    index: int
    train: Window
    validation: Window | None
    test: Window
    refit: bool  # parameters re-selected at this fold (else the previous selection)

    @property
    def selection_end(self) -> int:
        """The last session whose information selection may use."""
        return (self.validation or self.train).end


@dataclass(frozen=True)
class Layout:
    sessions: list[date]
    embargo: int
    development: Window
    holdout: Window | None
    folds: list[FoldWindows]
    required_embargo: int
    mode: WindowMode

    def describe(self) -> dict[str, Any]:
        s = self.sessions
        return {
            "mode": self.mode.value,
            "sessions": len(s),
            "embargo_sessions": self.embargo,
            "required_embargo_sessions": self.required_embargo,
            "development": self.development.describe(s),
            "holdout": None if self.holdout is None else self.holdout.describe(s),
            "folds": [
                {
                    "index": f.index,
                    "train": f.train.describe(s),
                    "validation": None if f.validation is None else f.validation.describe(s),
                    "test": f.test.describe(s),
                    "refit": f.refit,
                }
                for f in self.folds
            ],
            "rule": "train < embargo < validation < embargo < test; development < embargo < "
            "holdout; test windows never overlap; the holdout is never used for selection",
        }


def required_embargo(horizons: list[tuple[str, int, int]]) -> int:
    """Longest label of the candidates, in sessions: (timeframe, holding horizon periods,
    signal time-to-live periods) -> (horizon + ttl) x sessions per period."""
    longest = 0
    for timeframe, horizon, ttl in horizons:
        per = SESSIONS_PER_PERIOD.get(timeframe)
        if per is None:
            raise ValueError(f"unknown timeframe {timeframe}")
        longest = max(longest, (horizon + ttl) * per)
    return longest


def build_layout(sessions: list[date], spec: WindowSpec, minimum_embargo: int) -> Layout:
    n = len(sessions)
    if sessions != sorted(set(sessions)):
        raise ValueError("sessions must be strictly increasing")
    embargo = minimum_embargo if spec.embargo_sessions is None else spec.embargo_sessions
    if embargo < minimum_embargo:
        raise ValueError(
            f"embargo_sessions {embargo} is shorter than the longest label horizon "
            f"({minimum_embargo} sessions): labels would overlap across the boundary"
        )
    holdout_len = (
        max(round(n * 0.2), spec.test_sessions)
        if spec.holdout_sessions is None
        else spec.holdout_sessions
    )
    if holdout_len:
        dev_end = n - holdout_len - embargo - 1
        holdout: Window | None = Window(n - holdout_len, n - 1)
    else:
        dev_end = n - 1
        holdout = None
    if dev_end < 0:
        raise ValueError(f"{n} sessions leave no development period before the holdout")
    development = Window(0, dev_end)
    step = spec.step_sessions or spec.test_sessions
    gap_v = embargo if spec.validation_sessions else 0
    folds: list[FoldWindows] = []
    k = 0
    while len(folds) < spec.max_folds:
        if spec.mode is WindowMode.ROLLING:
            t_start = k * step
            t_end = t_start + spec.train_sessions - 1
        else:
            t_start = 0
            t_end = spec.train_sessions - 1 + k * step
        validation = None
        cursor = t_end
        if spec.validation_sessions:
            v_start = cursor + gap_v + 1
            validation = Window(v_start, v_start + spec.validation_sessions - 1)
            cursor = validation.end
        test_start = cursor + embargo + 1
        test = Window(test_start, test_start + spec.test_sessions - 1)
        if test.end > dev_end:
            break
        folds.append(
            FoldWindows(
                index=len(folds),
                train=Window(t_start, t_end),
                validation=validation,
                test=test,
                refit=len(folds) % spec.refit_every == 0,
            )
        )
        k += 1
    if not folds:
        need = spec.train_sessions + gap_v + spec.validation_sessions + embargo + spec.test_sessions
        raise ValueError(
            f"the development period has {dev_end + 1} sessions; one fold needs {need} "
            f"(train {spec.train_sessions}, validation {spec.validation_sessions}, embargo "
            f"{embargo} per boundary, test {spec.test_sessions})"
        )
    layout = Layout(
        sessions=list(sessions),
        embargo=embargo,
        development=development,
        holdout=holdout,
        folds=folds,
        required_embargo=minimum_embargo,
        mode=spec.mode,
    )
    check_layout(layout)
    return layout


def check_layout(layout: Layout) -> None:
    """Independent re-check of every chronological invariant (raises LeakageError)."""
    e = layout.embargo
    if e < layout.required_embargo:
        raise LeakageError("the embargo is shorter than the longest label horizon")
    dev = layout.development
    if layout.holdout is not None:
        if layout.holdout.start - dev.end - 1 < e:
            raise LeakageError("the development period reaches into the holdout embargo")
        if layout.holdout.end != len(layout.sessions) - 1:
            raise LeakageError("the holdout must be the final period")
    previous_test_end = -1
    for f in layout.folds:
        windows = [f.train, *([f.validation] if f.validation else []), f.test]
        for w in windows:
            if w.start < dev.start or w.end > dev.end or w.start > w.end:
                raise LeakageError(f"fold {f.index} leaves the development period")
            if layout.holdout is not None and w.end >= layout.holdout.start:
                raise LeakageError(f"fold {f.index} touches the holdout")
        if f.validation is not None and f.validation.start <= f.train.end:
            raise LeakageError(f"fold {f.index}: validation does not follow training")
        if f.test.start - f.selection_end - 1 < e:
            raise LeakageError(f"fold {f.index}: the test window is inside the embargo")
        if f.test.start <= previous_test_end:
            raise LeakageError("test windows overlap or go back in time")
        previous_test_end = f.test.end


@dataclass(frozen=True)
class PurgeResult:
    kept: list[Any]
    before_window: int  # opened before the window started
    open_at_end: int  # opened in the window but not closed by its last session
    after_window: int  # opened after the window


def purge_trades(trades: list[Any], first: date, last: date) -> PurgeResult:
    """Trades usable as evidence for the window [first, last]: opened (first fill session)
    in it and closed (last session) by its end. ``trades`` need ``first_session`` and
    ``last_session`` (None while open) and ``status``."""
    kept: list[Any] = []
    before = open_end = after = 0
    for t in trades:
        if t.first_session < first:
            before += 1
        elif t.first_session > last:
            after += 1
        elif (
            t.last_session is None
            or t.last_session > last
            or getattr(t.status, "value", t.status) != "CLOSED"
        ):
            open_end += 1
        else:
            kept.append(t)
    return PurgeResult(kept, before, open_end, after)


__all__ = [
    "SESSIONS_PER_PERIOD",
    "FoldWindows",
    "Layout",
    "PurgeResult",
    "Window",
    "WindowSpec",
    "build_layout",
    "check_layout",
    "purge_trades",
    "required_embargo",
]
