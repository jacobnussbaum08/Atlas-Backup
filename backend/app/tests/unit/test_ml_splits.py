"""Phase 5A chronological splits, purging, embargo and leakage checks (pure)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from app.ml_engine.validation.splits import (
    LeakageError,
    SplitSpec,
    Timeline,
    assert_disjoint,
    chronological_split,
    cpcv,
    purged_kfold,
    walk_forward,
)

START = datetime(2026, 1, 5, 22)  # noqa: DTZ001 - naive UTC: numpy datetime64 has no zone


def timeline(days: int = 100, assets: int = 3, horizon: int = 5) -> Timeline:
    decision = []
    end = []
    for d in range(days):
        for _ in range(assets):
            decision.append(np.datetime64(START + timedelta(days=d), "us"))
            end.append(np.datetime64(START + timedelta(days=d + horizon), "us"))
    return Timeline(np.array(decision), np.array(end))


def test_split_is_chronological_purged_and_embargoed():
    line = timeline()
    split = chronological_split(line, SplitSpec(), embargo=5)
    train_end = line.label_end[split.train.mask].max()
    assert train_end <= line.decision[split.validation.mask].min()
    assert line.label_end[split.validation.mask].max() <= line.decision[split.test.mask].min()
    assert split.purged["train"] > 0  # labels that would overlap validation were removed
    assert split.embargoed["test"] == 5 * 3  # first 5 decision times x 3 assets
    # Same-time observations of different assets are never on both sides of a boundary.
    for a, b in ((split.train, split.validation), (split.validation, split.test)):
        assert not set(line.decision[a.mask].tolist()) & set(line.decision[b.mask].tolist())


def test_split_never_shuffles():
    line = timeline()
    split = chronological_split(line, SplitSpec(), embargo=0)
    assert line.decision[split.train.mask].max() < line.decision[split.test.mask].min()


def test_walk_forward_trains_only_on_the_past():
    line = timeline()
    dev = np.ones(len(line), dtype=bool)
    folds = walk_forward(line, dev, folds=4, embargo=5)
    assert len(folds) == 4
    for fold in folds:
        assert line.label_end[fold.train].max() <= line.decision[fold.test].min()
    sizes = [int(f.train.sum()) for f in folds]
    assert sizes == sorted(sizes)  # expanding window


def test_purged_kfold_and_cpcv_remove_overlapping_training_labels():
    line = timeline(days=60)
    dev = np.ones(len(line), dtype=bool)
    for folds in (purged_kfold(line, dev, 5, 3), cpcv(line, dev, 6, 2, 3)):
        assert folds
        for fold in folds:
            assert_disjoint(line, fold.train, fold.test, ordered=False)
            assert fold.info["purged"] > 0 or fold.index == 0
    assert len(cpcv(line, dev, 6, 2, 3)) == 15  # C(6, 2)


def test_leakage_is_detected():
    line = timeline(days=20)
    train = np.zeros(len(line), dtype=bool)
    test = np.zeros(len(line), dtype=bool)
    train[:30] = True  # labels end 5 days later
    test[30:] = True
    with pytest.raises(LeakageError):
        assert_disjoint(line, train, test)
    with pytest.raises(LeakageError):
        assert_disjoint(line, train, train)


def test_specs_leave_a_test_period():
    with pytest.raises(ValueError, match="test period"):
        SplitSpec(train_fraction=0.7, validation_fraction=0.3)


def test_labels_must_end_after_their_decision():
    with pytest.raises(ValueError, match="after its decision"):
        Timeline(np.array([np.datetime64(START, "us")]), np.array([np.datetime64(START, "us")]))
