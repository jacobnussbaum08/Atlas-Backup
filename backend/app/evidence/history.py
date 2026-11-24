"""Backtest and out-of-sample evidence from stored Phase 8B validation runs (Phase 11A).

Everything is read from the run's own records (summary, folds, robustness, segments,
holdout reads, trials); nothing is re-simulated, so the scorecard judges exactly what was
validated. Two figures are computed here from stored bars because 8B did not store them:

* the **benchmark's Sharpe ratio** on the same stitched out-of-sample sessions (the 8B
  record has only its compound return), from split-adjusted closes;
* **simple baselines** on the same test windows: holding the benchmark, holding the
  universe equally weighted (split-adjusted price returns; no dividends, as everywhere in
  Atlas), and holding cash (0).

The **BACKTEST** section is the development (in-sample) replay of the final selection: it
shows what the parameters were chosen on and how much the out-of-sample record degrades.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.data import load_dataset
from app.database.models import Asset
from app.database.models.learning import (
    ValidationExperimentRecord,
    ValidationFoldRecord,
    ValidationHoldoutRecord,
    ValidationRobustnessRecord,
    ValidationRunRecord,
    ValidationSegmentRecord,
    ValidationTrialRecord,
)
from app.evidence.classify import OutOfSample, SectionStats
from app.learning import statistics as st
from app.learning.enums import RobustnessMethod, Sample, SegmentKind

ANNUAL = math.sqrt(252)


def _multiplier_key(m: float) -> str:
    return str(int(m)) if float(m).is_integer() else str(m)


def _segments(session: Session, run_id: uuid.UUID, kind: SegmentKind) -> dict[str, dict[str, Any]]:
    s = ValidationSegmentRecord
    rows = session.scalars(
        select(s).where(
            s.run_id == run_id, s.sample == Sample.OUT_OF_SAMPLE, s.segment_kind == kind
        )
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        m = dict(r.metrics or {})
        out[r.segment_value] = {
            "trades": int(m.get("trades", 0) or 0),
            "net_pnl": float(m.get("net_pnl", 0.0) or 0.0),
            "expectancy": m.get("expectancy"),
            "profit_factor": m.get("profit_factor"),
            "win_rate": m.get("win_rate"),
            "status": r.status.value,
        }
    return out


def _robustness(session: Session, run_id: uuid.UUID) -> dict[RobustnessMethod, dict[str, Any]]:
    r = ValidationRobustnessRecord
    out: dict[RobustnessMethod, dict[str, Any]] = {}
    for row in session.scalars(select(r).where(r.run_id == run_id).order_by(r.id)):
        out.setdefault(row.method, dict(row.result or {}))
    return out


def oos_sessions(session: Session, run_id: uuid.UUID) -> list[tuple[date, date, list[date]]]:
    """Each fold's test window: (first, last, sessions with a recorded return)."""
    f = ValidationFoldRecord
    out = []
    for fold in session.scalars(select(f).where(f.run_id == run_id).order_by(f.fold_index)):
        days = [date.fromisoformat(d) for d, *_ in (fold.oos_series or [])]
        out.append((fold.test_start, fold.test_end, days))
    return out


def _closes(
    session: Session, run: ValidationRunRecord, symbols: list[str], first: date, last: date
) -> dict[str, dict[date, float]]:
    data = run.base_config.get("data", {})
    assets = {
        a.id: a.symbol for a in session.scalars(select(Asset).where(Asset.symbol.in_(symbols)))
    }
    if not assets:
        return {}
    ds = load_dataset(
        session,
        list(assets),
        provider=data.get("provider", "alpaca"),
        feed=data.get("feed", "iex"),
        first=first - timedelta(days=10),
        last=last,
        known_at=run.data_known_at,
        settle=timedelta(seconds=int(data.get("settle_seconds", 3600))),
        split_adjusted=True,
    )
    out: dict[str, dict[date, float]] = {}
    for asset_id, symbol in assets.items():
        bars = ds.split.get(asset_id) or ds.raw.get(asset_id, {})
        out[symbol] = {d: float(b.close) for d, b in bars.items() if b.valid and b.close > 0}
    return out


def _window_return(closes: dict[date, float], first: date, last: date) -> float | None:
    before = [d for d in closes if d < first]
    upto = [d for d in closes if d <= last]
    if not before or not upto:
        return None
    start, end = closes[max(before)], closes[max(upto)]
    return end / start - 1 if start > 0 else None


