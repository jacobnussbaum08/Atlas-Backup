"""Judging evidence against a protocol (Phase 11A). Pure functions, no database.

The three evidence sections are judged separately and never pooled:

* **BACKTEST** (the development period the parameters were chosen on) is reported only; it
  passes no criterion.
* **OUT_OF_SAMPLE** (stitched walk-forward test windows, robustness results and the first
  holdout read) carries the historical criteria.
* **PAPER** (closed positions of the simulated paper broker) carries the paper criteria.

Order of judgement for one strategy:

1. **Rejection**: a section whose sample is sufficient and whose evidence is negative under
   the protocol's rejection rules rejects the strategy, whatever the other section says.
2. **Insufficient**: without a sufficient out-of-sample sample, or when a historical
   criterion cannot be evaluated, the strategy is INSUFFICIENT_EVIDENCE.
3. **Historical criteria**: a FAIL makes it NOT_DEMONSTRATED.
4. **Paper**: with every historical criterion passing, an insufficient paper sample means
   CONTINUE_PAPER; a paper FAIL means NOT_DEMONSTRATED; otherwise CANDIDATE_FOR_REVIEW.

Nothing here can produce a promotion: CANDIDATE_FOR_REVIEW is a request for a human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evidence.enums import Classification, CriterionStatus, EvidenceSection, ReadinessVerdict
from app.evidence.protocol import EvidenceCriteria
from app.learning import statistics as st

S = CriterionStatus


@dataclass(frozen=True)
class SectionStats:
    """Per-trade statistics of one evidence section. ``returns`` are per-trade net returns
    (net P/L / entry value); ``drawdown`` is the section's maximum drawdown as a fraction."""

    trades: int
    sessions: int
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    profit_factor: float | None = None  # None: no losing trade (or no trade)
    expectancy: float | None = None
    win_rate: float | None = None
    top5_share: float | None = None
    largest_asset_share: float | None = None
    drawdown: float | None = None
    net_return: float | None = None
    sharpe: float | None = None
    returns: tuple[float, ...] = ()
    by_direction: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_regime: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "trades": self.trades,
            "sessions": self.sessions,
            "net_pnl": self.net_pnl,
            "gross_pnl": self.gross_pnl,
            "costs": self.costs,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "win_rate": self.win_rate,
            "top5_share_of_net_profit": self.top5_share,
            "largest_asset_share_of_net_profit": self.largest_asset_share,
            "max_drawdown": self.drawdown,
            "net_return": self.net_return,
            "sharpe_annualized": self.sharpe,
            "by_direction": self.by_direction,
            "by_regime": self.by_regime,
        }


@dataclass(frozen=True)
class OutOfSample:
    stats: SectionStats
    run_id: str | None = None
    benchmark_sharpe: float | None = None
    deflated_sharpe: float | None = None
    pbo: float | None = None
    # Trade net P/L of the final parameters replayed at ``cost_stress_multiplier`` x costs.
    cost_stress_net_pnl: float | None = None
    holdout_access_number: int | None = None
    holdout_trades: int | None = None
    holdout_net_pnl: float | None = None
    run_verdict: str | None = None
    run_findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Paper:
    stats: SectionStats
    # Mean adverse entry shortfall (bps) against the decision's reference price, the
    # assumed slippage + half spread (bps) of the execution model, and the number of fills.
    entry_shortfall_bps: float | None = None
    assumed_cost_bps: float | None = None
    entry_fills: int = 0


@dataclass(frozen=True)
class Criterion:
    code: str
    section: EvidenceSection
    status: CriterionStatus
    value: Any
    threshold: Any
    message: str
    blocking: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "section": self.section.value,
            "status": self.status.value,
            "value": self.value,
            "threshold": self.threshold,
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class Judgement:
    classification: Classification
    criteria: list[Criterion]
    reasons: list[str]
    rejections: list[str]

    def as_json(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "reasons": self.reasons,
            "rejections": self.rejections,
            "criteria": [c.as_json() for c in self.criteria],
        }


