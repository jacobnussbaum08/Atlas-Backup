"""E2/E3 stage A, declared deviation D-238: 2020-H2 rerun with repaired lot sizes (read only).

* The fixed evaluator (``e2e3_evaluate.py``, unchanged; its SHA-256 is checked here) is
  applied through its own functions to the window set (r2020h2, a2021h1, a2021h2): the
  2021 windows are the original runs, 2020-H2 is the rerun. This is the *deviation view*;
  the original verdicts (``e2e3_stage_a_results.json``) are kept and shown beside it.
* Descriptive statistics of the 2020-H2 reruns alone, their artefacts and hashes, and the
  refusal codes (to confirm lot sizes no longer block entries).

Writes runs/e2e3_deviation_2020h2.json.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import e2e3_evaluate as ev
from analyze import MANIFEST, spy_regimes
from diagnose import load_closes, run_id
from e2e3_stage_a_report import PORTFOLIO, equity_stats, summary, trade_rows
from sqlalchemy import select

from app.core.config import get_settings
from app.database.models import (
    BacktestEquitySnapshotRecord,
    BacktestMetricRecord,
    BacktestRejectionRecord,
    BacktestRunEventRecord,
    BacktestRunRecord,
)
from app.database.sessions import build_engine, build_session_factory

HERE = Path(__file__).parent
OUT = MANIFEST.parent / "e2e3_deviation_2020h2.json"
EVALUATOR_SHA256 = "c7953f634b68f2fef955dae6269bdbf3e34ce40d0b529618051d6877d1f7ae0f"
REPAIRED_WINDOWS = ("r2020h2", "a2021h1", "a2021h2")
TESTS = [("e2", "momentum"), ("e2", "breakout"), ("e2", "trend_following"), ("e3", "momentum")]
CONTROL = ("momentum", "breakout", "trend_following")


def rerun_keys() -> list[str]:
    out = []
    for exp, fam in TESTS:
        out += [f"r2020h2--{exp}-{v}--{fam}" for v in ev.VARIANTS]
    return out + [f"r2020h2--control-primary--{f}" for f in CONTROL]


def main() -> None:
    sha = hashlib.sha256((HERE / "e2e3_evaluate.py").read_bytes()).hexdigest()
    if sha != EVALUATOR_SHA256:
        raise SystemExit(f"the evaluator changed: {sha}")
    manifest = json.loads(MANIFEST.read_text())
    original = json.loads((MANIFEST.parent / "e2e3_stage_a_results.json").read_text())
    factory = build_session_factory(build_engine(get_settings()))
    out: dict[str, Any] = {
        "evaluator_sha256": sha,
        "preregistration_sha256": hashlib.sha256(ev.PREREG.read_bytes()).hexdigest(),
        "artifacts": {},
        "per_config_2020h2": {},
        "pooled_2020h2": {},
        "deviation_view": {},
        "original_verdicts": {k: v["verdict"] for k, v in original["tests"].items()},
    }
    with factory() as session:
        closes = load_closes(session)
        regimes = spy_regimes(session, "sip")
        rows_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        curves: dict[str, list[dict[Any, float]]] = defaultdict(list)
        refusals: Counter[str] = Counter()
        for key in rerun_keys():
            entry = manifest.get(key, {})
            if entry.get("status") != "COMPLETED":
                out["artifacts"][key] = {"status": entry.get("status", "NOT_RUN"), **entry}
                continue
            rid = run_id(session, manifest, key)
            run = session.get(BacktestRunRecord, rid)
            original_key = key.replace("r2020h2--", "a2020h2--")
            orig = session.get(BacktestRunRecord, run_id(session, manifest, original_key))
            out["artifacts"][key] = {
                "run_id": str(rid),
                "status": run.status.value,
                "engine_version": run.engine_version,
                "execution_conventions": run.config.get("execution_conventions"),
                "execution_model": f"{run.config['execution_model']['code']} "
                f"v{run.config['execution_model']['version']}",
                "execution_model_sha256": run.execution_model_sha256,
                "config_sha256": run.config_sha256,
                "request_file_sha256": hashlib.sha256(
                    (MANIFEST.parent / f"{key}.json").read_bytes()
                ).hexdigest(),
                "data": f"{run.data_provider}/{run.data_feed}",
                "data_known_at": run.data_known_at.isoformat(),
                "dataset_sha256": run.dataset_sha256,
                "result_sha256": run.result_sha256,
                "original_run_id": str(orig.id),
                "original_result_sha256": orig.result_sha256,
                "original_dataset_sha256": orig.dataset_sha256,
            }
            for r in session.scalars(
                select(BacktestRejectionRecord).where(BacktestRejectionRecord.run_id == rid)
            ):
                refusals[f"{r.stage}:{r.code}"] += 1
            metrics = {
                m.metric: (None if m.value is None else float(m.value))
                for m in session.scalars(
                    select(BacktestMetricRecord).where(
                        BacktestMetricRecord.run_id == rid,
                        BacktestMetricRecord.scope == "PORTFOLIO",
                    )
                )
                if m.metric in PORTFOLIO
            }
            rows = trade_rows(session, rid, regimes)
            curve = {
                s.session: float(s.equity)
                for s in session.scalars(
                    select(BacktestEquitySnapshotRecord).where(
                        BacktestEquitySnapshotRecord.run_id == rid
                    )
                )
            }
            summary_event = next(
                (
                    e.details
                    for e in session.scalars(
                        select(BacktestRunEventRecord).where(
                            BacktestRunEventRecord.run_id == rid,
                            BacktestRunEventRecord.kind == "PIPELINE_SUMMARY",
                        )
                    )
                ),
                {},
            )
            out["per_config_2020h2"][key] = {
                "portfolio_metrics": metrics,
                "trades": summary(rows),
                "equity_curve": equity_stats([curve]),
                "dividends": (summary_event or {}).get("dividends"),
                "spread_fills": ((summary_event or {}).get("spreads") or {}).get("fills"),
                "corrected_fills": ((summary_event or {}).get("execution_fidelity") or {}).get(
                    "corrected_fills"
                ),
            }
            test = key.split("--")[1]
            fam = key.split("--")[2]
            rows_by[f"{test}:{fam}"] += rows
            curves[test].append(curve)
        out["refusals_2020h2"] = dict(refusals.most_common())
        for k, rows in sorted(rows_by.items()):
            out["pooled_2020h2"][k] = {
                "all": summary(rows),
                "by_direction": {
                    d: summary([r for r in rows if r["direction"] == d]) for d in ("LONG", "SHORT")
                },
                "by_regime": {
                    f"{dim}:{v}": summary([r for r in rows if r[dim] == v])
                    for dim in ("trend", "volatility")
                    for v in sorted({r[dim] for r in rows})
                },
                "exit_reasons": dict(Counter(r["exit"] for r in rows)),
            }
        out["combined_e2_2020h2"] = {
            t: equity_stats(c) for t, c in curves.items() if t.startswith(("e2-", "control"))
        }
        # The deviation view: the unchanged evaluator's functions over the repaired windows.
        for exp, fam in TESTS:
            per: dict[str, Any] = {}
            for v in ev.VARIANTS:
                rows = []
                for w in REPAIRED_WINDOWS:
                    rows += ev.trades_of(session, manifest, f"{w}--{exp}-{v}--{fam}", closes)
                per[v] = ev.stats(rows)
            per["verdict"] = ev.verdict(per["primary"], per["spread2x"], exp == "e3")
            out["deviation_view"][f"{exp}:{fam}"] = per
    OUT.write_text(json.dumps(out, indent=1, default=str))
    print("refusals", out["refusals_2020h2"])
    for k, v in out["deviation_view"].items():
        p = v["primary"]
        print(k, "original", out["original_verdicts"][k], "| deviation", v["verdict"], "n", p["n"],
              "R", p.get("mean_R"), "t", p.get("t_clustered"), "gPF", p.get("gross_pf"),
              "2x R", v["spread2x"].get("mean_R"),
              "old R", v["old-conventions"].get("mean_R"))  # fmt: skip
    for k, v in out["pooled_2020h2"].items():
        a = v["all"]
        print("2020H2", k, a["n"], a.get("net"), a.get("gross_pf"), a.get("net_pf"),
              a.get("mean_R"), a.get("ci95_R"), a.get("t_clustered"))  # fmt: skip


if __name__ == "__main__":
    main()
