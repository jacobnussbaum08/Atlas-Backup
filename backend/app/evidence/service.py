"""Persistence and assembly of the Phase 11A evidence program.

* :func:`register_protocol` stores a protocol once (operators only; a new set of criteria is
  a new version; identical criteria are refused rather than registered twice).
* :func:`run_audit` stores a data audit.
* :func:`build_scorecard` judges stored validation runs and the paper account's records
  under one protocol and stores the result with one entry per strategy and the portfolio.
* :func:`latest_entry` is what governance reads before an approval.

Every record is append-only (database triggers). Nothing here changes a strategy, a model,
a risk limit or anything that trades.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.backtesting.config import sha256_of
from app.core.exceptions import AtlasError, NotFoundError
from app.database.models import PortfolioAccount
from app.database.models.evidence import (
    PORTFOLIO,
    EvidenceDataAuditRecord,
    EvidenceProtocolRecord,
    EvidenceScorecardEntryRecord,
    EvidenceScorecardRecord,
)
from app.evidence import data_audit, history
from app.evidence import paper as pp
from app.evidence.classify import (
    Judgement,
    OutOfSample,
    Paper,
    PortfolioEvidence,
    classify,
    classify_portfolio,
)
from app.evidence.enums import AuditStatus, Classification
from app.evidence.protocol import EvidenceCriteria

STATEMENT = (
    "Paper trading only. Backtest, out-of-sample and paper evidence are reported separately "
    "and never pooled. A scorecard changes nothing that trades and promotes nothing: "
    "CANDIDATE_FOR_REVIEW asks a human to review. Simulated paper fills do not reproduce "
    "real-world fills, market impact, liquidity or brokerage behaviour."
)


class EvidenceError(AtlasError):
    code = "evidence_conflict"
    http_status = 409


def _operator(actor: str) -> str:
    if not actor.startswith("operator:") or len(actor) <= len("operator:"):
        raise EvidenceError("only an operator (operator:<name>) may do this")
    return actor


# ---------------------------------------------------------------------- protocols
def register_protocol(
    session: Session,
    criteria: EvidenceCriteria,
    *,
    name: str,
    rationale: str,
    actor: str,
    now: datetime,
) -> EvidenceProtocolRecord:
    _operator(actor)
    digest = criteria.sha256()
    same = session.scalars(
        select(EvidenceProtocolRecord).where(EvidenceProtocolRecord.criteria_sha256 == digest)
    ).first()
    if same is not None:
        raise EvidenceError(
            f"these criteria are already registered as {same.name} v{same.version} ({same.id})"
        )
    version = (
        session.scalar(
            select(func.max(EvidenceProtocolRecord.version)).where(
                EvidenceProtocolRecord.name == name
            )
        )
        or 0
    ) + 1
    row = EvidenceProtocolRecord(
        name=name,
        version=version,
        criteria=criteria.as_json(),
        criteria_sha256=digest,
        rationale=rationale.strip(),
        registered_by=actor,
        registered_at=now,
    )
    session.add(row)
    session.flush()
    return row


def protocol(session: Session, protocol_id: uuid.UUID) -> EvidenceProtocolRecord:
    row = session.get(EvidenceProtocolRecord, protocol_id)
    if row is None:
        raise NotFoundError(f"evidence protocol {protocol_id} does not exist")
    return row


def criteria_of(row: EvidenceProtocolRecord) -> EvidenceCriteria:
    criteria = EvidenceCriteria.model_validate(row.criteria)
    if criteria.sha256() != row.criteria_sha256:  # a stored protocol never changes
        raise EvidenceError(f"protocol {row.id} does not match its registered digest")
    return criteria


# ---------------------------------------------------------------------- audits
def run_audit(
    session: Session, scope: data_audit.AuditScope, *, actor: str, now: datetime
) -> EvidenceDataAuditRecord:
    status, result = data_audit.audit(session, scope)
    row = EvidenceDataAuditRecord(
        scope=scope.as_json(),
        status=status,
        result=result,
        result_sha256=sha256_of(result),
        created_by=actor,
        audited_at=now,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------- scorecards
@dataclass(frozen=True)
class ScorecardRequest:
    protocol_id: uuid.UUID
    # One WALK_FORWARD validation run per strategy (its out-of-sample evidence).
    strategy_runs: dict[str, uuid.UUID]
    # An EVALUATION run of the strategies together (portfolio out-of-sample trades).
    portfolio_run_id: uuid.UUID | None = None
    data_audit_id: uuid.UUID | None = None
    # The paper account whose records are the paper evidence (None: no paper section).
    account_name: str | None = None
    # Strategies with paper evidence but no validation run are judged too.
    extra_strategies: tuple[str, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, Any]:
        return {
            "protocol_id": str(self.protocol_id),
            "strategy_runs": {k: str(v) for k, v in sorted(self.strategy_runs.items())},
            "portfolio_run_id": None
            if self.portfolio_run_id is None
            else str(self.portfolio_run_id),
            "data_audit_id": None if self.data_audit_id is None else str(self.data_audit_id),
            "account_name": self.account_name,
            "extra_strategies": list(self.extra_strategies),
        }


def _portfolio_oos_trades(
    session: Session, req: ScorecardRequest, per_strategy: dict[str, OutOfSample]
) -> tuple[int, str]:
    if req.portfolio_run_id is not None:
        from app.database.models.learning import ValidationRunRecord

        run = session.get(ValidationRunRecord, req.portfolio_run_id)
        if run is None:
            raise NotFoundError(f"validation run {req.portfolio_run_id} does not exist")
        ts = ((run.summary or {}).get("out_of_sample") or {}).get("trade_statistics") or {}
        return int(ts.get("trades", 0) or 0), f"EVALUATION run {run.id}"
    return (
        sum(o.stats.trades for o in per_strategy.values()),
        "sum of the strategies' walk-forward runs (no combined EVALUATION run given)",
    )


def build_scorecard(
    session: Session, req: ScorecardRequest, *, actor: str, now: datetime
) -> EvidenceScorecardRecord:
    proto = protocol(session, req.protocol_id)
    c = criteria_of(proto)
    audit_row = None
    if req.data_audit_id is not None:
        audit_row = session.get(EvidenceDataAuditRecord, req.data_audit_id)
        if audit_row is None:
            raise NotFoundError(f"data audit {req.data_audit_id} does not exist")

    # Out of sample.
    oos: dict[str, OutOfSample] = {}
    contexts: dict[str, dict[str, Any]] = {}
    for strategy_id, run_id in sorted(req.strategy_runs.items()):
        try:
            oos[strategy_id], contexts[strategy_id] = history.oos_evidence(
                session, run_id, c.promotion.cost_stress_multiplier, strategy_id
            )
        except LookupError as exc:
            raise EvidenceError(str(exc)) from exc

    # Paper.
    since = proto.registered_at if c.paper_after_registration_only else None
    account = None
    if req.account_name is not None:
        account = session.scalars(
            select(PortfolioAccount).where(PortfolioAccount.name == req.account_name)
        ).first()
        if account is None:
            raise NotFoundError(f"paper account {req.account_name!r} does not exist")
    capital = 100_000.0
    trades: list[pp.ClosedTrade] = []
    observed: list[str] = []
    quality: dict[str, dict[str, Any]] = {}
    paper_obs: dict[str, Any] | None = None
    if account is not None:
        trades = pp.closed_trades(session, account.id, since, now)
        observed = pp.observed_sessions(session, since, now)
        quality = pp.execution_quality(session, account.id, since, now)
        drawdown = pp.account_drawdown(session, account.id, since, now)
        if drawdown.get("first_equity"):
            capital = float(drawdown["first_equity"])
        paper_obs = {
            "account_name": account.name,
            "since": None if since is None else since.isoformat(),
            "until": now.isoformat(),
            "observed_sessions": len(observed),
            "first_observed_session": observed[0] if observed else None,
            "last_observed_session": observed[-1] if observed else None,
            "closed_trades": len(trades),
            "funnel": pp.funnel(session, account.id, since, now),
            "execution_quality": quality,
            "account": drawdown,
            "reconciliation": pp.reconciliation(session, account.id),
            "open_critical_alerts": pp.open_critical_alerts(session, account.id),
            "alerts_by_type": pp.alerts_by_type(session, account.id, since, now),
        }

    subjects = sorted(
        set(req.strategy_runs) | set(req.extra_strategies) | {t.strategy_id for t in trades}
    )
    judgements: dict[str, Judgement] = {}
    papers: dict[str, Paper | None] = {}
    for sid in subjects:
        papers[sid] = (
            None
            if account is None
            else pp.strategy_paper(trades, sid, len(observed), capital, quality.get(sid))
        )
        judgements[sid] = classify(c, oos.get(sid), papers[sid])

    oos_trades, oos_source = _portfolio_oos_trades(session, req, oos)
    port_stats = pp.portfolio_trades_stats(trades, len(observed), capital)
    repro = history.reproduction_status(
        session,
        [*req.strategy_runs.values(), *([req.portfolio_run_id] if req.portfolio_run_id else [])],
    )
    pe = PortfolioEvidence(
        oos_trades=oos_trades,
        oos_source=oos_source,
        paper_trades=len(trades),
        paper_sessions=len(observed),
        paper_drawdown=None if paper_obs is None else paper_obs["account"].get("max_drawdown"),
        paper_net_pnl=port_stats.net_pnl if trades else None,
        open_critical_alerts=0 if paper_obs is None else paper_obs["open_critical_alerts"],
        reconciliation_clean=None
        if paper_obs is None
        else paper_obs["reconciliation"].get("clean"),
        reproduction_identical=repro["status"],
        audit_status=None if audit_row is None else audit_row.status.value,
        largest_asset_share=port_stats.largest_asset_share,
    )
    port_class, verdict, port_criteria = classify_portfolio(
        c, pe, {k: v.classification for k, v in judgements.items()}
    )

    strategies_json: dict[str, Any] = {}
    for sid in subjects:
        j = judgements[sid]
        o = oos.get(sid)
        p = papers[sid]
        ctx = contexts.get(sid, {})
        strategies_json[sid] = {
            **j.as_json(),
            "backtest": ctx.get("backtest"),
            "out_of_sample": None
            if o is None
            else {
                **o.stats.as_json(),
                "deflated_sharpe": o.deflated_sharpe,
                "pbo": o.pbo,
                "benchmark_sharpe_annualized": o.benchmark_sharpe,
                "cost_stress_net_pnl": o.cost_stress_net_pnl,
                "holdout": ctx.get("holdout"),
                "baselines": ctx.get("baselines"),
                "run": {
                    k: ctx.get(k)
                    for k in (
                        "run_id",
                        "source",
                        "period",
                        "data_known_at",
                        "result_sha256",
                        "verdict_8b",
                        "findings_8b",
                        "windows",
                        "per_trade_returns_stored",
                    )
                },
            },
            "paper": None
            if p is None
            else {
                **p.stats.as_json(),
                "entry_shortfall_vs_decision_bps": p.entry_shortfall_bps,
                "assumed_cost_bps": p.assumed_cost_bps,
                "entry_fills": p.entry_fills,
                "execution_quality": quality.get(sid),
            },
        }
    for sid in strategies_json:  # per-trade return vectors are evidence, not display
        for section in ("out_of_sample", "paper"):
            if strategies_json[sid][section] is not None:
                strategies_json[sid][section].pop("returns", None)

    result: dict[str, Any] = {
        "statement": STATEMENT,
        "protocol": {
            "id": str(proto.id),
            "name": proto.name,
            "version": proto.version,
            "criteria_sha256": proto.criteria_sha256,
            "registered_at": proto.registered_at.isoformat(),
            "registered_by": proto.registered_by,
        },
        "as_of": now.isoformat(),
        "verdict": verdict.value,
        "strategies": strategies_json,
        "portfolio": {
            "classification": port_class.value,
            "criteria": [x.as_json() for x in port_criteria],
            "oos_trades": oos_trades,
            "oos_source": oos_source,
            "paper": None if account is None else {**port_stats.as_json()},
            "reproduction": repro,
            "data_audit": None
            if audit_row is None
            else {
                "id": str(audit_row.id),
                "status": audit_row.status.value,
                "failures": audit_row.result.get("failures"),
                "warnings": audit_row.result.get("warnings"),
            },
        },
        "paper_observation": paper_obs,
        "rules": {
            "continue_paper_trading": [
                c_.value
                for c_ in (
                    Classification.INSUFFICIENT_EVIDENCE,
                    Classification.NOT_DEMONSTRATED,
                    Classification.CONTINUE_PAPER,
                    Classification.CANDIDATE_FOR_REVIEW,
                )
            ],
            "reject": Classification.REJECTED.value,
            "note": "REJECTED recommends disabling the strategy in paper trading; an "
            "operator decides. Nothing is disabled or promoted by a scorecard.",
        },
    }
    if result["portfolio"]["paper"] is not None:
        result["portfolio"]["paper"].pop("returns", None)
    row = EvidenceScorecardRecord(
        protocol_id=proto.id,
        data_audit_id=None if audit_row is None else audit_row.id,
        verdict=verdict,
        inputs=req.as_json(),
        result=result,
        result_sha256=sha256_of(result),
        created_by=actor,
        as_of=now,
    )
    session.add(row)
    session.flush()
    for sid in subjects:
        session.add(
            EvidenceScorecardEntryRecord(
                scorecard_id=row.id,
                subject=sid,
                classification=judgements[sid].classification,
                reasons=judgements[sid].reasons[:20],
            )
        )
    session.add(
        EvidenceScorecardEntryRecord(
            scorecard_id=row.id,
            subject=PORTFOLIO,
            classification=port_class,
            reasons=[x.message for x in port_criteria if x.status.value != "PASS"][:20],
        )
    )
    session.flush()
    return row


def latest_entry(
    session: Session, subject: str, *, after: datetime | None = None
) -> tuple[EvidenceScorecardEntryRecord, EvidenceScorecardRecord, EvidenceProtocolRecord] | None:
    """The newest scorecard entry for ``subject`` (created after ``after``), with its
    scorecard and protocol."""
    e, s, p = EvidenceScorecardEntryRecord, EvidenceScorecardRecord, EvidenceProtocolRecord
    q = (
        select(e, s, p)
        .join(s, s.id == e.scorecard_id)
        .join(p, p.id == s.protocol_id)
        .where(e.subject == subject)
        .order_by(s.created_at.desc(), e.id.desc())
        .limit(1)
    )
    if after is not None:
        q = q.where(s.created_at >= after)
    row = session.execute(q).first()
    return None if row is None else (row[0], row[1], row[2])


def audit_status_ok(c: EvidenceCriteria, status: AuditStatus) -> bool:
    return status in c.portfolio.allowed_audit_statuses


__all__ = [
    "STATEMENT",
    "EvidenceError",
    "ScorecardRequest",
    "audit_status_ok",
    "build_scorecard",
    "criteria_of",
    "latest_entry",
    "protocol",
    "register_protocol",
    "run_audit",
]
