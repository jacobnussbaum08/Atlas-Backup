"""Does combining the inputs add anything? Past decisions against simple baselines.

For completed runs in a range, each evaluated opportunity's **realized outcome** is
measured from bars known at ``known_at``: entry at the next session's open after the lead
proposal's data cutoff (tradable), exit at the close of its holding horizon, direction-
adjusted and net of the stated cost assumption (the Phase 5A label code, FORWARD_RETURN).
An opportunity whose horizon has not completed is IMMATURE and left out; one with a
missing bar is UNDEFINED and left out. Each lead proposal counts once, however many
daily runs re-evaluated it.

Groups compared:

* ``coordinator``: opportunities the coordinator PRIORITIZED_FOR_REVIEW;
* ``take_all``: every evaluated opportunity (all proposals, one per asset and direction);
* ``strongest_signal``: per run, the single opportunity with the strongest raw signal;
* ``declined``: everything the coordinator did not prioritize;
* ``no_trade``: zero, by definition.

The verdict is INSUFFICIENT_EVIDENCE below ``min_observations`` matured coordinator
outcomes. Above it, a Welch t-statistic of the coordinator's mean against take-all of at
least 2 is reported as INCREMENTAL_VALUE_OBSERVED: an observation on overlapping,
unsized, per-opportunity returns, not proof of an edge.
"""

from __future__ import annotations

import math
import statistics
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_engine.enums import DecisionOutcome, DecisionRunStatus, EvaluationVerdict
from app.asset_discovery.sessions import ExchangeCalendar, local_date, trading_day
from app.data_engine.enums import PriceAdjustment
from app.database.models import (
    AIDecisionRecord,
    DecisionEvaluationRecord,
    DecisionRunRecord,
    StrategyFeatureSnapshotRecord,
    StrategyProposalRecord,
)
from app.database.repositories import universe as bar_reads
from app.ml_engine.datasets.bars import DecisionPeriod, forward_sessions
from app.ml_engine.datasets.labels import LabelInput, PathBar, make_label
from app.ml_engine.datasets.spec import TargetSpec
from app.ml_engine.enums import EntryReference, LabelStatus, TargetKind

