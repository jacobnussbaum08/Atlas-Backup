"""Machine-learning engine settings (``ML_*``, Phase 5A).

Operational knobs only. What a dataset contains, how a target is defined and which
hyperparameters a run uses are not settings: they are validated specifications stored
with the dataset version or training run they produced.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config.settings import (
    ENV_FILE,
    InvalidSettingsError,
    describe_validation_error,
    reject_unknown_keys,
)

ENV_PREFIX = "ML_"
REPO_ROOT = Path(__file__).resolve().parents[3]


class MLSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # Where model artifacts are written. Relative paths resolve against the repository
    # root; the registry stores paths relative to this directory plus each file's sha256.
    artifact_dir: Path = Path("data/ml/artifacts")
    # Upper bound on the observations one dataset version may hold.
    max_observations: int = Field(default=250_000, ge=100, le=5_000_000)
    # Threads a model may use. 1 keeps training bit-for-bit reproducible.
    n_jobs: int = Field(default=1, ge=1, le=64)

    def artifact_root(self) -> Path:
        root = self.artifact_dir
        return root if root.is_absolute() else REPO_ROOT / root


@lru_cache(maxsize=1)
def get_ml_settings() -> MLSettings:
    """Load once. Unknown ``ML_*`` keys are typos; errors never include values."""
    known = {f"{ENV_PREFIX}{name}".upper() for name in MLSettings.model_fields}
    reject_unknown_keys((ENV_PREFIX,), known)
    try:
        return MLSettings()
    except ValidationError as exc:
        raise InvalidSettingsError(describe_validation_error(exc, "ML settings")) from None


__all__ = ["MLSettings", "get_ml_settings"]