# ---------------------------------------------------------------------- helpers
def _at_least(
    code: str, sec: EvidenceSection, value: float | None, minimum: float, what: str
) -> Criterion:
    if value is None:
        return Criterion(code, sec, S.NOT_AVAILABLE, None, minimum, f"{what}: not available")
    ok = value >= minimum
    return Criterion(
        code,
        sec,
        S.PASS if ok else S.FAIL,
        value,
        minimum,
        f"{what} {value:.4g} {'>=' if ok else '<'} {minimum:g}",
    )


def _above(
    code: str, sec: EvidenceSection, value: float | None, minimum: float, what: str
) -> Criterion:
    if value is None:
        return Criterion(code, sec, S.NOT_AVAILABLE, None, minimum, f"{what}: not available")
    ok = value > minimum
    return Criterion(
        code,
        sec,
        S.PASS if ok else S.FAIL,
        value,
        minimum,
        f"{what} {value:.4g} {'>' if ok else '<='} {minimum:g}",
    )


def _at_most(
    code: str, sec: EvidenceSection, value: float | None, maximum: float, what: str
) -> Criterion:
    if value is None:
        return Criterion(code, sec, S.NOT_AVAILABLE, None, maximum, f"{what}: not available")
    ok = value <= maximum
    return Criterion(
        code,
        sec,
        S.PASS if ok else S.FAIL,
        value,
        maximum,
        f"{what} {value:.4g} {'<=' if ok else '>'} {maximum:g}",
    )


def _profit_factor(sec: EvidenceSection, s: SectionStats, minimum: float) -> Criterion:
    if s.trades == 0:
        return Criterion("PROFIT_FACTOR", sec, S.NOT_AVAILABLE, None, minimum, "no trades")
    if s.profit_factor is None:  # no losing trade
        ok = s.net_pnl > 0
        return Criterion(
            "PROFIT_FACTOR",
            sec,
            S.PASS if ok else S.FAIL,
            None,
            minimum,
            "no losing trade" if ok else "no winning trade",
        )
    return _at_least("PROFIT_FACTOR", sec, s.profit_factor, minimum, "profit factor")


def _sample(
    code: str, sec: EvidenceSection, have: int, need: int, what: str, blocking: bool = True
) -> Criterion:
    ok = have >= need
    return Criterion(
        code,
        sec,
        S.PASS if ok else S.INSUFFICIENT,
        have,
        need,
        f"{have} {what} (need {need})",
        blocking,
    )


def _no_profit_is_not_a_pass(cs: list[Criterion], s: SectionStats) -> list[Criterion]:
    """Concentration shares are undefined without net profit; with trades, that fails."""
    return [
        Criterion(x.code, x.section, S.FAIL, None, x.threshold, "no net profit to attribute")
        if x.code in {"TOP5_SHARE", "ASSET_CONCENTRATION"} and x.value is None and s.trades
        else x
        for x in cs
    ]


def sufficient_oos(c: EvidenceCriteria, oos: OutOfSample | None) -> bool:
    return (
        oos is not None
        and oos.stats.trades >= c.sample.min_oos_trades
        and oos.stats.sessions >= c.sample.min_oos_sessions
    )


def sufficient_paper(c: EvidenceCriteria, paper: Paper | None) -> bool:
    return (
        paper is not None
        and paper.stats.trades >= c.sample.min_paper_trades
        and paper.stats.sessions >= c.sample.min_paper_sessions
    )