def baselines(
    session: Session, run: ValidationRunRecord, windows: list[tuple[date, date, list[date]]]
) -> dict[str, Any]:
    if not windows:
        return {"note": "no out-of-sample windows"}
    cfg = run.base_config
    universe = [a["symbol"] for a in cfg.get("assets", [])]
    bench = cfg.get("benchmark_symbol") or (cfg.get("benchmark") or {}).get("symbol")
    symbols = sorted(set(universe) | ({bench} if bench else set()))
    closes = _closes(session, run, symbols, windows[0][0], windows[-1][1])
    bench_daily: list[float] = []
    bench_total, ew_total, used = 1.0, 1.0, []
    for first, last, days in windows:
        if bench and bench in closes:
            c = closes[bench]
            r = _window_return(c, first, last)
            bench_total *= 1 + (r or 0.0)
            prev = [d for d in c if d < first]
            p = c[max(prev)] if prev else None
            for d in days:
                if d in c and p:
                    bench_daily.append(c[d] / p - 1)
                    p = c[d]
        rets = [
            r
            for s in universe
            if s in closes and (r := _window_return(closes[s], first, last)) is not None
        ]
        used.append(len(rets))
        ew_total *= 1 + (sum(rets) / len(rets) if rets else 0.0)
    sr = st.sharpe(bench_daily)
    return {
        "benchmark": bench,
        "benchmark_return": bench_total - 1 if bench else None,
        "benchmark_sharpe_annualized": None if sr is None else sr * ANNUAL,
        "benchmark_sessions": len(bench_daily),
        "equal_weight_universe_return": ew_total - 1,
        "equal_weight_assets_per_window": used,
        "cash_return": 0.0,
        "basis": "split-adjusted closes, price returns, same test windows; no dividends",
    }


def backtest_section(session: Session, run: ValidationRunRecord) -> dict[str, Any] | None:
    final = (run.summary or {}).get("final_trial")
    if final is None:
        return None
    t = session.scalars(
        select(ValidationTrialRecord).where(
            ValidationTrialRecord.run_id == run.id, ValidationTrialRecord.trial_number == final
        )
    ).first()
    if t is None or not isinstance(t.development, dict):
        return None
    d = t.development
    return {
        "source": f"validation run {run.id}, trial {final}, development period (in sample)",
        "first": d.get("first"),
        "last": d.get("last"),
        "sessions": d.get("sessions"),
        "net_return": d.get("net_return"),
        "max_drawdown": d.get("max_drawdown"),
        "sharpe_annualized": d.get("sharpe_annualized"),
        "trade_statistics": d.get("trade_statistics"),
        "note": "in-sample: the parameters were chosen on this period; it passes no criterion",
    }


def holdout_first_read(session: Session, run_id: uuid.UUID) -> dict[str, Any] | None:
    h = ValidationHoldoutRecord
    row = session.scalars(
        select(h).where(h.run_id == run_id).order_by(h.access_number, h.created_at).limit(1)
    ).first()
    if row is None:
        return None
    ts = (row.result or {}).get("trade_statistics") or {}
    return {
        "access_number": row.access_number,
        "trades": int(ts.get("trades", 0) or 0),
        "net_pnl": float(ts.get("net_pnl", 0.0) or 0.0),
        "net_return": (row.result or {}).get("net_return"),
        "first": row.holdout_start.isoformat(),
        "last": row.holdout_end.isoformat(),
    }


def _standalone(
    session: Session, run: ValidationRunRecord, strategy_id: str
) -> tuple[OutOfSample, dict[str, Any]]:
    """A strategy's stand-alone replay inside a combined EVALUATION run: its stored
    parameters (no search) on the run's test windows. Only the stitched statistics exist:
    no trade list, no direction or regime segments, no DSR/PBO, no per-strategy cost stress
    or holdout read, so the criteria that need them stay NOT_AVAILABLE."""
    r = ValidationRobustnessRecord
    record = None
    for row in session.scalars(
        select(r).where(r.run_id == run.id, r.method == RobustnessMethod.REGIME_SEGMENTATION)
    ):
        if "individual" in (row.result or {}):
            record = row.result["individual"].get(strategy_id)
            break
    if record is None:
        raise LookupError(f"run {run.id} holds no stand-alone replay of {strategy_id}")
    ts = record.get("trade_statistics") or {}
    windows = oos_sessions(session, run.id)
    base = baselines(session, run, windows)
    stats = SectionStats(
        trades=int(ts.get("trades", 0) or 0),
        sessions=int(record.get("sessions", 0) or 0),
        net_pnl=float(ts.get("net_pnl", 0.0) or 0.0),
        gross_pnl=float(ts.get("gross_pnl", 0.0) or 0.0),
        costs=float(ts.get("costs", 0.0) or 0.0),
        profit_factor=ts.get("profit_factor"),
        expectancy=ts.get("expectancy"),
        win_rate=ts.get("win_rate"),
        top5_share=ts.get("top5_share_of_net_profit"),
        drawdown=record.get("max_drawdown"),
        net_return=record.get("net_return"),
        sharpe=record.get("sharpe_annualized"),
    )
    evidence = OutOfSample(
        stats=stats,
        run_id=str(run.id),
        benchmark_sharpe=base.get("benchmark_sharpe_annualized"),
        run_verdict=None,
    )
    context = {
        "run_id": str(run.id),
        "source": "stand-alone replay inside a combined EVALUATION run (stored parameters, "
        "no search); trade-level segments, DSR, PBO, cost stress and holdout are not "
        "available for it",
        "period": [run.start_date.isoformat(), run.end_date.isoformat()],
        "data_known_at": run.data_known_at.isoformat(),
        "result_sha256": run.result_sha256,
        "verdict_8b": None,
        "findings_8b": [],
        "windows": [[a.isoformat(), b.isoformat(), len(d)] for a, b, d in windows],
        "baselines": base,
        "holdout": None,
        "per_trade_returns_stored": False,
        "backtest": None,
    }
    return evidence, context


