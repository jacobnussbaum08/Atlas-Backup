"""Feature preparation fitted on training data only (Phase 5A).

:func:`fit_preprocessor` learns every statistic it uses from the *training* rows it is
given, and nothing else:

* **selection**: features missing in more than ``max_missing_fraction`` of training rows,
  constant in training, or (optionally) nearly duplicating an earlier feature
  (``max_correlation``) are dropped, with the reason recorded; ``select_k`` keeps the k
  features with the highest mutual information with the training label;
* **clipping** (optional) to training quantiles;
* **imputation**: training medians, plus a missing indicator for every feature that had
  missing training values;
* **scaling**: training mean and standard deviation (STANDARD, or AUTO for linear models);
* **categorical encoding**: one column per category seen in training; unseen or missing
  categories encode as all zeros;
* **direction interactions**: when training holds both LONG and SHORT observations, each
  numeric input is repeated multiplied by the direction's sign, so one model can learn
  that the same move means opposite outcomes for the two directions.

The fitted state is plain data (``describe``), stored with the training run, and applied
unchanged to validation, test and inference rows. Registry feature definitions are never
modified: the outputs are new columns derived from them.

:func:`valid_rows` rejects observations on their own content only (unmatured labels,
non-finite values), so it cannot leak information between rows.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import polars as pl
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from app.ml_engine.enums import ModelType, TaskKind

PREPROCESSING_VERSION = "preprocessing-v1"
MISSING_SUFFIX = "__missing"
INTERACTION_SUFFIX = "__x_direction"
# Context columns a model may use besides registry features.
CONTEXT_COLUMNS = ("direction", "timeframe", "strategy_id")


class PreprocessingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_missing_fraction: float = Field(default=0.5, ge=0, le=1)
    max_correlation: float | None = Field(default=0.98, gt=0, le=1)
    select_k: int | None = Field(default=None, ge=1, le=500)
    clip_quantile: float = Field(default=0.0, ge=0, le=0.1)
    missing_indicators: bool = True
    scale: Literal["AUTO", "STANDARD", "NONE"] = "AUTO"
    # Context columns used as categorical inputs (from CONTEXT_COLUMNS).
    context: tuple[Literal["direction", "timeframe", "strategy_id"], ...] = ("direction",)
    max_categories: int = Field(default=50, ge=2, le=500)
    # Add each numeric input multiplied by the direction's sign (+1 LONG, -1 SHORT): the
    # same move means opposite outcomes for the two directions, which an additive model
    # cannot express with a direction indicator alone.
    direction_interactions: bool = True


@dataclass
class FittedPreprocessor:
    numeric: list[str]  # kept numeric inputs, in order
    categorical: dict[str, list[str]]  # kept categorical inputs -> training categories
    medians: dict[str, float]
    clip: dict[str, tuple[float, float]]
    indicators: list[str]  # numeric inputs with a missing indicator
    means: dict[str, float]
    stds: dict[str, float]
    scaled: bool
    dropped: dict[str, str] = field(default_factory=dict)  # input -> reason
    interactions: bool = False  # numeric inputs x direction sign (needs a direction column)
    training_rows: int = 0
    version: str = PREPROCESSING_VERSION

    @property
    def inputs(self) -> list[str]:
        return [*self.numeric, *self.categorical]

    @property
    def output_columns(self) -> list[str]:
        columns = list(self.numeric)
        if self.interactions:
            columns += [f"{c}{INTERACTION_SUFFIX}" for c in self.numeric]
        columns += [f"{c}{MISSING_SUFFIX}" for c in self.indicators]
        for name, categories in self.categorical.items():
            columns += [f"{name}={value}" for value in categories]
        return columns

    def transform(self, frame: pl.DataFrame) -> NDArray[np.float64]:
        missing = [c for c in self.inputs if c not in frame.columns]
        if self.interactions and "direction" not in frame.columns:
            missing.append("direction")
        if missing:
            raise ValueError(f"missing input column(s): {', '.join(missing)}")
        n = frame.height
        parts: list[NDArray[np.float64]] = []
        raw: dict[str, NDArray[np.float64]] = {}
        for column in self.numeric:
            values = frame[column].cast(pl.Float64).to_numpy().astype(np.float64, copy=True)
            raw[column] = values
            nan = ~np.isfinite(values)
            values[nan] = self.medians[column]
            if column in self.clip:
                low, high = self.clip[column]
                values = np.clip(values, low, high)
            if self.scaled:
                values = (values - self.means[column]) / self.stds[column]
            parts.append(values.reshape(n, 1))
        if self.interactions:
            sign = np.where(
                np.array(frame["direction"].cast(pl.String).to_list()) == "SHORT", -1.0, 1.0
            )
            parts.extend((p[:, 0] * sign).reshape(n, 1) for p in list(parts))
        for column in self.indicators:
            parts.append((~np.isfinite(raw[column])).astype(np.float64).reshape(n, 1))
        for column, categories in self.categorical.items():
            labels = frame[column].cast(pl.String).to_list()
            for category in categories:
                parts.append(
                    np.array([v == category for v in labels], dtype=np.float64).reshape(n, 1)
                )
        if not parts:
            return np.zeros((n, 0), dtype=np.float64)
        return np.hstack(parts)

    def describe(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "fitted_on": "TRAIN",
            "training_rows": self.training_rows,
            "numeric": self.numeric,
            "categorical": self.categorical,
            "medians": self.medians,
            "clip": {k: list(v) for k, v in self.clip.items()},
            "missing_indicators": self.indicators,
            "means": self.means,
            "stds": self.stds,
            "scaled": self.scaled,
            "interactions": self.interactions,
            "dropped": self.dropped,
            "output_columns": self.output_columns,
        }

    @classmethod
    def from_description(cls, data: Mapping[str, Any]) -> FittedPreprocessor:
        return cls(
            numeric=list(data["numeric"]),
            categorical={k: list(v) for k, v in data["categorical"].items()},
            medians={k: float(v) for k, v in data["medians"].items()},
            clip={k: (float(v[0]), float(v[1])) for k, v in data["clip"].items()},
            indicators=list(data["missing_indicators"]),
            means={k: float(v) for k, v in data["means"].items()},
            stds={k: float(v) for k, v in data["stds"].items()},
            scaled=bool(data["scaled"]),
            interactions=bool(data.get("interactions", False)),
            dropped=dict(data["dropped"]),
            training_rows=int(data["training_rows"]),
            version=str(data["version"]),
        )


def valid_rows(frame: pl.DataFrame, numeric: Sequence[str]) -> tuple[pl.Series, dict[str, int]]:
    """Rows usable for training or evaluation, judged on each row alone."""
    matured = frame["label_status"] == "MATURED"
    has_label = frame["label_value"].is_not_null() & frame["label_value"].is_finite()
    has_outcome = frame["net_return"].is_not_null()
    finite = pl.Series([True] * frame.height)
    for column in numeric:
        series = frame[column]
        # null is missing (allowed, imputed); +/-inf or NaN is an invalid value.
        finite = finite & (series.is_null() | series.is_finite()).fill_null(True)
    keep = matured & has_label.fill_null(False) & has_outcome & finite
    reasons = {
        "label_not_matured": int((~matured).sum()),
        "label_missing": int((matured & ~has_label.fill_null(False)).sum()),
        "non_finite_feature": int((matured & ~finite).sum()),
    }
    return keep, reasons


def _scaled(spec: PreprocessingSpec, model: ModelType) -> bool:
    if spec.scale == "AUTO":
        return model is ModelType.LOGISTIC_REGRESSION
    return spec.scale == "STANDARD"


def fit_preprocessor(
    train: pl.DataFrame,
    numeric: Sequence[str],
    categorical: Sequence[str],
    spec: PreprocessingSpec,
    *,
    model: ModelType,
    task: TaskKind,
    label: NDArray[np.float64] | None = None,
    seed: int = 0,
) -> FittedPreprocessor:
    """Fit on ``train`` only. ``label`` (training labels) is needed for ``select_k``."""
    if train.height == 0:
        raise ValueError("cannot fit preprocessing on zero training rows")
    dropped: dict[str, str] = {}
    kept: list[str] = []
    for column in numeric:
        values = train[column].cast(pl.Float64).to_numpy()
        finite = values[np.isfinite(values)]
        missing = 1 - finite.size / values.size
        if missing > spec.max_missing_fraction:
            dropped[column] = f"missing in {missing:.0%} of training rows"
        elif finite.size == 0 or np.unique(finite).size < 2:
            dropped[column] = "constant in training"
        else:
            kept.append(column)
    medians = {c: float(np.nanmedian(train[c].cast(pl.Float64).to_numpy())) for c in kept}

    if spec.max_correlation is not None and len(kept) > 1:
        filled = np.column_stack(
            [_filled(train[c].cast(pl.Float64).to_numpy(), medians[c]) for c in kept]
        )
        corr: NDArray[np.float64] = np.atleast_2d(np.corrcoef(filled, rowvar=False))
        survivors: list[int] = []
        for i, column in enumerate(kept):
            twin = next((j for j in survivors if abs(corr[i, j]) > spec.max_correlation), None)
            if twin is None:
                survivors.append(i)
            else:
                dropped[column] = f"correlation {corr[i, twin]:.3f} with {kept[twin]} in training"
        kept = [kept[i] for i in survivors]

    categories: dict[str, list[str]] = {}
    for column in [*categorical, *spec.context]:
        if column not in train.columns:
            continue
        seen = train[column].cast(pl.String).drop_nulls().unique().sort().to_list()
        if len(seen) < 2:
            dropped[column] = "fewer than two categories in training"
        elif len(seen) > spec.max_categories:
            dropped[column] = f"{len(seen)} categories in training (limit {spec.max_categories})"
        else:
            categories[column] = [str(v) for v in seen]

    if spec.select_k is not None and len(kept) > spec.select_k:
        if label is None:
            raise ValueError("select_k needs the training labels")
        kept = _select_k(train, kept, medians, label, spec.select_k, task, seed, dropped)

    medians = {c: medians[c] for c in kept}
    clip: dict[str, tuple[float, float]] = {}
    if spec.clip_quantile > 0:
        for column in kept:
            values = _filled(train[column].cast(pl.Float64).to_numpy(), medians[column])
            q = spec.clip_quantile
            clip[column] = (float(np.quantile(values, q)), float(np.quantile(values, 1 - q)))
    indicators = (
        [c for c in kept if train[c].cast(pl.Float64).is_null().any()]
        if spec.missing_indicators
        else []
    )
    scaled = _scaled(spec, model)
    means: dict[str, float] = {}
    stds: dict[str, float] = {}
    for column in kept:
        values = _filled(train[column].cast(pl.Float64).to_numpy(), medians[column])
        if column in clip:
            values = np.clip(values, *clip[column])
        means[column] = float(values.mean())
        sd = float(values.std())
        stds[column] = sd if sd > 0 and math.isfinite(sd) else 1.0
    directions = (
        set(train["direction"].cast(pl.String).drop_nulls().unique().to_list())
        if "direction" in train.columns
        else set()
    )
    return FittedPreprocessor(
        interactions=spec.direction_interactions and len(directions) > 1,
        numeric=kept,
        categorical=categories,
        medians=medians,
        clip=clip,
        indicators=indicators,
        means=means,
        stds=stds,
        scaled=scaled,
        dropped=dropped,
        training_rows=train.height,
    )


def _filled(values: NDArray[Any], median: float) -> NDArray[np.float64]:
    out = values.astype(np.float64, copy=True)
    out[~np.isfinite(out)] = median
    return out


def _select_k(
    train: pl.DataFrame,
    kept: list[str],
    medians: dict[str, float],
    label: NDArray[np.float64],
    k: int,
    task: TaskKind,
    seed: int,
    dropped: dict[str, str],
) -> list[str]:
    from sklearn.feature_selection import mutual_info_classif, mutual_info_regression

    matrix = np.column_stack(
        [_filled(train[c].cast(pl.Float64).to_numpy(), medians[c]) for c in kept]
    )
    if task is TaskKind.CLASSIFICATION:
        scores = mutual_info_classif(matrix, label.astype(int), random_state=seed)
    else:
        scores = mutual_info_regression(matrix, label, random_state=seed)
    order = sorted(range(len(kept)), key=lambda i: (-float(scores[i]), kept[i]))
    chosen = sorted(order[:k])
    for i in order[k:]:
        dropped[kept[i]] = f"not among the top {k} by training mutual information"
    return [kept[i] for i in chosen]


__all__ = [
    "CONTEXT_COLUMNS",
    "INTERACTION_SUFFIX",
    "MISSING_SUFFIX",
    "PREPROCESSING_VERSION",
    "FittedPreprocessor",
    "PreprocessingSpec",
    "fit_preprocessor",
    "valid_rows",
]
