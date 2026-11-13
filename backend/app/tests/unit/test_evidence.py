"""Phase 11A evidence program: protocol, classification rules and paper statistics.

Every input here is a deterministic TEST FIXTURE vector, not a result.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.evidence.classify import (
    OutOfSample,
    Paper,
    PortfolioEvidence,
    SectionStats,
    classify,
    classify_portfolio,
)
from app.evidence.data_audit import _simple_ratio
from app.evidence.enums import Classification, CriterionStatus, ReadinessVerdict
from app.evidence.paper import ClosedTrade, _stats_of
from app.evidence.protocol import EvidenceCriteria, RejectionRules

C = EvidenceCriteria()


def good_stats(**changes: object) -> SectionStats:
    """A TEST FIXTURE out-of-sample record meeting every default criterion."""
    base = SectionStats(
        trades=120,
        sessions=500,
        net_pnl=6000.0,
        gross_pnl=6500.0,
        costs=500.0,
        profit_factor=1.6,
        expectancy=50.0,
        win_rate=0.52,
        top5_share=0.2,
        largest_asset_share=0.15,
        drawdown=0.05,
        net_return=0.06,
        sharpe=1.1,
        returns=tuple(0.004 + 0.01 * ((i % 7) - 3) / 3 for i in range(120)),
        by_direction={
            "LONG": {"trades": 80, "net_pnl": 4500.0, "expectancy": 56.0},
            "SHORT": {"trades": 40, "net_pnl": 1500.0, "expectancy": 37.5},
        },
        by_regime={
            "UP": {"trades": 70, "net_pnl": 4000.0},
            "DOWN": {"trades": 50, "net_pnl": 2000.0},
        },
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def good_oos(**changes: object) -> OutOfSample:
    base = OutOfSample(
        stats=good_stats(),
        run_id="fixture",
        benchmark_sharpe=0.8,
        deflated_sharpe=0.97,
        pbo=0.2,
        cost_stress_net_pnl=3000.0,
        holdout_access_number=1,
        holdout_trades=25,
        holdout_net_pnl=800.0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def good_paper(**changes: object) -> Paper:
    base = Paper(
        stats=good_stats(trades=40, sessions=80, drawdown=0.03),
        entry_shortfall_bps=12.0,
        assumed_cost_bps=7.0,
        entry_fills=40,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def status_of(judgement: Any, code: str, section: str | None = None) -> CriterionStatus:
    found = [
        c
        for c in judgement.criteria
        if c.code == code and (section is None or c.section.value == section)
    ]
    assert found, code
    status: CriterionStatus = found[0].status
    return status


# ---------------------------------------------------------------------- protocol
def test_the_default_protocol_is_deterministic_and_frozen():
    assert EvidenceCriteria().sha256() == EvidenceCriteria().sha256()
    assert C.portfolio.min_paper_trades == 200  # spec 20.1
    assert C.promotion.min_profit_factor == 1.2
    assert C.promotion.max_drawdown == 0.10  # spec 2: maximum drawdown threshold
    with pytest.raises(ValidationError):
        C.sample.min_oos_trades = 1  # type: ignore[misc]
    changed = EvidenceCriteria.model_validate({"sample": {"min_oos_trades": 31}})
    assert changed.sha256() != C.sha256()


def test_incoherent_protocols_are_refused():
    with pytest.raises(ValidationError, match="rejection profit factor"):
        EvidenceCriteria(rejection=RejectionRules(reject_profit_factor_below=1.5))
    with pytest.raises(ValidationError, match="rejection drawdown"):
        EvidenceCriteria(rejection=RejectionRules(reject_drawdown_above=0.05))
    with pytest.raises(ValidationError):
        EvidenceCriteria.model_validate({"unknown": 1})


# ---------------------------------------------------------------------- strategies
def test_every_criterion_met_is_only_a_candidate_for_review():
    j = classify(C, good_oos(), good_paper())
    assert j.classification is Classification.CANDIDATE_FOR_REVIEW
    assert all(c.status is CriterionStatus.PASS for c in j.criteria if c.blocking)
    assert "human review" in j.reasons[0]


def test_good_history_without_paper_evidence_means_continue_paper_trading():
    assert classify(C, good_oos(), None).classification is Classification.CONTINUE_PAPER
    few = good_paper(stats=good_stats(trades=12, sessions=80))
    assert classify(C, good_oos(), few).classification is Classification.CONTINUE_PAPER
    short = good_paper(stats=good_stats(trades=40, sessions=20))
    assert classify(C, good_oos(), short).classification is Classification.CONTINUE_PAPER


def test_too_few_out_of_sample_trades_or_sessions_is_insufficient_whatever_the_results():
    for trades, sessions in ((29, 500), (120, 249), (0, 0)):
        j = classify(C, good_oos(stats=good_stats(trades=trades, sessions=sessions)), good_paper())
        assert j.classification is Classification.INSUFFICIENT_EVIDENCE, (trades, sessions)
    assert classify(C, None, good_paper()).classification is Classification.INSUFFICIENT_EVIDENCE


def test_insufficient_evidence_can_never_become_a_candidate():
    """Sweep sample sizes around every minimum: below any of them, never a candidate."""
    for oos_t, oos_s, pap_t, pap_s in itertools.product(
        (0, 29, 30), (0, 249, 250), (0, 29, 30), (0, 59, 60)
    ):
        oos = good_oos(stats=good_stats(trades=oos_t, sessions=oos_s))
        paper = good_paper(stats=good_stats(trades=pap_t, sessions=pap_s, drawdown=0.03))
        j = classify(C, oos, paper)
        enough = oos_t >= 30 and oos_s >= 250 and pap_t >= 30 and pap_s >= 60
        if not enough:
            assert j.classification is not Classification.CANDIDATE_FOR_REVIEW


def test_negative_sufficient_evidence_rejects():
    losing = good_oos(stats=good_stats(net_pnl=-500.0, expectancy=-4.0, profit_factor=0.9))
    j = classify(C, losing, None)
    assert j.classification is Classification.REJECTED
    assert any("expectancy" in r for r in j.rejections)
    deep = good_oos(stats=good_stats(drawdown=0.2))
    assert classify(C, deep, None).classification is Classification.REJECTED
    overfit = good_oos(pbo=0.8, stats=good_stats(net_pnl=-1.0, expectancy=-0.01))
    assert classify(C, overfit, None).classification is Classification.REJECTED


def test_negative_paper_evidence_rejects_a_strategy_that_looked_good_out_of_sample():
    bad = good_paper(stats=good_stats(trades=40, sessions=80, net_pnl=-900.0, expectancy=-22.5))
    j = classify(C, good_oos(), bad)
    assert j.classification is Classification.REJECTED
    assert any(r.startswith("paper") for r in j.rejections)


def test_negative_but_insufficient_evidence_is_not_a_rejection():
    small = good_oos(stats=good_stats(trades=10, net_pnl=-500.0, expectancy=-50.0))
    assert classify(C, small, None).classification is Classification.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"stats": good_stats(profit_factor=1.1)}, "PROFIT_FACTOR"),
        ({"deflated_sharpe": 0.6}, "DEFLATED_SHARPE"),
        ({"pbo": 0.6}, "PBO"),
        ({"stats": good_stats(drawdown=0.12)}, "MAX_DRAWDOWN"),
        ({"stats": good_stats(top5_share=0.7)}, "TOP5_SHARE"),
        ({"stats": good_stats(largest_asset_share=0.8)}, "ASSET_CONCENTRATION"),
        ({"cost_stress_net_pnl": -10.0}, "COST_STRESS"),
        ({"benchmark_sharpe": 1.5}, "BENCHMARK_SHARPE"),
        ({"holdout_net_pnl": -100.0}, "HOLDOUT"),
    ],
)
def test_a_failed_historical_criterion_means_not_demonstrated(change, code):
    j = classify(C, good_oos(**change), good_paper())
    assert j.classification is Classification.NOT_DEMONSTRATED
    assert status_of(j, code, "OUT_OF_SAMPLE") is CriterionStatus.FAIL


def test_regimes_need_coverage_and_no_losing_regime():
    one = good_oos(stats=good_stats(by_regime={"UP": {"trades": 120, "net_pnl": 6000.0}}))
    j = classify(C, one, good_paper())
    assert j.classification is Classification.INSUFFICIENT_EVIDENCE
    assert status_of(j, "REGIME_COVERAGE") is CriterionStatus.INSUFFICIENT
    losing = good_oos(
        stats=good_stats(
            by_regime={
                "UP": {"trades": 70, "net_pnl": 7000.0},
                "DOWN": {"trades": 50, "net_pnl": -1000.0},
            }
        )
    )
    j = classify(C, losing, good_paper())
    assert j.classification is Classification.NOT_DEMONSTRATED
    assert status_of(j, "REGIME_CONSISTENCY") is CriterionStatus.FAIL


def test_long_and_short_are_judged_separately():
    losing_shorts = good_oos(
        stats=good_stats(
            by_direction={
                "LONG": {"trades": 80, "net_pnl": 7000.0, "expectancy": 87.5},
                "SHORT": {"trades": 40, "net_pnl": -1000.0, "expectancy": -25.0},
            }
        )
    )
    j = classify(C, losing_shorts, good_paper())
    assert j.classification is Classification.NOT_DEMONSTRATED
    assert status_of(j, "DIRECTION_SHORT") is CriterionStatus.FAIL
    few_shorts = good_oos(
        stats=good_stats(
            by_direction={
                "LONG": {"trades": 116, "net_pnl": 6100.0, "expectancy": 52.6},
                "SHORT": {"trades": 4, "net_pnl": -100.0, "expectancy": -25.0},
            }
        )
    )
    j = classify(C, few_shorts, good_paper())
    # Too few shorts to judge: reported, not blocking.
    assert status_of(j, "DIRECTION_SHORT") is CriterionStatus.INSUFFICIENT
    assert j.classification is Classification.CANDIDATE_FOR_REVIEW


def test_only_a_first_holdout_read_is_evidence():
    reread = good_oos(holdout_access_number=2)
    j = classify(C, reread, good_paper())
    assert status_of(j, "HOLDOUT") is CriterionStatus.NOT_AVAILABLE
    assert j.classification is Classification.INSUFFICIENT_EVIDENCE
    unread = good_oos(holdout_access_number=None)
    assert classify(C, unread, good_paper()).classification is (
        Classification.INSUFFICIENT_EVIDENCE
    )


def test_paper_significantly_worse_than_out_of_sample_is_not_demonstrated():
    worse = good_paper(
        stats=good_stats(
            trades=60,
            sessions=80,
            drawdown=0.03,
            returns=tuple(-0.01 + 0.002 * ((i % 5) - 2) for i in range(60)),
        )
    )
    j = classify(C, good_oos(), worse)
    assert status_of(j, "PAPER_CONSISTENCY") is CriterionStatus.FAIL
    assert j.classification is Classification.NOT_DEMONSTRATED


def test_paper_execution_worse_than_the_backtest_assumption_is_not_demonstrated():
    slow = good_paper(entry_shortfall_bps=40.0, assumed_cost_bps=7.0)
    j = classify(C, good_oos(), slow)
    assert status_of(j, "EXECUTION_SHORTFALL") is CriterionStatus.FAIL
    assert j.classification is Classification.NOT_DEMONSTRATED


# ---------------------------------------------------------------------- portfolio
def portfolio(**changes: object) -> PortfolioEvidence:
    base = PortfolioEvidence(
        oos_trades=600,
        oos_source="fixture",
        paper_trades=240,
        paper_sessions=90,
        paper_drawdown=0.04,
        paper_net_pnl=5000.0,
        open_critical_alerts=0,
        reconciliation_clean=True,
        reproduction_identical=True,
        audit_status="WARN",
        largest_asset_share=0.2,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_the_portfolio_is_ready_for_review_only_when_every_gate_passes():
    ok = {"a": Classification.CANDIDATE_FOR_REVIEW}
    cls, verdict, _ = classify_portfolio(C, portfolio(), ok)
    assert (cls, verdict) == (
        Classification.CANDIDATE_FOR_REVIEW,
        ReadinessVerdict.READY_FOR_REVIEW,
    )
    change: dict[str, Any]
    for change in (
        {"paper_trades": 199},
        {"paper_sessions": 59},
        {"oos_trades": 150},
        {"reconciliation_clean": None},
        {"reproduction_identical": None},
        {"audit_status": None},
    ):
        cls, verdict, _ = classify_portfolio(C, portfolio(**change), ok)
        assert verdict is ReadinessVerdict.NOT_READY, change
        assert cls is Classification.INSUFFICIENT_EVIDENCE, change
    for change in (
        {"reconciliation_clean": False},
        {"open_critical_alerts": 1},
        {"paper_drawdown": 0.11},
        {"audit_status": "FAIL"},
        {"reproduction_identical": False},
    ):
        cls, verdict, _ = classify_portfolio(C, portfolio(**change), ok)
        assert (cls, verdict) == (Classification.NOT_DEMONSTRATED, ReadinessVerdict.NOT_READY)


def test_without_a_candidate_strategy_the_portfolio_is_not_ready():
    unproven = {"a": Classification.CONTINUE_PAPER, "b": Classification.INSUFFICIENT_EVIDENCE}
    cls, verdict, _ = classify_portfolio(C, portfolio(), unproven)
    assert (cls, verdict) == (Classification.INSUFFICIENT_EVIDENCE, ReadinessVerdict.NOT_READY)
    rejected = {"a": Classification.REJECTED}
    cls, verdict, _ = classify_portfolio(C, portfolio(), rejected)
    assert (cls, verdict) == (Classification.NOT_DEMONSTRATED, ReadinessVerdict.NOT_READY)
    losing = classify_portfolio(
        C, portfolio(paper_net_pnl=-10.0), {"a": Classification.CANDIDATE_FOR_REVIEW}
    )
    assert losing[:2] == (Classification.REJECTED, ReadinessVerdict.NOT_READY)


# ---------------------------------------------------------------------- paper statistics
def test_paper_statistics_from_closed_trades():
    t0 = datetime(2026, 11, 2, 15, tzinfo=UTC)

    def trade(i: int, pnl: float, symbol: str = "AAA", direction: str = "LONG") -> ClosedTrade:
        return ClosedTrade(
            "momentum", symbol, direction, t0, t0 + timedelta(days=i + 1), pnl, 2.0, 10_000.0, None
        )

    trades = [
        trade(0, 300.0),
        trade(1, -100.0, "BBB"),
        trade(2, -200.0),
        trade(3, 100.0, "BBB", "SHORT"),
    ]
    s = _stats_of(trades, 10, 100_000.0)
    assert s.trades == 4
    assert s.net_pnl == 100.0
    assert s.profit_factor == pytest.approx(400.0 / 300.0)
    assert s.expectancy == 25.0
    assert s.win_rate == 0.5
    assert s.top5_share == pytest.approx(4.0)  # 400 of winners over 100 of net profit
    # Equity 100300 -> 100200 -> 100000 -> 100100: drawdown 300 / 100300.
    assert s.drawdown == pytest.approx(300.0 / 100_300.0)
    assert s.largest_asset_share == pytest.approx(1.0)  # AAA +100, BBB 0
    assert s.by_direction["SHORT"] == {"trades": 1, "net_pnl": 100.0, "expectancy": 100.0}
    assert s.returns[0] == pytest.approx(0.03)
    empty = _stats_of([], 0, 100_000.0)
    assert (empty.trades, empty.expectancy, empty.drawdown) == (0, None, None)


def test_split_ratios_are_checked_for_plausibility():
    for ratio in ("2", "3", "0.2", "1.5"):
        assert _simple_ratio(Decimal(ratio))
    assert _simple_ratio(Decimal("0.333333333333"))
    assert not _simple_ratio(Decimal("1.0473"))