# ---------------------------------------------------------------------- rejection
def rejections(c: EvidenceCriteria, oos: OutOfSample | None, paper: Paper | None) -> list[str]:
    """Negative evidence with a sufficient sample. Each entry is a reason."""
    r = c.rejection
    out: list[str] = []
    sections: list[tuple[str, SectionStats]] = []
    if sufficient_oos(c, oos):
        assert oos is not None  # noqa: S101
        sections.append(("out of sample", oos.stats))
    if sufficient_paper(c, paper):
        assert paper is not None  # noqa: S101
        sections.append(("paper", paper.stats))
    for label, s in sections:
        if r.reject_non_positive_expectancy and (s.expectancy is None or s.expectancy <= 0):
            out.append(f"{label}: net expectancy {s.expectancy} per trade is not positive")
        if s.profit_factor is not None and s.profit_factor < r.reject_profit_factor_below:
            out.append(
                f"{label}: profit factor {s.profit_factor:.2f} < {r.reject_profit_factor_below}"
            )
        if s.drawdown is not None and s.drawdown > r.reject_drawdown_above:
            out.append(f"{label}: drawdown {s.drawdown:.1%} > {r.reject_drawdown_above:.0%}")
    if (
        r.reject_overfit_and_unprofitable
        and oos is not None
        and sufficient_oos(c, oos)
        and oos.pbo is not None
        and oos.pbo > c.promotion.max_pbo
        and oos.stats.net_pnl <= 0
    ):
        out.append(
            f"out of sample: likely overfit (PBO {oos.pbo:.2f}) and unprofitable "
            f"(net {oos.stats.net_pnl:.2f})"
        )
    return out


# ---------------------------------------------------------------------- criteria
def historical_criteria(c: EvidenceCriteria, oos: OutOfSample | None) -> list[Criterion]:
    sec = EvidenceSection.OUT_OF_SAMPLE
    p, smp = c.promotion, c.sample
    if oos is None:
        return [
            Criterion(
                "OOS_EVIDENCE", sec, S.NOT_AVAILABLE, None, None, "no walk-forward validation run"
            )
        ]
    s = oos.stats
    out = [
        _sample("OOS_TRADES", sec, s.trades, smp.min_oos_trades, "closed out-of-sample trades"),
        _sample("OOS_SESSIONS", sec, s.sessions, smp.min_oos_sessions, "out-of-sample sessions"),
        _profit_factor(sec, s, p.min_profit_factor),
        _above("NET_EXPECTANCY", sec, s.expectancy, p.min_net_expectancy, "net expectancy"),
        _at_least(
            "DEFLATED_SHARPE", sec, oos.deflated_sharpe, p.min_deflated_sharpe, "deflated Sharpe"
        ),
        _at_most("PBO", sec, oos.pbo, p.max_pbo, "probability of backtest overfitting"),
        _at_most("MAX_DRAWDOWN", sec, s.drawdown, p.max_drawdown, "out-of-sample drawdown"),
        _at_most(
            "TOP5_SHARE", sec, s.top5_share, p.max_top5_share, "share of profit in 5 best trades"
        ),
        _at_most(
            "ASSET_CONCENTRATION",
            sec,
            s.largest_asset_share,
            p.max_single_asset_share,
            "largest asset's share of net profit",
        ),
        _above(
            "COST_STRESS",
            sec,
            oos.cost_stress_net_pnl,
            0.0,
            f"net trade P/L at {p.cost_stress_multiplier:g}x costs",
        ),
    ]
    out = _no_profit_is_not_a_pass(out, s)
    if p.require_positive_oos_return:
        out.append(_above("OOS_NET_RETURN", sec, s.net_return, 0.0, "out-of-sample net return"))
    if p.require_benchmark_sharpe:
        if s.sharpe is None or oos.benchmark_sharpe is None:
            out.append(
                Criterion(
                    "BENCHMARK_SHARPE",
                    sec,
                    S.NOT_AVAILABLE,
                    s.sharpe,
                    oos.benchmark_sharpe,
                    "Sharpe or benchmark Sharpe not available",
                )
            )
        else:
            out.append(
                _at_least(
                    "BENCHMARK_SHARPE",
                    sec,
                    s.sharpe,
                    oos.benchmark_sharpe,
                    "out-of-sample Sharpe vs benchmark Sharpe",
                )
            )
    # Regimes: enough trades in enough benchmark trend regimes, and none of them losing.
    usable = {
        k: v
        for k, v in s.by_regime.items()
        if k != "UNKNOWN" and int(v.get("trades", 0)) >= smp.min_regime_trades
    }
    out.append(
        _sample(
            "REGIME_COVERAGE",
            sec,
            len(usable),
            smp.min_regimes,
            f"trend regimes with at least {smp.min_regime_trades} trades",
        )
    )
    losing = sorted(k for k, v in usable.items() if float(v.get("net_pnl", 0.0)) <= 0)
    if usable:
        out.append(
            Criterion(
                "REGIME_CONSISTENCY",
                sec,
                S.FAIL if losing else S.PASS,
                losing,
                "no losing regime",
                "losing in " + ", ".join(losing) if losing else "profitable in every regime",
            )
        )
    for direction, v in sorted(s.by_direction.items()):
        n = int(v.get("trades", 0))
        if n == 0:
            continue
        if n < smp.min_direction_trades:
            out.append(
                _sample(
                    f"DIRECTION_{direction}",
                    sec,
                    n,
                    smp.min_direction_trades,
                    f"{direction} trades",
                    blocking=False,
                )
            )
            continue
        out.append(
            _above(
                f"DIRECTION_{direction}",
                sec,
                v.get("expectancy"),
                0.0,
                f"{direction} net expectancy",
            )
        )
    # The untouched holdout, first read only.
    if oos.holdout_access_number is None:
        out.append(
            Criterion("HOLDOUT", sec, S.NOT_AVAILABLE, None, None, "the holdout was never read")
        )
    elif oos.holdout_access_number != 1:
        out.append(
            Criterion(
                "HOLDOUT",
                sec,
                S.NOT_AVAILABLE,
                oos.holdout_access_number,
                1,
                "only a first holdout read is evidence",
            )
        )
    elif (oos.holdout_trades or 0) < smp.min_holdout_trades:
        out.append(
            _sample(
                "HOLDOUT", sec, oos.holdout_trades or 0, smp.min_holdout_trades, "holdout trades"
            )
        )
    else:
        out.append(_above("HOLDOUT", sec, oos.holdout_net_pnl, 0.0, "holdout net trade P/L"))
    return out


