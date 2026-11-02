"""Market intelligence enumerations (Phase 3B). Stored as strings with CHECK constraints."""

from __future__ import annotations

from enum import StrEnum


class Dimension(StrEnum):
    TREND = "TREND"
    VOLATILITY = "VOLATILITY"
    STABILITY = "STABILITY"


class ClassificationQuality(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"  # computed from incomplete inputs; uncertainty raised accordingly
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"  # not computed: label UNCERTAIN, probability 1


class RunKind(StrEnum):
    LIVE = "LIVE"  # one decision time: now
    RECONSTRUCTION = "RECONSTRUCTION"  # a range of past decision times


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"


class KnowledgeBasis(StrEnum):
    """What "available at the decision time" means for a result's inputs.

    * RECORDED: inputs Atlas had stored by the decision time (ingested and available).
      Strictly point-in-time: what a strategy could have used then.
    * RECONSTRUCTED: inputs retrieved later whose bars had completed and settled by the
      decision time (session close + settle delay), at their newest stored revision.
      For research on past periods Atlas did not record live; it may reflect corrections
      published after the decision time, and is always labelled as such.
    """

    RECORDED = "RECORDED"
    RECONSTRUCTED = "RECONSTRUCTED"


class Scope(StrEnum):
    ASSET = "ASSET"
    MARKET = "MARKET"  # the broad US equity market, measured through the benchmark


class RelationKind(StrEnum):
    PAIR = "PAIR"
    BENCHMARK = "BENCHMARK"


__all__ = [
    "ClassificationQuality",
    "Dimension",
    "KnowledgeBasis",
    "RelationKind",
    "RunKind",
    "RunStatus",
    "Scope",
]
