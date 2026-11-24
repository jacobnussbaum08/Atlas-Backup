"""X7 V2 (D-259): the pipeline's integrity anchor, held outside the pipeline.

``x7v2_pipeline.py`` cannot vouch for itself: a changed pipeline could also change its own check.
This module holds the pipeline's expected SHA-256 as a constant and refuses unless three things
agree: the pipeline file on disk, this constant, and the ``pre_run_inputs`` entry of
``config/simulation/x7/v2_freeze.json``. Changing the pipeline and its manifest entry together
therefore still fails here. This module's own SHA-256 is recorded in the manifest too.

It is called by the CLI before any pipeline function (``x7v2-backtest``, ``x7v2-verify``), and
again inside the pipeline's ``verify_inputs``, before any data is loaded. It reads only the
manifest and the two source files; it never imports the pipeline.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.research import x7v2_evaluator as ev

PIPELINE = "backend/app/research/x7v2_pipeline.py"
PIPELINE_SHA256 = "037318f93b9abf95f4ccbacca57edc4e0ac79cc49a45ace734dab4c36ea9fe9e"
SELF = "backend/app/research/x7v2_gate.py"


class PipelineIntegrityError(PermissionError):
    """The X7 V2 pipeline is not the recorded one; nothing may run."""


def _sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_pipeline() -> dict[str, Any]:
    """Refuse unless the pipeline equals this anchor and the manifest, and this file its entry."""
    man = json.loads(Path(ev.FREEZE).read_bytes())
    entries: dict[str, Any] = (man.get("pre_run_inputs") or {}).get("files") or {}
    bad: dict[str, Any] = {}
    found = _sha(PIPELINE) if Path(PIPELINE).is_file() else "missing"
    recorded = (entries.get(PIPELINE) or {}).get("sha256")
    if not recorded:
        bad[PIPELINE] = "not recorded in the freeze manifest"
    elif not found == recorded == PIPELINE_SHA256:
        bad[PIPELINE] = {"found": found, "manifest": recorded, "anchor": PIPELINE_SHA256}
    self_found = _sha(SELF)
    self_recorded = (entries.get(SELF) or {}).get("sha256")
    if self_found != self_recorded:
        bad[SELF] = {"found": self_found, "manifest": self_recorded or "not recorded"}
    if bad:
        raise PipelineIntegrityError(f"the X7 V2 pipeline is not the recorded one: {bad}")
    return {"pipeline": found, "anchor": PIPELINE_SHA256, "gate": self_found}
