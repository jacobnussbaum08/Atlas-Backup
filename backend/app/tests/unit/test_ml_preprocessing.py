"""Phase 5A preprocessing: fitted on training rows only (pure; synthetic TEST data)."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from app.ml_engine.enums import ModelType, TaskKind
from app.ml_engine.features.preprocessing import (
    FittedPreprocessor,
    PreprocessingSpec,
    fit_preprocessor,
    valid_rows,
)


def frame(n: int = 200, seed: int = 0, shift: float = 0.0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    a = rng.normal(shift, 1, n)
    return pl.DataFrame(
        {
            "a": a,
            "twin": a * 2 + 1,  # perfectly correlated with a
            "sparse": [None if i % 3 else float(i) for i in range(n)],  # 2/3 missing
            "const": [1.0] * n,
            "holes": [None if i % 10 == 0 else float(v) for i, v in enumerate(rng.normal(0, 1, n))],
            "label": rng.choice(["UP", "DOWN"], n).tolist(),
            "direction": ["LONG" if i % 2 else "SHORT" for i in range(n)],
        }
    )


NUMERIC = ["a", "twin", "sparse", "const", "holes"]


def fit(
    train: pl.DataFrame, spec: PreprocessingSpec | None = None, model=ModelType.LOGISTIC_REGRESSION
):
    return fit_preprocessor(
        train,
        NUMERIC,
        ["label"],
        spec or PreprocessingSpec(),
        model=model,
        task=TaskKind.CLASSIFICATION,
    )


def test_selection_imputation_and_scaling_use_training_data_only():
    train = frame()
    pre = fit(train)
    assert pre.numeric == ["a", "holes"]
    assert set(pre.dropped) == {"twin", "sparse", "const"}
    assert pre.indicators == ["holes"]
    assert pre.medians["a"] == pytest.approx(float(np.median(train["a"].to_numpy())))
    assert pre.means["a"] == pytest.approx(float(train["a"].to_numpy().mean()))
    assert pre.categorical == {"label": ["DOWN", "UP"], "direction": ["LONG", "SHORT"]}


def test_evaluation_rows_cannot_change_the_fitted_state():
    train = frame()
    before = fit(train).describe()
    # A very different later period: refitting on train alone must give the same state,
    # and transforming the later rows must not alter it.
    test = frame(seed=9, shift=50.0)
    pre = fit(train)
    pre.transform(test)
    assert pre.describe() == before
    x = pre.transform(test)
    assert x[:, 0].mean() > 10  # scaled with the training mean, not re-centred on test


def test_unseen_categories_encode_as_zeros_and_trees_are_not_scaled():
    pre = fit(frame(), model=ModelType.XGBOOST)
    assert not pre.scaled
    unseen = frame(n=5).with_columns(pl.lit("SIDEWAYS").alias("label"))
    x = pre.transform(unseen)
    cols = pre.output_columns
    label_cols = [i for i, c in enumerate(cols) if c.startswith("label=")]
    assert (x[:, label_cols] == 0).all()


def test_description_round_trips():
    pre = fit(frame(), PreprocessingSpec(clip_quantile=0.01))
    again = FittedPreprocessor.from_description(pre.describe())
    data = frame(seed=3)
    assert np.array_equal(pre.transform(data), again.transform(data))


def test_missing_input_column_is_refused():
    pre = fit(frame())
    with pytest.raises(ValueError, match="missing input"):
        pre.transform(frame().drop("a"))


def test_select_k_needs_labels_and_keeps_k():
    train = frame()
    spec = PreprocessingSpec(select_k=1, max_correlation=None)
    with pytest.raises(ValueError, match="labels"):
        fit(train, spec)
    y = (train["a"].to_numpy() > 0).astype(float)
    pre = fit_preprocessor(
        train,
        NUMERIC,
        [],
        spec,
        model=ModelType.RANDOM_FOREST,
        task=TaskKind.CLASSIFICATION,
        label=y,
    )
    assert pre.numeric == ["a"] or pre.numeric == ["twin"]


def test_valid_rows_rejects_unmatured_and_non_finite_rows_only():
    data = pl.DataFrame(
        {
            "label_status": ["MATURED", "IMMATURE", "MATURED", "MATURED"],
            "label_value": [0.1, None, 0.2, 0.3],
            "net_return": [0.1, None, 0.2, 0.3],
            "a": [1.0, 2.0, float("inf"), None],
        }
    )
    keep, reasons = valid_rows(data, ["a"])
    assert keep.to_list() == [True, False, False, True]  # a missing value is imputed, not rejected
    assert reasons["label_not_matured"] == 1
    assert reasons["non_finite_feature"] == 1


def test_direction_interactions_flip_the_sign_for_short_rows():
    pre = fit(frame())
    assert pre.interactions
    data = frame(n=4)
    x = pre.transform(data)
    cols = pre.output_columns
    a, ax = cols.index("a"), cols.index("a__x_direction")
    sign = [-1.0 if d == "SHORT" else 1.0 for d in data["direction"].to_list()]
    assert np.allclose(x[:, ax], x[:, a] * np.array(sign))
    single = fit(frame().filter(pl.col("direction") == "LONG"))
    assert not single.interactions  # one direction: nothing to interact with