GROUPS = ("coordinator", "take_all", "strongest_signal", "declined")


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"observations": 0, "mean": None, "median": None, "hit_rate": None, "stdev": None}
    return {
        "observations": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "hit_rate": sum(1 for v in values if v > 0) / len(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else None,
        "sum": sum(values),
    }


def _welch(a: list[float], b: list[float]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    if va + vb <= 0:
        return None
    return (statistics.fmean(a) - statistics.fmean(b)) / math.sqrt(va + vb)


def realized_outcome(
    session: Session,
    proposal: StrategyProposalRecord,
    *,
    calendar: ExchangeCalendar,
    known_at: datetime,
    cost_bps: float,
    settle_seconds: float,
) -> tuple[LabelStatus, float | None, dict[str, Any]]:
    snapshot = session.get(StrategyFeatureSnapshotRecord, proposal.feature_snapshot_ids[0])
    if snapshot is None:
        return LabelStatus.UNDEFINED, None, {"reason": "feature snapshot missing"}
    day = trading_day(calendar, local_date(calendar, proposal.data_cutoff))
    if day is None:
        return LabelStatus.UNDEFINED, None, {"reason": "the data cutoff is not a session"}
    period = DecisionPeriod(proposal.timeframe, (day,), proposal.data_cutoff)
    horizon = proposal.holding_horizon_periods
    forward = forward_sessions(calendar, period, horizon)
    settle = timedelta(seconds=settle_seconds)
    completed = [s for s in forward if s.regular_close + settle <= known_at]
    bars = (
        {
            b.session: b
            for b in bar_reads.daily_bars(
                session,
                [proposal.asset_id],
                provider=snapshot.provider,
                feed=snapshot.feed,
                adjustment=PriceAdjustment(snapshot.adjustment),
                sessions=completed,
                calendar=calendar,
                as_of=known_at,
                known_at=known_at,
            )
        }
        if completed
        else {}
    )
    path: list[PathBar | None] = []
    for s in forward:
        bar = bars.get(s.day)
        if s.regular_close + settle > known_at or bar is None or bar.quality_status != "VALID":
            path.append(None)
        else:
            path.append(PathBar(bar.event_time, bar.open, bar.high, bar.low, bar.close))
    target = TargetSpec(
        kind=TargetKind.FORWARD_RETURN,
        horizon_periods=horizon,
        entry=EntryReference.NEXT_OPEN,
        round_trip_cost_bps=cost_bps,
    )
    label = make_label(
        target,
        LabelInput(
            direction=proposal.direction,
            horizon_periods=horizon,
            decision_close=None,
            path=path,
            label_end_at=forward[-1].regular_close + settle,
            data_known_at=known_at,
        ),
    )
    return label.status, label.value, label.outcome


def evaluate_decisions(
    session: Session,
    *,
    calendar: ExchangeCalendar,
    known_at: datetime,
    start: datetime | None = None,
    end: datetime | None = None,
    policy_version_id: uuid.UUID | None = None,
    cost_bps: float = 10.0,
    settle_seconds: float = 3600.0,
    min_observations: int = 30,
    actor: str = "coordinator",
    store: bool = True,
) -> dict[str, Any]:
    r = DecisionRunRecord
    stmt = select(r).where(r.status == DecisionRunStatus.COMPLETED, r.decision_at <= known_at)
    if start is not None:
        stmt = stmt.where(r.decision_at >= start)
    if end is not None:
        stmt = stmt.where(r.decision_at <= end)
    if policy_version_id is not None:
        stmt = stmt.where(r.policy_version_id == policy_version_id)
    runs = list(session.scalars(stmt.order_by(r.decision_at, r.id)))
    seen: dict[str, set[uuid.UUID]] = {g: set() for g in GROUPS}
    values: dict[str, list[float]] = {g: [] for g in GROUPS}
    statuses: dict[str, int] = {}
    cache: dict[uuid.UUID, tuple[LabelStatus, float | None]] = {}
    for run in runs:
        decisions = [
            d
            for d in session.scalars(
                select(AIDecisionRecord).where(
                    AIDecisionRecord.run_id == run.id, AIDecisionRecord.asset_id.is_not(None)
                )
            )
        ]
        leads: list[tuple[AIDecisionRecord, StrategyProposalRecord]] = []
        for d in decisions:
            lead_id = d.expected.get("lead_proposal_id")
            proposal = (
                None if lead_id is None else session.get(StrategyProposalRecord, uuid.UUID(lead_id))
            )
            if proposal is not None:
                leads.append((d, proposal))
        strongest = max(leads, key=lambda x: (x[1].signal_strength, str(x[1].id)), default=None)
        for d, proposal in leads:
            if proposal.id not in cache:
                status, value, _ = realized_outcome(
                    session,
                    proposal,
                    calendar=calendar,
                    known_at=known_at,
                    cost_bps=cost_bps,
                    settle_seconds=settle_seconds,
                )
                cache[proposal.id] = (status, value)
                statuses[status.value] = statuses.get(status.value, 0) + 1
            status, value = cache[proposal.id]
            if status is not LabelStatus.MATURED or value is None:
                continue
            groups = ["take_all"]
            groups.append(
                "coordinator" if d.outcome is DecisionOutcome.PRIORITIZE_FOR_REVIEW else "declined"
            )
            if strongest is not None and strongest[1].id == proposal.id:
                groups.append("strongest_signal")
            for g in groups:
                if proposal.id not in seen[g]:
                    seen[g].add(proposal.id)
                    values[g].append(value)
    results: dict[str, Any] = {g: _stats(values[g]) for g in GROUPS}
    results["no_trade"] = {"observations": None, "mean": 0.0, "note": "zero by definition"}
    results["outcome_status_counts"] = statuses
    t_take_all = _welch(values["coordinator"], values["take_all"])
    t_strongest = _welch(values["coordinator"], values["strongest_signal"])
    results["coordinator_vs_take_all_welch_t"] = t_take_all
    results["coordinator_vs_strongest_welch_t"] = t_strongest
    n = len(values["coordinator"])
    if n < min_observations:
        verdict = EvaluationVerdict.INSUFFICIENT_EVIDENCE
    elif t_take_all is not None and t_take_all >= 2 and (results["coordinator"]["mean"] or 0) > 0:
        verdict = EvaluationVerdict.INCREMENTAL_VALUE_OBSERVED
    else:
        verdict = EvaluationVerdict.NO_INCREMENTAL_VALUE
    notes = [
        f"realized net returns per opportunity, entry at the next open, exit at the "
        f"holding horizon's close, {cost_bps:g} bps round-trip cost ASSUMPTION",
        "returns overlap in time and are unsized: no portfolio return or annualized ratio "
        "is implied",
        f"{n} matured coordinator outcome(s); the verdict needs at least {min_observations}",
    ]
    spec = {
        "known_at": known_at.isoformat(),
        "start": None if start is None else start.isoformat(),
        "end": None if end is None else end.isoformat(),
        "policy_version_id": None if policy_version_id is None else str(policy_version_id),
        "cost_bps": cost_bps,
        "min_observations": min_observations,
        "outcome": "FORWARD_RETURN, NEXT_OPEN entry, lead proposal's holding horizon",
    }
    record_id = None
    if store:
        record = DecisionEvaluationRecord(
            id=uuid.uuid4(),
            policy_version_id=policy_version_id,
            known_at=known_at,
            decision_start=runs[0].decision_at if runs else None,
            decision_end=runs[-1].decision_at if runs else None,
            runs_evaluated=len(runs),
            spec=spec,
            results=results,
            verdict=verdict,
            notes=notes,
            created_by=actor,
        )
        session.add(record)
        session.flush()
        record_id = str(record.id)
    return {
        "id": record_id,
        "runs_evaluated": len(runs),
        "verdict": verdict.value,
        "results": results,
        "notes": notes,
        "spec": spec,
    }


__all__ = ["GROUPS", "evaluate_decisions", "realized_outcome"]
