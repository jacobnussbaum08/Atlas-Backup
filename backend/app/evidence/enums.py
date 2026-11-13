"""Enumerations of the Phase 11A evidence program."""

from __future__ import annotations

from enum import StrEnum


class EvidenceSection(StrEnum):
    """The three kinds of evidence, never pooled (11A readiness reporting)."""

    # The development (in-sample) period of a walk-forward run: what the parameters were
    # chosen on. Shown for context and to measure degradation; never passes a criterion.
    BACKTEST = "BACKTEST"
    # Stitched walk-forward test windows plus the first read of the untouched holdout.
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    # Closed positions of the simulated paper broker, traded by the live system on data
    # that arrived after the decision. Never a replay.
    PAPER = "PAPER"


class Classification(StrEnum):
    """What the evidence supports for one strategy. None of these changes anything that
    trades; a human acts on them."""

    # Sufficient evidence that is negative under the protocol's rejection rules.
    REJECTED = "REJECTED"
    # Too few trades, sessions or regimes to conclude anything.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    # Enough evidence, not negative enough to reject, but a promotion criterion fails.
    NOT_DEMONSTRATED = "NOT_DEMONSTRATED"
    # Every historical (out-of-sample) criterion is met; paper evidence is not yet.
    CONTINUE_PAPER = "CONTINUE_PAPER"
    # Every criterion of the protocol is met. Promotion still needs an operator.
    CANDIDATE_FOR_REVIEW = "CANDIDATE_FOR_REVIEW"


class CriterionStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a status, not a password
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT"  # not enough data to evaluate it
    NOT_AVAILABLE = "NOT_AVAILABLE"  # the evidence it needs does not exist


class AuditStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a status, not a password
    WARN = "WARN"  # limitations that must be reported, none that invalidate the data
    FAIL = "FAIL"  # the dataset cannot support conclusions


class ReadinessVerdict(StrEnum):
    """Portfolio-level answer of a scorecard. There is no "ready for live trading"."""

    NOT_READY = "NOT_READY"  # evidence insufficient or negative: continue or stop paper trading
    READY_FOR_REVIEW = "READY_FOR_REVIEW"  # every portfolio gate met; a human decides on 11B


__all__ = [
    "AuditStatus",
    "Classification",
    "CriterionStatus",
    "EvidenceSection",
    "ReadinessVerdict",
]
