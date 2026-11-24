"""Run simulation configurations against the isolated simulation database.

    python config/simulation/run.py PATTERN [--jobs 4]

Each configuration runs as its own ``python -m app.backtesting.cli run`` process (the
database must be the simulation database: ``.env.sim``). Completed runs are recorded in
``runs/manifest.json`` and skipped next time.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
MANIFEST = HERE / "runs" / "manifest.json"
LOCK = threading.Lock()


def load() -> dict:
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def run_one(path: Path) -> None:
    key = path.stem
    started = time.time()
    proc = subprocess.run(  # noqa: S603  # our own CLI
        [sys.executable, "-m", "app.backtesting.cli", "run", "--config", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(
        r'created backtest run ([0-9a-f-]{36})|"id": "([0-9a-f-]{36})"', proc.stderr + proc.stdout
    )
    status = re.search(r'"status": "([A-Z_]+)"', proc.stdout)
    entry = {
        "run_id": (match.group(1) or match.group(2)) if match else None,
        "status": status.group(1) if status else f"EXIT_{proc.returncode}",
        "seconds": round(time.time() - started),
    }
    if entry["status"] != "COMPLETED":
        entry["stderr_tail"] = proc.stderr[-1500:]
    with LOCK:
        manifest = load()
        manifest[key] = entry
        MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(key, entry["status"], entry["run_id"], f"{entry['seconds']}s", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pattern")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    done = {k for k, v in load().items() if v.get("status") == "COMPLETED"}
    paths = [p for p in sorted((HERE / "runs").glob(f"{args.pattern}.json")) if p.stem not in done]
    # Longest first: combined runs, then the main period.
    paths.sort(key=lambda p: ("all6" not in p.stem, "main" not in p.stem, p.stem))
    print(f"{len(paths)} configuration(s) to run", flush=True)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(run_one, paths))


if __name__ == "__main__":
    main()