def paper_criteria(
    c: EvidenceCriteria, paper: Paper | None, oos: OutOfSample | None
) -> list[Criterion]:
    sec = EvidenceSection.PAPER
    p, smp = c.promotion, c.sample
    if paper is None:
        return [Criterion("PAPER_EVIDENCE", sec, S.NOT_AVAILABLE, None, None, "no paper trading")]
    s = paper.stats
    out = [
        _sample("PAPER_TRADES", sec, s.trades, smp.min_paper_trades, "closed paper trades"),
        _sample("PAPER_SESSIONS", sec, s.sessions, smp.min_paper_sessions, "observed sessions"),
        _profit_factor(sec, s, p.min_profit_factor),
        _above("NET_EXPECTANCY", sec, s.expectancy, p.min_net_expectancy, "paper net expectancy"),
        _at_most("MAX_DRAWDOWN", sec, s.drawdown, p.max_drawdown, "paper drawdown"),
        _at_most(
            "TOP5_SHARE", sec, s.top5_share, p.max_top5_share, "share of profit in 5 best trades"
        ),
        _at_most(
            "ASSET_CONCENTRATION",
            sec,
            s.largest_asset_share,
            p.max_single_asset_share,
            "largest asset's share of net profit",
        ),
    ]
    out = _no_profit_is_not_a_pass(out, s)
    # Paper vs out of sample: is paper significantly worse? (one-sided Welch)
    ref = () if oos is None else oos.stats.returns
    test = st.welch_shift(ref, s.returns) if ref and s.returns else None
    if test is None:
        out.append(
            Criterion(
                "PAPER_CONSISTENCY",
                sec,
                S.INSUFFICIENT,
                None,
                p.paper_consistency_alpha,
                "too few trades to compare paper with out-of-sample returns",
            )
        )
    else:
        _, pvalue = test
        ok = pvalue >= p.paper_consistency_alpha
        out.append(
            Criterion(
                "PAPER_CONSISTENCY",
                sec,
                S.PASS if ok else S.FAIL,
                pvalue,
                p.paper_consistency_alpha,
                f"paper per-trade returns {'not ' if ok else ''}significantly below out of sample "
                f"(one-sided Welch p {pvalue:.3f})",
            )
        )
    if paper.entry_shortfall_bps is None or paper.assumed_cost_bps is None:
        out.append(
            Criterion(
                "EXECUTION_SHORTFALL",
                sec,
                S.INSUFFICIENT if paper.entry_fills == 0 else S.NOT_AVAILABLE,
                None,
                p.max_execution_shortfall_excess_bps,
                "no paper entry fills with a decision reference price",
            )
        )
    else:
        excess = paper.entry_shortfall_bps - paper.assumed_cost_bps
        out.append(
            _at_most(
                "EXECUTION_SHORTFALL",
                sec,
                excess,
                p.max_execution_shortfall_excess_bps,
                "paper entry shortfall above the backtest's assumption (bps)",
            )
        )
    return out


