"""Model artifact files (Phase 5A).

An artifact is one joblib file holding the fitted model, the fitted preprocessing, the
calibrator and the input schema. joblib files are pickles and can run code when loaded,
so an artifact is only ever loaded from inside ``ML_ARTIFACT_DIR`` and only if its
sha256 equals the hash recorded in ``ml_model_artifacts`` when it was written. Files are
written once (exclusive create) and never overwritten.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ARTIFACT_FORMAT = "joblib-v1"


class ArtifactError(RuntimeError):
    """The artifact is missing, outside the artifact directory, or altered."""


@dataclass(frozen=True)
class WrittenArtifact:
    path: str  # relative to the artifact root, POSIX separators
    sha256: str
    size_bytes: int


def _resolve(root: Path, relative: str) -> Path:
    base = root.resolve()
    target = (base / relative).resolve()
    if base != target and base not in target.parents:
        raise ArtifactError("artifact path escapes the artifact directory")
    return target


def write_artifact(root: Path, relative: str, payload: dict[str, Any]) -> WrittenArtifact:
    import joblib

    buffer = io.BytesIO()
    joblib.dump(payload, buffer, compress=3)
    data = buffer.getvalue()
    target = _resolve(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:  # never overwrite an artifact
        handle.write(data)
    return WrittenArtifact(relative, hashlib.sha256(data).hexdigest(), len(data))


def read_artifact(root: Path, relative: str, expected_sha256: str) -> dict[str, Any]:
    import joblib

    target = _resolve(root, relative)
    if not target.is_file():
        raise ArtifactError(f"artifact {relative} is missing")
    data = target.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ArtifactError(f"artifact {relative} does not match its recorded sha256")
    payload = joblib.load(io.BytesIO(data))
    if not isinstance(payload, dict) or payload.get("format") != ARTIFACT_FORMAT:
        raise ArtifactError(f"artifact {relative} has an unknown format")
    return payload


__all__ = ["ARTIFACT_FORMAT", "ArtifactError", "WrittenArtifact", "read_artifact", "write_artifact"]
