"""Chronological splits with purging and embargo (Phase 5A, spec 18.2).

Observations are never shuffled. Every split is defined on **decision times**, so all
assets observed at the same time fall on the same side of a boundary. Each observation
spans an interval: from its decision time to its label's end (when its outcome settled).

* **Purge.** An earlier-segment observation whose label ends after a later segment's first
  decision time overlaps it and is removed (its outcome would be learned from the period
  being evaluated).
* **Embargo.** The first ``embargo`` decision times of every later segment are skipped as
  well, so serially correlated features straddling a boundary do not connect the two.
  The default is the longest label horizon in periods.
* **Final test.** The last segment (``test_fraction``) is the untouched test period: never
  used for fitting, preprocessing, calibration, model or hyperparameter selection; it is
  evaluated once per run.

Walk-forward folds (the primary evaluation) and the supplementary purged k-fold and
combinatorial purged cross-validation (CPCV, De Prado 2018) use only the development
period (train + validation). :func:`assert_disjoint` re-checks every split before use.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

Mask = NDArray[np.bool_]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class LeakageError(AssertionError):
    """A split would let training data overlap the period it is evaluated on."""


class SplitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    train_fraction: float = Field(default=0.6, gt=0, lt=1)
    validation_fraction: float = Field(default=0.2, gt=0, lt=1)
    # None: the longest label horizon (in periods) in the data.
    embargo_periods: int | None = Field(default=None, ge=0, le=260)
    walk_forward_folds: int = Field(default=4, ge=2, le=20)
    purged_kfold: int = Field(default=0, ge=0, le=20)  # 0: not run
    cpcv_groups: int = Field(default=0, ge=0, le=12)  # 0: not run
    cpcv_test_groups: int = Field(default=2, ge=1, le=6)

    @model_validator(mode="after")
    def _fractions(self) -> SplitSpec:
        if self.train_fraction + self.validation_fraction >= 1:
            raise ValueError("train and validation fractions must leave a test period")
        if self.cpcv_groups and self.cpcv_test_groups >= self.cpcv_groups:
            raise ValueError("cpcv_test_groups must be fewer than cpcv_groups")
        return self

    @property
    def test_fraction(self) -> float:
        return 1 - self.train_fraction - self.validation_fraction


@dataclass(frozen=True)
class Segment:
    mask: Mask
    start: datetime | None
    end: datetime | None

    @property
    def size(self) -> int:
        return int(self.mask.sum())

    def describe(self) -> dict[str, Any]:
        return {
            "observations": self.size,
            "start": None if self.start is None else self.start.isoformat(),
            "end": None if self.end is None else self.end.isoformat(),
        }


@dataclass(frozen=True)
class Split:
    train: Segment
    validation: Segment
    test: Segment
    embargo: int
    purged: dict[str, int]
    embargoed: dict[str, int]

    def describe(self) -> dict[str, Any]:
        return {
            "train": self.train.describe(),
            "validation": self.validation.describe(),
            "test": self.test.describe(),
            "embargo_periods": self.embargo,
            "purged": self.purged,
            "embargoed": self.embargoed,
            "method": "chronological by decision time; purge on label overlap; embargo at "
            "the start of each later segment; final segment untouched",
        }


@dataclass(frozen=True)
class Fold:
    index: int
    train: Mask
    test: Mask
    info: dict[str, Any]


class Timeline:
    """Decision times and label ends of the usable observations."""

    def __init__(self, decision: NDArray[Any], label_end: NDArray[Any]) -> None:
        self.decision = decision.astype("datetime64[us]")
        self.label_end = label_end.astype("datetime64[us]")
        if self.decision.shape != self.label_end.shape:
            raise ValueError("decision and label_end must have the same length")
        if np.isnat(self.label_end).any():
            raise ValueError("every usable observation needs a label end")
        if (self.label_end <= self.decision).any():
            raise ValueError("a label must end after its decision time")
        self.times = np.unique(self.decision)

    def __len__(self) -> int:
        return int(self.decision.size)

    def index_of(self) -> NDArray[np.int64]:
        """For each observation, the position of its decision time in ``times``."""
        return np.searchsorted(self.times, self.decision).astype(np.int64)

    def when(self, mask: Mask) -> tuple[datetime | None, datetime | None]:
        if not mask.any():
            return None, None
        return _dt(self.decision[mask].min()), _dt(self.decision[mask].max())


def _dt(value: np.datetime64) -> datetime:
    micros = int(np.datetime64(value, "us").astype(np.int64))
    return EPOCH + timedelta(microseconds=micros)


def _purge_before(line: Timeline, earlier: Mask, later_start_index: int) -> tuple[Mask, int]:
    """Remove ``earlier`` observations whose label ends after the later segment begins."""
    if later_start_index >= line.times.size:
        return earlier, 0
    boundary = line.times[later_start_index]
    overlapping = earlier & (line.label_end > boundary)
    return earlier & ~overlapping, int(overlapping.sum())


def _embargo_after(line: Timeline, later: Mask, start_index: int, embargo: int) -> tuple[Mask, int]:
    """Skip the first ``embargo`` decision times of a later segment."""
    if embargo <= 0:
        return later, 0
    idx = line.index_of()
    skipped = later & (idx < start_index + embargo)
    return later & ~skipped, int(skipped.sum())


def chronological_split(line: Timeline, spec: SplitSpec, embargo: int) -> Split:
    n_times = line.times.size
    if n_times < 10:
        raise ValueError(f"only {n_times} distinct decision times; at least 10 are needed")
    v_start = max(1, round(n_times * spec.train_fraction))
    t_start = max(v_start + 1, round(n_times * (spec.train_fraction + spec.validation_fraction)))
    if t_start >= n_times:
        raise ValueError("no decision times left for the test period")
    idx = line.index_of()
    train = idx < v_start
    validation = (idx >= v_start) & (idx < t_start)
    test = idx >= t_start
    purged: dict[str, int] = {}
    embargoed: dict[str, int] = {}
    train, purged["train"] = _purge_before(line, train, v_start)
    validation, purged["validation"] = _purge_before(line, validation, t_start)
    # A training label must not reach into the test period either.
    train, extra = _purge_before(line, train, t_start)
    purged["train"] += extra
    validation, embargoed["validation"] = _embargo_after(line, validation, v_start, embargo)
    test, embargoed["test"] = _embargo_after(line, test, t_start, embargo)
    split = Split(
        train=Segment(train, *line.when(train)),
        validation=Segment(validation, *line.when(validation)),
        test=Segment(test, *line.when(test)),
        embargo=embargo,
        purged=purged,
        embargoed=embargoed,
    )
    assert_disjoint(line, train, validation)
    assert_disjoint(line, train, test)
    assert_disjoint(line, validation, test)
    return split


def walk_forward(line: Timeline, development: Mask, folds: int, embargo: int) -> list[Fold]:
    """Expanding-window folds over the development period: fold k trains on every block
    before block k and tests on block k (purged and embargoed)."""
    idx = line.index_of()
    dev_times = np.unique(idx[development])
    blocks = np.array_split(dev_times, folds + 1)
    result: list[Fold] = []
    for k in range(1, folds + 1):
        if blocks[k].size == 0:
            continue
        start = int(blocks[k][0])
        end = int(blocks[k][-1])
        train = development & (idx < start)
        test = development & (idx >= start) & (idx <= end)
        train, purged = _purge_before(line, train, start)
        test, skipped = _embargo_after(line, test, start, embargo)
        if not train.any() or not test.any():
            continue
        assert_disjoint(line, train, test)
        result.append(
            Fold(
                index=k,
                train=train,
                test=test,
                info={
                    "train_end": _iso(line.when(train)[1]),
                    "test_start": _iso(line.when(test)[0]),
                    "test_end": _iso(line.when(test)[1]),
                    "purged": purged,
                    "embargoed": skipped,
                },
            )
        )
    return result


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _purge_around(line: Timeline, train: Mask, test: Mask, embargo: int) -> tuple[Mask, int, int]:
    """Purged k-fold rule: drop training observations whose label interval overlaps any
    test observation's interval, and those decided within ``embargo`` times after a test
    block ends."""
    idx = line.index_of()
    test_idx = np.unique(idx[test])
    # Contiguous runs of test times (CPCV may test several separate groups).
    runs = np.split(test_idx, np.where(np.diff(test_idx) != 1)[0] + 1)
    purged = np.zeros_like(train)
    embargoed = np.zeros_like(train)
    for run in runs:
        if run.size == 0:
            continue
        lo = line.times[run[0]]
        run_mask = test & (idx >= run[0]) & (idx <= run[-1])
        hi_end = line.label_end[run_mask].max()
        overlap = train & (line.decision < hi_end) & (line.label_end > lo)
        purged |= overlap
        if embargo > 0:
            after = train & (idx > run[-1]) & (idx <= run[-1] + embargo)
            embargoed |= after & ~overlap
    kept = train & ~purged & ~embargoed
    return kept, int(purged.sum()), int(embargoed.sum())


def purged_kfold(line: Timeline, development: Mask, k: int, embargo: int) -> list[Fold]:
    idx = line.index_of()
    blocks = np.array_split(np.unique(idx[development]), k)
    result: list[Fold] = []
    for i, block in enumerate(blocks):
        if block.size == 0:
            continue
        test = development & np.isin(idx, block)
        train, purged, embargoed = _purge_around(line, development & ~test, test, embargo)
        if not train.any() or not test.any():
            continue
        assert_disjoint(line, train, test, ordered=False)
        result.append(
            Fold(i, train, test, {"test_block": i, "purged": purged, "embargoed": embargoed})
        )
    return result


def cpcv(
    line: Timeline, development: Mask, groups: int, test_groups: int, embargo: int
) -> list[Fold]:
    """Combinatorial purged cross-validation: every choice of ``test_groups`` of the
    ``groups`` contiguous blocks is a test set; the rest (purged, embargoed) trains."""
    idx = line.index_of()
    blocks = np.array_split(np.unique(idx[development]), groups)
    result: list[Fold] = []
    for n, combo in enumerate(itertools.combinations(range(groups), test_groups)):
        chosen = np.concatenate([blocks[g] for g in combo])
        test = development & np.isin(idx, chosen)
        train, purged, embargoed = _purge_around(line, development & ~test, test, embargo)
        if not train.any() or not test.any():
            continue
        assert_disjoint(line, train, test, ordered=False)
        result.append(
            Fold(
                n,
                train,
                test,
                {"test_groups": list(combo), "purged": purged, "embargoed": embargoed},
            )
        )
    return result


def assert_disjoint(line: Timeline, train: Mask, test: Mask, *, ordered: bool = True) -> None:
    """Raise if any training observation overlaps the evaluated observations.

    ``ordered``: training must also precede (its labels end by the first evaluated decision
    time). Otherwise no training label interval may intersect a test label interval.
    """
    if (train & test).any():
        raise LeakageError("an observation is in both training and evaluation")
    if not train.any() or not test.any():
        return
    if ordered:
        first = line.decision[test].min()
        if line.label_end[train].max() > first:
            raise LeakageError("a training label ends after the evaluation period begins")
        return
    t_start, t_end = line.decision[test], line.label_end[test]
    for start, end in zip(line.decision[train], line.label_end[train], strict=True):
        if ((t_start < end) & (t_end > start)).any():
            raise LeakageError("a training label interval overlaps an evaluation interval")


__all__ = [
    "Fold",
    "LeakageError",
    "Segment",
    "Split",
    "SplitSpec",
    "Timeline",
    "assert_disjoint",
    "chronological_split",
    "cpcv",
    "purged_kfold",
    "walk_forward",
]
