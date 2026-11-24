# ruff: noqa: E501  (assumption tables and journal lines read as one row each)
"""X6 (D-255): the execution-inputs artifact, built once and rebuilt byte-for-byte by x6-verify.

``x6_execution_inputs_v2.json.gz`` records, from the pinned X4.6.1 and X5 artifacts:

* the pins (file SHA-256 of every input, both X5 content hashes) and the X6 configuration's
  definition, hash and labelled assumptions;
* the check that X6's executable sessions reproduce X5's series exactly;
* the formation step of every slot (X5 limitation 5), with its source and cross-check;
* how each exit and each restricted spin-off is treated by the ledger;
* the ledger's reconciliation with X5: single positions held with costs off reproduce X5's
  daily factors and terminal steps (an accounting replay, not a backtest);
* worked execution examples on recorded bars: every input listed, reproducible by hand.

No backtest is run, no performance figure is computed, nothing is written to a database.
"""

from __future__ import annotations

import hashlib
import itertools
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.research import resolution as rs
from app.research import x4_evidence as x4
from app.research import x5_accounting as acc
from app.research import x6_data as xd
from app.research import x6_execution as ex
from app.research import x6_portfolio as xp

D = Decimal
PATH = Path(xd.LIFECYCLE) / "x6_execution_inputs_v2.json.gz"
# V1 (file aeda44c1..., content ab0f099e...) was built before the exact-share fix (D-256): a
# share factor such as 1/3 was a repeating decimal, and cash in lieu was not rounded, so the
# journal could lose digits. It stays as written; the current code rebuilds V2.
V1_PATH = Path(xd.LIFECYCLE) / "x6_execution_inputs_v1.json.gz"
V1_SHA256 = "aeda44c15f08d9ce964b087069c28ae9f219ff2dec1b5ad149bb7508326e4d39"
FORMATION_DIVIDEND_SLOTS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
RECONCILE_QUANTITY = D(600)  # divisible by every share factor in the ledger


def _nonshare(snap: dict[str, Any]) -> D:
    return D(snap["cash"]) + sum((D(v) for v in snap["receivables"].values()), D(0))


def reconcile_slot(market: xd.Market, series: dict[str, Any], slot: str) -> dict[str, Any]:
    """Hold RECONCILE_QUANTITY shares from the usable range's first close, costs off, until the
    exit's valuation session or the range's end; compare every session's value change with
    X5's factor and the exit with X5's terminal step. Stops comparing once frozen."""
    sd = market.slots[slot]
    if sd.usable_range is None:
        raise ValueError(f"{slot}: no usable range")
    lo, hi = sd.usable_range
    term = sd.terminal
    end = term["valuation_session"] if term and term.get("status") in xd.TERMINAL_APPLIED else hi
    cfg = ex.X6Config(
        costs_enabled=False, window_end_policy=ex.WindowEndPolicy.EXIT_BEFORE_WINDOW_END
    )
    pf = xp.run_hold(market, cfg, slot, RECONCILE_QUANTITY, lo, end)
    factors = dict(series["returns"])
    worst = D(0)
    compared = 0
    terminal_diff = None
    snaps = pf.snapshots[1:]
    for a, b in itertools.pairwise(snaps):
        if slot in a["frozen"]:
            break
        mv_a = D(a["long_market_value"])
        if mv_a == 0:
            break
        g = (D(b["long_market_value"]) + _nonshare(b) - _nonshare(a)) / mv_a
        if b["session"] in factors:
            worst = max(worst, abs(g - xd.dec(factors[b["session"]])))
            compared += 1
        step = series.get("terminal_step")
        if step and b["session"] == step["session"] and not b["positions"]:
            terminal_diff = str(abs(g - xd.dec(step["gross"])))
    frozen = [e for e in pf.events if e["event"] == "FROZEN"]
    return {
        "slot": slot,
        "factors_compared": compared,
        "max_abs_difference": str(worst),
        "terminal_step_difference": terminal_diff,
        "frozen": [{"session": e["session"], "reason": e["reason"]} for e in frozen],
        "invariant_violations": pf.violations,
    }


