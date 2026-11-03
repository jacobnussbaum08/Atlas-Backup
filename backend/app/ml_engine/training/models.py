"""Model factories with validated hyperparameters (Phase 5A).

Three model families, simplest first (spec 12.2):

* LOGISTIC_REGRESSION: L2-regularized logistic regression for classification targets;
  for regression targets its linear counterpart, ridge regression.
* RANDOM_FOREST: scikit-learn random forests.
* XGBOOST: gradient-boosted trees (``hist`` method, CPU).

Every model receives the run's random seed; with ``n_jobs = 1`` training is bit-for-bit
reproducible for the same data and library versions. No neural networks are offered:
nothing has shown they improve out-of-sample results here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ml_engine.enums import ModelType, TaskKind


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LogisticParams(_Params):
    C: float = Field(default=1.0, gt=0, le=1000)  # inverse regularization (classification)
    alpha: float = Field(default=1.0, ge=0, le=1000)  # ridge penalty (regression)
    class_weight: Literal["balanced"] | None = None
    max_iter: int = Field(default=2000, ge=100, le=100_000)


class ForestParams(_Params):
    n_estimators: int = Field(default=300, ge=10, le=2000)
    max_depth: int | None = Field(default=6, ge=1, le=64)
    min_samples_leaf: int = Field(default=50, ge=1, le=10_000)
    max_features: float = Field(default=0.5, gt=0, le=1)
    class_weight: Literal["balanced", "balanced_subsample"] | None = None


class XGBoostParams(_Params):
    n_estimators: int = Field(default=300, ge=10, le=5000)
    max_depth: int = Field(default=3, ge=1, le=16)
    learning_rate: float = Field(default=0.05, gt=0, le=1)
    subsample: float = Field(default=0.8, gt=0, le=1)
    colsample_bytree: float = Field(default=0.8, gt=0, le=1)
    min_child_weight: float = Field(default=20.0, ge=0, le=10_000)
    reg_lambda: float = Field(default=1.0, ge=0, le=1000)


PARAMS: dict[ModelType, type[_Params]] = {
    ModelType.LOGISTIC_REGRESSION: LogisticParams,
    ModelType.RANDOM_FOREST: ForestParams,
    ModelType.XGBOOST: XGBoostParams,
}


def parse_params(model: ModelType, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and complete hyperparameters; every field explicit in the result."""
    return PARAMS[model].model_validate(raw).model_dump(mode="json")


def build_model(
    model: ModelType, task: TaskKind, params: dict[str, Any], *, seed: int, n_jobs: int
) -> Any:
    p = PARAMS[model].model_validate(params)
    classification = task is TaskKind.CLASSIFICATION
    if isinstance(p, LogisticParams):
        if classification:
            from sklearn.linear_model import LogisticRegression

            return LogisticRegression(
                C=p.C, class_weight=p.class_weight, max_iter=p.max_iter, random_state=seed
            )
        from sklearn.linear_model import Ridge

        return Ridge(alpha=p.alpha, random_state=seed)
    if isinstance(p, ForestParams):
        common = {
            "n_estimators": p.n_estimators,
            "max_depth": p.max_depth,
            "min_samples_leaf": p.min_samples_leaf,
            "max_features": p.max_features,
            "random_state": seed,
            "n_jobs": n_jobs,
        }
        if classification:
            from sklearn.ensemble import RandomForestClassifier

            return RandomForestClassifier(class_weight=p.class_weight, **common)
        from sklearn.ensemble import RandomForestRegressor

        return RandomForestRegressor(**common)
    if isinstance(p, XGBoostParams):
        xgb_common = {
            "n_estimators": p.n_estimators,
            "max_depth": p.max_depth,
            "learning_rate": p.learning_rate,
            "subsample": p.subsample,
            "colsample_bytree": p.colsample_bytree,
            "min_child_weight": p.min_child_weight,
            "reg_lambda": p.reg_lambda,
            "tree_method": "hist",
            "random_state": seed,
            "n_jobs": n_jobs,
        }
        if classification:
            from xgboost import XGBClassifier

            return XGBClassifier(objective="binary:logistic", eval_metric="logloss", **xgb_common)
        from xgboost import XGBRegressor

        return XGBRegressor(objective="reg:squarederror", **xgb_common)
    raise ValueError(f"unsupported model {model}")


def describe_family(model: ModelType, task: TaskKind) -> str:
    if model is ModelType.LOGISTIC_REGRESSION:
        return (
            "logistic regression (L2)"
            if task is TaskKind.CLASSIFICATION
            else "ridge regression (linear baseline for a regression target)"
        )
    return {
        ModelType.RANDOM_FOREST: "random forest",
        ModelType.XGBOOST: "gradient-boosted trees (XGBoost, hist)",
    }[model]


__all__ = [
    "PARAMS",
    "ForestParams",
    "LogisticParams",
    "XGBoostParams",
    "build_model",
    "describe_family",
    "parse_params",
]