def _blocking(cs: list[Criterion], *statuses: CriterionStatus) -> list[Criterion]:
    return [x for x in cs if x.blocking and x.status in statuses]


def classify(c: EvidenceCriteria, oos: OutOfSample | None, paper: Paper | None) -> Judgement:
    rejected = rejections(c, oos, paper)
    hist = historical_criteria(c, oos)
    pap = paper_criteria(c, paper, oos)
    criteria = hist + pap
    if rejected:
        return Judgement(Classification.REJECTED, criteria, rejected, rejected)
    missing = _blocking(hist, S.INSUFFICIENT, S.NOT_AVAILABLE)
    failed = _blocking(hist, S.FAIL)
    if not sufficient_oos(c, oos):
        # Below the sample minimum nothing is concluded, good or bad.
        return Judgement(
            Classification.INSUFFICIENT_EVIDENCE, criteria, [x.message for x in missing], []
        )
    if failed:
        return Judgement(
            Classification.NOT_DEMONSTRATED, criteria, [x.message for x in failed + missing], []
        )
    if missing:
        return Judgement(
            Classification.INSUFFICIENT_EVIDENCE, criteria, [x.message for x in missing], []
        )
    if not sufficient_paper(c, paper):
        why = [x.message for x in _blocking(pap, S.INSUFFICIENT, S.NOT_AVAILABLE)][:3]
        return Judgement(
            Classification.CONTINUE_PAPER,
            criteria,
            ["every out-of-sample criterion is met; paper evidence is not yet sufficient", *why],
            [],
        )
    pfail = _blocking(pap, S.FAIL)
    pmissing = _blocking(pap, S.INSUFFICIENT, S.NOT_AVAILABLE)
    if pfail:
        return Judgement(
            Classification.NOT_DEMONSTRATED, criteria, [x.message for x in pfail + pmissing], []
        )
    if pmissing:
        return Judgement(Classification.CONTINUE_PAPER, criteria, [x.message for x in pmissing], [])
    return Judgement(
        Classification.CANDIDATE_FOR_REVIEW,
        criteria,
        ["every criterion of the protocol is met: a human review may consider promotion"],
        [],
    )


# ---------------------------------------------------------------------- portfolio
@dataclass(frozen=True)
class PortfolioEvidence:
    oos_trades: int
    oos_source: str  # "EVALUATION run <id>" or "sum of strategy runs"
    paper_trades: int
    paper_sessions: int
    paper_drawdown: float | None
    paper_net_pnl: float | None
    open_critical_alerts: int
    reconciliation_clean: bool | None  # None: no reconciliation recorded
    reproduction_identical: bool | None  # None: nothing reproduced
    audit_status: str | None
    largest_asset_share: float | None