def reconciliation(market: xd.Market, series: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Every usable slot with a split, a distribution, a terminal step, a restricted spin-off or
    a series break, plus the formation-dividend slots."""
    chosen = set(FORMATION_DIVIDEND_SLOTS)
    for slot, ser in series.items():
        sd = market.slots[slot]
        types = {e["type"] for e in sd.events if e.get("in_usable_range")}
        if (
            ser["breaks"]
            or ser.get("terminal_step")
            or types & {"STOCK_SPLIT", "REVERSE_SPLIT", "SPIN_OFF"}
        ):
            chosen.add(slot)
    rows = [reconcile_slot(market, series[slot], slot) for slot in sorted(chosen)]
    worst = max((D(r["max_abs_difference"]) for r in rows), default=D(0))
    term = [
        D(r["terminal_step_difference"]) for r in rows if r["terminal_step_difference"] is not None
    ]
    return {
        "method": "one position of 600 shares booked at the usable range's first close, costs off; per session, (market value + new cash and receivables) / previous market value, compared with X5's factor",
        "tolerance_basis": "REDACTED_FOR_GITHUB",
        "slots": len(rows),
        "factors_compared": sum(r["factors_compared"] for r in rows),
        "max_abs_difference": str(worst),
        "terminal_steps_compared": len(term),
        "terminal_max_abs_difference": str(max(term, default=D(0))),
        "tolerance": "1e-8",
        "ok": worst < D("1e-8") and all(t < D("1e-8") for t in term) and not any(r["invariant_violations"] for r in rows),
        "rows": rows,
    }  # fmt: skip


def formation_dividend_replay(market: xd.Market) -> list[dict[str, Any]]:
    """Held from the formation close (costs off): the first session's value change equals the
    formation step, its dividend is a receivable, and spendable cash does not change."""
    out = []
    cfg = ex.X6Config(costs_enabled=False)
    for slot in FORMATION_DIVIDEND_SLOTS:
        sd = market.slots[slot]
        step = xd.formation_step(sd, market)
        pf = xp.run_hold(market, cfg, slot, RECONCILE_QUANTITY, sd.formation, sd.window[0])
        a, b = pf.snapshots[1], pf.snapshots[2]
        g = (D(b["long_market_value"]) + _nonshare(b) - _nonshare(a)) / D(a["long_market_value"])
        out.append({
            "slot": slot, "formation_step": step["gross"], "ledger_gross": str(round(g, 10)),
            "dividend_receivable": b["receivables"]["RECEIVABLE_DIVIDENDS"],
            "spendable_before": a["spendable"], "spendable_after": b["spendable"],
            "bought_at_first_open_entitled": False,
        })  # fmt: skip
    return out


def exit_treatment(market: xd.Market) -> dict[str, Any]:
    rows = []
    for slot, sd in sorted(market.slots.items()):
        t = sd.terminal
        if not t:
            continue
        credited = t["status"] in xd.TERMINAL_APPLIED and sd.has_series
        if credited:
            treatment = "CONVERTED_AT_VALUATION_SESSION"
        elif t["status"] in xd.TERMINAL_APPLIED:
            treatment = "SLOT_NOT_USABLE_NEVER_HELD"
        else:
            treatment = "FROZEN_AT_LAST_USABLE_CLOSE"
        rows.append({"slot": slot, "status": t["status"], "slot_usable": sd.has_series,
                     "valuation_session": t.get("valuation_session"),
                     "value_per_share": t.get("value_per_share"), "bounds": t.get("bounds"),
                     "conditional_value": t.get("conditional_value"), "treatment": treatment})  # fmt: skip
    return {
        "by_status": dict(sorted(Counter(r["status"] for r in rows).items())),
        "by_treatment": dict(sorted(Counter(r["treatment"] for r in rows).items())),
        "rows": rows,
    }


def restricted_spin_offs(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"parent": sp["parent"], "ex_date": sp["ex_date"], "tr_treatment": sp["tr_treatment"],
         "slots": sp["slots"], "treatment": "FREEZE_POSITION_HELD_AT_PREVIOUS_CLOSE"}
        for sp in ledger["spin_offs"] if sp["tr_treatment"] != "APPLIED"
    ]  # fmt: skip


def examples(*_args: object, **_kwargs: object) -> object:
    raise NotImplementedError("REDACTED_FOR_GITHUB")


def build() -> dict[str, Any]:
    docs = xd.load_documents()
    market = xd.Market.from_documents(docs)
    series = docs[xd.X5_TOTAL_RETURNS]["series"]
    steps = {slot: xd.formation_step(sd, market) for slot, sd in sorted(market.slots.items())}
    computed = [s for s in steps.values() if s["status"] == "COMPUTED"]
    cfg = ex.DEFAULT_CONFIG
    doc: dict[str, Any] = {
        "kind": "x6_execution_inputs",
        "decision": xd.DECISION,
        "code": "X6_EXECUTION_INPUTS_V1",
        "inputs": {"pins": xd.verify_pins(), "x5_content_sha256": xd.X5_CONTENT},
        "config": {"definition": cfg.definition(), "sha256": cfg.sha256(), "assumptions": cfg.assumptions()},
        "validity_check": xd.check_against_x5(market, series),
        "formation_steps": {
            "summary": {
                "by_status": dict(sorted(Counter(s["status"] for s in steps.values()).items())),
                "cross_checked_with_previous_slot": sum(1 for s in computed if s.get("cross_check")),
                "cross_check_equal": sum(1 for s in computed if (s.get("cross_check") or {}).get("equal")),
                "with_first_session_dividend": sorted(s["slot"] for s in computed if s["dividends_on_first_session"]),
                "with_first_session_split": sorted(s["slot"] for s in computed if s["split_ratio"] != "1"),
                "with_first_session_distribution": sorted(s["slot"] for s in computed if s["distribution_value"] != "0"),
            },
            "replay": formation_dividend_replay(market),
            "rows": steps,
        },
        "exits": exit_treatment(market),
        "restricted_spin_offs": restricted_spin_offs(docs[xd.X5_LEDGER]),
        "breaks": {slot: ser["breaks"] for slot, ser in sorted(series.items()) if ser["breaks"]},
        "reconciliation": reconciliation(market, series),
        "examples": examples(market),
    }  # fmt: skip
    doc = _strings(doc)
    doc["content_sha256"] = content_sha256(doc)
    return doc


def content_sha256(doc: dict[str, Any]) -> str:
    return acc.content_sha256(doc)


def write(doc: dict[str, Any], path: Path = PATH) -> str:
    if path.exists():
        raise FileExistsError(f"{path} exists: X6 artifacts are written once")
    data = x4.gz(x4.canonical(rs._plain(_strings(doc))))
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _strings(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strings(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strings(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    return obj


def verify() -> dict[str, Any]:
    doc = _strings(build())
    stored = x4.read_json(PATH)
    rebuilt = content_sha256(doc)
    return {
        "content": [stored["content_sha256"], content_sha256(stored), rebuilt],
        "file": {PATH.as_posix(): xd.file_sha256(PATH.as_posix())},
        "validity_ok": doc["validity_check"]["ok"],
        "reconciliation_ok": doc["reconciliation"]["ok"],
        "ok": len({stored["content_sha256"], content_sha256(stored), rebuilt}) == 1
        and doc["validity_check"]["ok"]
        and doc["reconciliation"]["ok"],
    }
