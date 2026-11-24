"""Write the E2/E3 stage-A 2020-H2 deviation configurations (D-238).

Each ``r2020h2--*.json`` is the original ``a2020h2--*.json`` with only its ``name`` changed
(``sim a2020h2--...`` -> ``sim r2020h2--...``), so the original runs stay recorded under their
own names and the reruns are separate runs. The run's name is not part of its replay hash.
Verifies that nothing else differs and prints both files' SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

RUNS = Path(__file__).parent / "runs"


def main() -> None:
    originals = sorted(RUNS.glob("a2020h2--*.json"))
    assert len(originals) == 15, len(originals)  # noqa: S101
    for path in originals:
        body = json.loads(path.read_text())
        rerun = {**body, "name": body["name"].replace("sim a2020h2--", "sim r2020h2--")}
        assert {k: v for k, v in rerun.items() if k != "name"} == {  # noqa: S101
            k: v for k, v in body.items() if k != "name"
        }
        target = RUNS / path.name.replace("a2020h2--", "r2020h2--")
        target.write_text(json.dumps(rerun, indent=2))
        print(path.name, hashlib.sha256(path.read_bytes()).hexdigest()[:16], "->",
              target.name, hashlib.sha256(target.read_bytes()).hexdigest()[:16])  # fmt: skip


if __name__ == "__main__":
    main()