def classify_portfolio(
    c: EvidenceCriteria, pe: PortfolioEvidence, strategies: dict[str, Classification]
) -> tuple[Classification, ReadinessVerdict, list[Criterion]]:
    r = c.portfolio
    sec_o, sec_p = EvidenceSection.OUT_OF_SAMPLE, EvidenceSection.PAPER
    cs = [
        _sample("PORTFOLIO_OOS_TRADES", sec_o, pe.oos_trades, r.min_oos_trades, "OOS trades"),
        _sample("PORTFOLIO_PAPER_TRADES", sec_p, pe.paper_trades, r.min_paper_trades, "trades"),
        _sample(
            "PORTFOLIO_PAPER_SESSIONS",
            sec_p,
            pe.paper_sessions,
            r.min_paper_sessions,
            "observed paper sessions",
        ),
        _at_most(
            "PORTFOLIO_PAPER_DRAWDOWN",
            sec_p,
            pe.paper_drawdown,
            r.max_paper_drawdown,
            "paper account drawdown",
        ),
        _at_most(
            "OPEN_CRITICAL_ALERTS",
            sec_p,
            float(pe.open_critical_alerts),
            float(r.max_open_critical_alerts),
            "open critical alerts",
        ),
        _at_most(
            "PORTFOLIO_ASSET_CONCENTRATION",
            sec_p,
            pe.largest_asset_share,
            r.max_single_asset_share,
            "largest asset's share of paper net profit",
        ),
    ]
    if r.require_clean_reconciliation:
        cs.append(
            Criterion(
                "RECONCILIATION",
                sec_p,
                S.NOT_AVAILABLE
                if pe.reconciliation_clean is None
                else (S.PASS if pe.reconciliation_clean else S.FAIL),
                pe.reconciliation_clean,
                True,
                "latest accounting, reservation and execution reconciliations",
            )
        )
    if r.require_reproduction:
        cs.append(
            Criterion(
                "REPRODUCTION",
                sec_o,
                S.NOT_AVAILABLE
                if pe.reproduction_identical is None
                else (S.PASS if pe.reproduction_identical else S.FAIL),
                pe.reproduction_identical,
                True,
                "reproductions of the validation runs give identical results",
            )
        )
    allowed = [a.value for a in r.allowed_audit_statuses]
    cs.append(
        Criterion(
            "DATA_AUDIT",
            sec_o,
            S.NOT_AVAILABLE
            if pe.audit_status is None
            else (S.PASS if pe.audit_status in allowed else S.FAIL),
            pe.audit_status,
            allowed,
            "historical data audit status",
        )
    )
    if pe.paper_trades >= r.min_paper_trades and (pe.paper_net_pnl or 0.0) <= 0:
        cs.append(
            Criterion(
                "PORTFOLIO_PAPER_RESULT",
                sec_p,
                S.FAIL,
                pe.paper_net_pnl,
                0.0,
                "paper portfolio is not profitable after costs",
            )
        )
    candidates = [k for k, v in strategies.items() if v is Classification.CANDIDATE_FOR_REVIEW]
    all_rejected = bool(strategies) and all(
        v is Classification.REJECTED for v in strategies.values()
    )
    cs.append(
        Criterion(
            "STRATEGY_CANDIDATES",
            sec_p,
            S.PASS if candidates else (S.FAIL if all_rejected else S.INSUFFICIENT),
            candidates,
            ">= 1",
            "strategies meeting every criterion: " + (", ".join(candidates) or "none"),
        )
    )
    if any(x.status is S.FAIL and x.code == "PORTFOLIO_PAPER_RESULT" for x in cs):
        return Classification.REJECTED, ReadinessVerdict.NOT_READY, cs
    blocking = _blocking(cs, S.FAIL, S.INSUFFICIENT, S.NOT_AVAILABLE)
    if not blocking:
        return Classification.CANDIDATE_FOR_REVIEW, ReadinessVerdict.READY_FOR_REVIEW, cs
    if _blocking(cs, S.FAIL):
        return Classification.NOT_DEMONSTRATED, ReadinessVerdict.NOT_READY, cs
    return Classification.INSUFFICIENT_EVIDENCE, ReadinessVerdict.NOT_READY, cs


__all__ = [
    "Criterion",
    "Judgement",
    "OutOfSample",
    "Paper",
    "PortfolioEvidence",
    "SectionStats",
    "classify",
    "classify_portfolio",
    "historical_criteria",
    "paper_criteria",
    "rejections",
    "sufficient_oos",
    "sufficient_paper",
]