def oos_evidence(
    session: Session,
    run_id: uuid.UUID,
    cost_multiplier: float,
    strategy_id: str | None = None,
) -> tuple[OutOfSample, dict[str, Any]]:
    run = session.get(ValidationRunRecord, run_id)
    if run is None:
        raise LookupError(f"validation run {run_id} does not exist")
    experiment = session.get(ValidationExperimentRecord, run.experiment_id)
    if strategy_id is not None and experiment is not None:
        if strategy_id not in experiment.strategy_ids:
            raise LookupError(f"run {run.id} did not evaluate {strategy_id}")
        if len(experiment.strategy_ids) > 1:
            return _standalone(session, run, strategy_id)
    summary = run.summary or {}
    oos = summary.get("out_of_sample") or {}
    ts = oos.get("trade_statistics") or {}
    rob = _robustness(session, run.id)
    by_asset = _segments(session, run.id, SegmentKind.ASSET)
    total = float(ts.get("net_pnl", 0.0) or 0.0)
    largest = (
        max(v["net_pnl"] for v in by_asset.values()) / total if total > 0 and by_asset else None
    )
    returns: list[float] = []
    for fold in session.scalars(
        select(ValidationFoldRecord).where(ValidationFoldRecord.run_id == run.id)
    ):
        returns += [float(t["return"]) for t in (fold.out_of_sample or {}).get("trades", [])]
    windows = oos_sessions(session, run.id)
    base = baselines(session, run, windows)
    stress = rob.get(RobustnessMethod.COST_STRESS, {}).get("levels", {})
    level = stress.get(_multiplier_key(cost_multiplier)) or {}
    hold = holdout_first_read(session, run.id)
    stats = SectionStats(
        trades=int(ts.get("trades", 0) or 0),
        sessions=int(oos.get("sessions", 0) or 0),
        net_pnl=total,
        gross_pnl=float(ts.get("gross_pnl", 0.0) or 0.0),
        costs=float(ts.get("costs", 0.0) or 0.0),
        profit_factor=ts.get("profit_factor"),
        expectancy=ts.get("expectancy"),
        win_rate=ts.get("win_rate"),
        top5_share=ts.get("top5_share_of_net_profit"),
        largest_asset_share=largest,
        drawdown=oos.get("max_drawdown"),
        net_return=oos.get("net_return"),
        sharpe=oos.get("sharpe_annualized"),
        returns=tuple(returns),
        by_direction=_segments(session, run.id, SegmentKind.DIRECTION),
        by_regime=_segments(session, run.id, SegmentKind.MARKET_TREND),
    )
    evidence = OutOfSample(
        stats=stats,
        run_id=str(run.id),
        benchmark_sharpe=base.get("benchmark_sharpe_annualized"),
        deflated_sharpe=rob.get(RobustnessMethod.DEFLATED_SHARPE, {}).get("deflated_sharpe"),
        pbo=rob.get(RobustnessMethod.PBO, {}).get("pbo"),
        cost_stress_net_pnl=level.get("trade_net_pnl"),
        holdout_access_number=None if hold is None else hold["access_number"],
        holdout_trades=None if hold is None else hold["trades"],
        holdout_net_pnl=None if hold is None else hold["net_pnl"],
        run_verdict=run.verdict.value if run.verdict is not None else None,
        run_findings=tuple(run.findings or ()),
    )
    context = {
        "run_id": str(run.id),
        "period": [run.start_date.isoformat(), run.end_date.isoformat()],
        "data_known_at": run.data_known_at.isoformat(),
        "result_sha256": run.result_sha256,
        "verdict_8b": evidence.run_verdict,
        "findings_8b": list(evidence.run_findings),
        "windows": [[a.isoformat(), b.isoformat(), len(d)] for a, b, d in windows],
        "baselines": base,
        "holdout": hold,
        "per_trade_returns_stored": bool(returns),
        "backtest": backtest_section(session, run),
    }
    return evidence, context


def reproduction_status(session: Session, run_ids: list[uuid.UUID]) -> dict[str, Any]:
    r = ValidationRunRecord
    rows = session.scalars(
        select(r).where(r.reproduction_of.in_(run_ids), r.status == "COMPLETED")
    ).all()
    identical = [str(x.reproduction_of) for x in rows if x.reproduced_identical]
    different = [str(x.reproduction_of) for x in rows if x.reproduced_identical is False]
    return {
        "reproductions": len(rows),
        "identical": identical,
        "different": different,
        "status": None if not rows else (not different),
    }


__all__ = [
    "backtest_section",
    "baselines",
    "holdout_first_read",
    "oos_evidence",
    "oos_sessions",
    "reproduction_status",
]
