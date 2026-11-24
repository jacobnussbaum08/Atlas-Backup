"""Cross-asset relationships (Phase 3B).

Algorithm ``cross-asset-v1`` over daily log returns of completed sessions:

* A return exists only between two *consecutive expected sessions* that both have a bar;
  a missing session never becomes a silent multi-day return.
* **Pairwise correlation** uses the returns both instruments have (pairwise-complete).
  Fewer than ``min_overlap`` shared returns: no coefficient, status
  ``INSUFFICIENT_OVERLAP``. Every coefficient carries a Fisher-z confidence interval and
  the number of observations; ``significant`` means the interval excludes zero, and is
  only a guide: returns are autocorrelated and many pairs are tested at once.
* **Correlation change**: the same pair over the preceding ``prior_window``.
* **Benchmark**: correlation, beta and relative log performance against the benchmark.
* **Common exposure** (absorption ratio): the share of total variance carried by the first
  principal component of the correlation matrix of instruments with complete overlap
  (power iteration on a matrix of at most ``max_assets`` rows; complete cases keep it
  positive semi-definite). Higher means assets move more as one.

Correlation describes co-movement in the window; it says nothing about cause, and past
relationships need not persist.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations, pairwise

from app.feature_engine.indicators import pearson, stdev
from app.market_intelligence.config import CorrelationConfig

ALGORITHM = "cross-asset-v1"
OK, INSUFFICIENT_OVERLAP = "OK", "INSUFFICIENT_OVERLAP"


@dataclass(frozen=True)
class PairCorrelation:
    asset_a: uuid.UUID
    asset_b: uuid.UUID
    status: str
    observations: int
    correlation: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    significant: bool | None = None
    prior_observations: int = 0
    prior_correlation: float | None = None
    change: float | None = None


@dataclass(frozen=True)
class BenchmarkRelation:
    asset_id: uuid.UUID
    status: str
    observations: int
    correlation: float | None = None
    beta: float | None = None
    relative_log_return: float | None = None


@dataclass
class CrossAssetResult:
    window_start: datetime | None
    window_end: datetime | None
    pairs: list[PairCorrelation] = field(default_factory=list)
    benchmark: list[BenchmarkRelation] = field(default_factory=list)
    average_correlation: float | None = None
    absorption_ratio: float | None = None
    absorption_assets: int = 0
    absorption_observations: int = 0
    notes: list[str] = field(default_factory=list)


def session_returns(
    closes: Mapping[datetime, float], sessions: Sequence[datetime]
) -> list[float | None]:
    """Log return per session slot ``1..n-1``; None unless both adjacent sessions have bars."""
    out: list[float | None] = []
    for before, after in pairwise(sessions):
        a, b = closes.get(before), closes.get(after)
        out.append(math.log(b / a) if a and b and a > 0 and b > 0 else None)
    return out


def _paired(
    xs: Sequence[float | None], ys: Sequence[float | None]
) -> tuple[list[float], list[float]]:
    pairs = [(x, y) for x, y in zip(xs, ys, strict=True) if x is not None and y is not None]
    return [p[0] for p in pairs], [p[1] for p in pairs]


def fisher_interval(r: float, n: int, z: float) -> tuple[float, float] | None:
    """Confidence interval of ``r`` via the Fisher z-transform; ``None`` for n <= 3.

    A perfect correlation (|r| = 1) has a degenerate interval [r, r]: the transform is
    infinite there, and clamping it would give an interval that excludes ``r`` itself.
    """
    if n <= 3:
        return None
    if abs(r) >= 1.0:
        return r, r
    centre = math.atanh(r)
    half = z / math.sqrt(n - 3)
    return math.tanh(centre - half), math.tanh(centre + half)


def top_eigenvalue(matrix: Sequence[Sequence[float]], iterations: int = 500) -> float:
    """Largest eigenvalue of a symmetric positive semi-definite matrix (power iteration)."""
    size = len(matrix)
    vector = [1.0 / math.sqrt(size)] * size
    value = 0.0
    for _ in range(iterations):
        product = [sum(row[j] * vector[j] for j in range(size)) for row in matrix]
        norm = math.sqrt(sum(p * p for p in product))
        if norm == 0:
            return 0.0
        vector = [p / norm for p in product]
        new = sum(
            vector[i] * sum(matrix[i][j] * vector[j] for j in range(size)) for i in range(size)
        )
        if abs(new - value) < 1e-12:
            return new
        value = new
    return value


def cross_asset(
    closes: Mapping[uuid.UUID, Mapping[datetime, float]],
    sessions: Sequence[datetime],
    cfg: CorrelationConfig,
    *,
    benchmark: uuid.UUID | None,
) -> CrossAssetResult:
    """Relationships among ``closes`` (benchmark included) over the newest sessions.

    ``sessions`` lists the expected daily bar event times, oldest first; at least
    ``window + prior_window + 1`` are used when available.
    """
    needed = cfg.window + cfg.prior_window + 1
    slots = list(sessions[-needed:])
    result = CrossAssetResult(
        window_start=slots[-cfg.window - 1] if len(slots) > cfg.window else None,
        window_end=slots[-1] if slots else None,
    )
    if len(slots) < cfg.window + 1:
        result.notes.append(f"{len(slots)} sessions; {cfg.window + 1} needed")
        return result
    returns = {asset: session_returns(series, slots) for asset, series in closes.items()}
    current = {a: r[-cfg.window :] for a, r in returns.items()}
    prior = {a: r[-cfg.window - cfg.prior_window : -cfg.window] for a, r in returns.items()}
    assets = sorted(a for a in closes if a != benchmark)

    computed: list[float] = []
    for a, b in combinations(assets, 2):
        xs, ys = _paired(current[a], current[b])
        if len(xs) < cfg.min_overlap:
            result.pairs.append(PairCorrelation(a, b, INSUFFICIENT_OVERLAP, len(xs)))
            continue
        r = pearson(xs, ys)
        if r is None:  # a constant series: correlation undefined, never reported as 0
            result.pairs.append(PairCorrelation(a, b, INSUFFICIENT_OVERLAP, len(xs)))
            continue
        interval = fisher_interval(r, len(xs), cfg.confidence_z)
        px, py = _paired(prior[a], prior[b])
        prior_r = pearson(px, py) if len(px) >= cfg.min_overlap else None
        computed.append(r)
        result.pairs.append(
            PairCorrelation(
                a,
                b,
                OK,
                len(xs),
                correlation=r,
                ci_low=None if interval is None else interval[0],
                ci_high=None if interval is None else interval[1],
                significant=None if interval is None else interval[0] > 0 or interval[1] < 0,
                prior_observations=len(px),
                prior_correlation=prior_r,
                change=None if prior_r is None else r - prior_r,
            )
        )
    if computed:
        result.average_correlation = sum(computed) / len(computed)

    if benchmark is not None and benchmark in current:
        for asset in assets:
            xs, ys = _paired(current[asset], current[benchmark])
            if len(xs) < cfg.min_overlap:
                result.benchmark.append(BenchmarkRelation(asset, INSUFFICIENT_OVERLAP, len(xs)))
                continue
            r = pearson(xs, ys)
            mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
            var_y = sum((y - mean_y) ** 2 for y in ys)
            beta = (
                sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / var_y
                if var_y > 0
                else None
            )
            result.benchmark.append(
                BenchmarkRelation(
                    asset,
                    OK if r is not None else INSUFFICIENT_OVERLAP,
                    len(xs),
                    correlation=r,
                    beta=beta,
                    relative_log_return=sum(xs) - sum(ys),
                )
            )
    _absorption(result, {a: current[a] for a in assets}, cfg)
    return result


def _absorption(
    result: CrossAssetResult, series: dict[uuid.UUID, list[float | None]], cfg: CorrelationConfig
) -> None:
    """First-component variance share over the complete-case returns of as many assets as
    keep at least ``min_overlap`` shared observations (dropping the most incomplete)."""
    members = sorted(series, key=lambda a: (-sum(r is not None for r in series[a]), str(a)))
    while len(members) >= 3:
        rows = [i for i in range(cfg.window) if all(series[a][i] is not None for a in members)]
        if len(rows) >= cfg.min_overlap:
            break
        members.pop()  # the least complete instrument
    else:
        result.notes.append("too few instruments with complete overlap for common exposure")
        return
    # ``rows`` are complete for every member, so each column is a full list of floats.
    columns = {a: [v for i in rows if (v := series[a][i]) is not None] for a in members}
    usable = [a for a in members if stdev(columns[a]) not in (None, 0.0)]
    if len(usable) < 3:
        result.notes.append("too few non-constant instruments for common exposure")
        return
    matrix = [
        [1.0 if a == b else pearson(columns[a], columns[b]) or 0.0 for b in usable] for a in usable
    ]
    result.absorption_ratio = top_eigenvalue(matrix) / len(usable)
    result.absorption_assets = len(usable)
    result.absorption_observations = len(rows)


__all__ = [
    "ALGORITHM",
    "BenchmarkRelation",
    "CrossAssetResult",
    "PairCorrelation",
    "cross_asset",
    "fisher_interval",
    "session_returns",
    "top_eigenvalue",
]
