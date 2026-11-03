"""Code and library versions recorded with every dataset version and training run.

``ATLAS_CODE_VERSION`` (set when an image is built) wins; otherwise the git commit of the
working tree is read, with ``dirty`` set when uncommitted changes exist. Library versions
matter as much as code: a model trained with one scikit-learn or XGBoost release is not
guaranteed to load or predict identically under another.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from app.ml_engine.config import REPO_ROOT

# Bumped whenever dataset building, labelling, preprocessing, splitting, training or
# evaluation changes behaviour; recorded with every result.
ML_ENGINE_VERSION = "ml-engine-v1"

LIBRARIES = ("numpy", "polars", "scikit-learn", "xgboost", "xgboost-cpu", "scipy", "joblib")


def _git(*args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [git, *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def library_versions() -> dict[str, str]:
    found: dict[str, str] = {"python": platform.python_version()}
    for name in LIBRARIES:
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            continue
    return found


@lru_cache(maxsize=1)
def _commit() -> tuple[str, bool | None]:
    configured = os.environ.get("ATLAS_CODE_VERSION")
    if configured:
        return configured, None
    commit = _git("rev-parse", "HEAD")
    if commit is None:
        return "unknown", None
    status = _git("status", "--porcelain", "--untracked-files=no")
    return commit, None if status is None else bool(status)


def code_version() -> dict[str, Any]:
    commit, dirty = _commit()
    return {
        "ml_engine": ML_ENGINE_VERSION,
        "commit": commit,
        "dirty": dirty,
        "libraries": library_versions(),
    }


__all__ = ["ML_ENGINE_VERSION", "code_version", "library_versions"]
