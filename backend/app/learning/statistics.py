"""Statistics of the validation laboratory (Phase 8B). Pure functions on numbers.

Every function states its assumptions in its docstring; the report and the dashboard carry
the same text. Randomized methods take an explicit seed and use ``numpy.random.Generator``
(PCG64), so a recorded seed reproduces the result bit for bit.

References: Bailey & López de Prado (2012, 2014) for the probabilistic and deflated Sharpe
ratios and the expected maximum Sharpe ratio under the null; Bailey, Borwein, López de
Prado & Zhu (2017) for the probability of backtest overfitting (CSCV); López de Prado
(2018) for combinatorial purged cross-validation; Politis & Romano (1994) for the
stationary bootstrap.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy import stats as sps

EULER_GAMMA = 0.5772156649015329
Array = NDArray[np.float64]


def _arr(values: Sequence[float] | Array) -> Array:
    return np.asarray(values, dtype=np.float64)


# ---------------------------------------------------------------------- Sharpe family
def sharpe(returns: Sequence[float] | Array) -> float | None:
    """Per-period Sharpe ratio (mean / sample sd, risk-free 0); None if undefined."""
    r = _arr(returns)
    if r.size < 2:
        return None
    sd = float(r.std(ddof=1))
    if sd == 0 or not math.isfinite(sd):
        return None
    return float(r.mean()) / sd


def moments(returns: Sequence[float] | Array) -> tuple[float, float]:
    """(skewness, non-excess kurtosis) of the returns (normal: 0, 3)."""
    r = _arr(returns)
    if r.size < 3 or float(r.std()) == 0:
        return 0.0, 3.0
    return float(sps.skew(r)), float(sps.kurtosis(r, fisher=False))


def probabilistic_sharpe(
    sr: float, n: int, skew: float = 0.0, kurtosis: float = 3.0, benchmark: float = 0.0
) -> float | None:
    """P(true Sharpe > benchmark) given an observed per-period Sharpe ``sr`` from ``n``
    returns, allowing for non-normal returns (Bailey & López de Prado 2012). Assumes
    the returns are stationary and serially independent (overlapping trades break this,
    so the result is optimistic when positions overlap)."""
    if n < 2:
        return None
    denom = 1 - skew * sr + (kurtosis - 1) / 4 * sr * sr
    if denom <= 0:
        return None
    z = (sr - benchmark) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(sps.norm.cdf(z))


def expected_max_sharpe(trials: float, variance: float) -> float:
    """Expected maximum per-period Sharpe of ``trials`` independent strategies whose true
    Sharpe is zero, when the observed Sharpes have variance ``variance`` (the "false
    strategy" theorem). A selected Sharpe below this is what luck alone would produce."""
    if trials <= 1 or variance <= 0:
        return 0.0
    a = sps.norm.ppf(1 - 1 / trials)
    b = sps.norm.ppf(1 - 1 / (trials * math.e))
    return float(math.sqrt(variance) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b))


def effective_trials(returns_matrix: Array) -> tuple[float, float]:
    """(effective number of independent trials, average pairwise correlation) from a
    sessions x trials matrix: N_eff = rho + (1 - rho) N (an approximation; correlated
    trials count less than independent ones)."""
    m = np.asarray(returns_matrix, dtype=np.float64)
    n = m.shape[1] if m.ndim == 2 else 0
    if n <= 1:
        return float(n), 0.0
    sd = m.std(axis=0, ddof=1)
    usable = m[:, sd > 0]
    k = usable.shape[1]
    if k <= 1:
        return float(n), 0.0
    c = np.corrcoef(usable, rowvar=False)
    rho = float((c.sum() - k) / (k * (k - 1)))
    rho = min(max(rho, 0.0), 1.0)
    return rho + (1 - rho) * n, rho


@dataclass(frozen=True)
class DeflatedSharpe:
    sharpe: float  # per period
    observations: int
    trials: float  # effective trials used
    trials_recorded: int
    sharpe_variance: float
    expected_max_null: float
    psr_zero: float | None  # P(SR > 0), no deflation
    dsr: float | None  # P(SR > expected max under the null)

    def as_json(self) -> dict[str, Any]:
        return {
            "sharpe_per_session": self.sharpe,
            "sharpe_annualized": self.sharpe * math.sqrt(252),
            "observations": self.observations,
            "effective_trials": self.trials,
            "trials_recorded": self.trials_recorded,
            "trial_sharpe_variance": self.sharpe_variance,
            "expected_max_sharpe_under_null": self.expected_max_null,
            "probabilistic_sharpe_vs_zero": self.psr_zero,
            "deflated_sharpe": self.dsr,
        }


def deflated_sharpe(
    returns: Sequence[float] | Array,
    trial_sharpes: Sequence[float],
    *,
    trials: float | None = None,
) -> DeflatedSharpe | None:
    """Deflated Sharpe ratio of the selected strategy's ``returns`` after ``trials`` tries
    (default: ``len(trial_sharpes)``) whose per-period Sharpes were ``trial_sharpes``."""
    r = _arr(returns)
    sr = sharpe(r)
    if sr is None:
        return None
    skew, kurt = moments(r)
    finite = [s for s in trial_sharpes if s is not None and math.isfinite(s)]
    var = float(np.var(finite, ddof=1)) if len(finite) > 1 else 0.0
    n_trials = trials if trials is not None else float(len(finite))
    sr0 = expected_max_sharpe(n_trials, var)
    return DeflatedSharpe(
        sharpe=sr,
        observations=int(r.size),
        trials=n_trials,
        trials_recorded=len(trial_sharpes),
        sharpe_variance=var,
        expected_max_null=sr0,
        psr_zero=probabilistic_sharpe(sr, int(r.size), skew, kurt, 0.0),
        dsr=probabilistic_sharpe(sr, int(r.size), skew, kurt, sr0),
    )


# ---------------------------------------------------------------------- multiple testing
def t_test_mean(values: Sequence[float] | Array) -> tuple[float | None, float | None]:
    """(t statistic, one-sided p-value for mean > 0). Assumes independent observations."""
    v = _arr(values)
    if v.size < 2 or float(v.std(ddof=1)) == 0:
        return None, None
    t = float(v.mean() / (v.std(ddof=1) / math.sqrt(v.size)))
    return t, float(sps.t.sf(t, v.size - 1))


def holm(p_values: Sequence[float | None]) -> list[float | None]:
    """Holm-Bonferroni adjusted p-values (family-wise error control; valid under any
    dependence between the tests)."""
    indexed = sorted((p, i) for i, p in enumerate(p_values) if p is not None)
    m = len(indexed)
    out: list[float | None] = [None] * len(p_values)
    running = 0.0
    for rank, (p, i) in enumerate(indexed):
        adj = min(1.0, (m - rank) * p)
        running = max(running, adj)
        out[i] = running
    return out


def bonferroni(p: float | None, tests: float) -> float | None:
    return None if p is None else min(1.0, p * max(tests, 1.0))


# ---------------------------------------------------------------------- CSCV / PBO
def _groups(n_rows: int, groups: int) -> list[NDArray[np.int64]]:
    return [g.astype(np.int64) for g in np.array_split(np.arange(n_rows), groups)]


@dataclass(frozen=True)
class PBOResult:
    pbo: float
    combinations: int
    logits: list[float]
    is_best_oos_rank: list[float]  # relative rank (0..1) of the IS-best trial out of sample
    degradation_slope: float | None  # OLS slope of OOS on IS performance of the selected
    probability_oos_loss: float  # share of combinations whose selected trial lost OOS
    groups: int

    def as_json(self) -> dict[str, Any]:
        return {
            "pbo": self.pbo,
            "combinations": self.combinations,
            "groups": self.groups,
            "logits": self.logits,
            "selected_relative_oos_rank": self.is_best_oos_rank,
            "is_oos_degradation_slope": self.degradation_slope,
            "probability_selected_loses_oos": self.probability_oos_loss,
        }


def _score(block: Array) -> Array:
    """Per-column Sharpe of a block (0 where undefined), the CSCV performance measure."""
    mean = block.mean(axis=0)
    sd = block.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(sd > 0, mean / sd, 0.0)
    return np.asarray(s, dtype=np.float64)


def pbo_cscv(matrix: Array, groups: int = 8) -> PBOResult | None:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation:
    split the sessions x trials return matrix into ``groups`` (even) contiguous blocks; for
    every half/half split, pick the trial with the best in-sample Sharpe and record its
    out-of-sample rank. PBO = share of splits where that rank is below the median.
    Assumes the trials were all evaluated on the same sessions; with fewer than two trials
    or too few sessions per block it is not applicable (None)."""
    m = np.asarray(matrix, dtype=np.float64)
    if m.ndim != 2 or m.shape[1] < 2 or groups < 2 or groups % 2:
        return None
    if m.shape[0] < groups * 4:
        return None
    blocks = _groups(m.shape[0], groups)
    logits: list[float] = []
    ranks: list[float] = []
    is_perf: list[float] = []
    oos_perf: list[float] = []
    losses = 0
    n = m.shape[1]
    combos = list(itertools.combinations(range(groups), groups // 2))
    for combo in combos:
        train_idx = np.concatenate([blocks[g] for g in combo])
        test_idx = np.concatenate([blocks[g] for g in range(groups) if g not in combo])
        is_s = _score(m[train_idx])
        oos_s = _score(m[test_idx])
        best = int(np.argmax(is_s))  # ties: the lowest trial number
        rank = float((oos_s < oos_s[best]).sum() + 0.5 * ((oos_s == oos_s[best]).sum() - 1))
        w = (rank + 1) / (n + 1)
        ranks.append(w)
        logits.append(math.log(w / (1 - w)))
        is_perf.append(float(is_s[best]))
        oos_perf.append(float(oos_s[best]))
        if float(m[test_idx, best].sum()) < 0:
            losses += 1
    slope = None
    if len(set(is_perf)) > 1:
        slope = float(np.polyfit(is_perf, oos_perf, 1)[0])
    return PBOResult(
        pbo=float(np.mean([lg <= 0 for lg in logits])),
        combinations=len(combos),
        logits=logits,
        is_best_oos_rank=ranks,
        degradation_slope=slope,
        probability_oos_loss=losses / len(combos),
        groups=groups,
    )


# ---------------------------------------------------------------------- CPCV / purged k-fold
@dataclass(frozen=True)
class CVSplit:
    test_groups: tuple[int, ...]
    selected: int
    in_sample_sharpe: float
    oos_sharpe: float | None
    oos_mean: float
    train_sessions: int
    test_sessions: int
    purged_sessions: int
    embargoed_sessions: int

    def as_json(self) -> dict[str, Any]:
        return {
            "test_groups": list(self.test_groups),
            "selected_trial": self.selected,
            "in_sample_sharpe": self.in_sample_sharpe,
            "oos_sharpe": self.oos_sharpe,
            "oos_mean_return": self.oos_mean,
            "train_sessions": self.train_sessions,
            "test_sessions": self.test_sessions,
            "purged_sessions": self.purged_sessions,
            "embargoed_sessions": self.embargoed_sessions,
        }


def purged_combinatorial(
    matrix: Array, groups: int, test_groups: int, horizon: int, embargo: int
) -> list[CVSplit]:
    """CPCV on a sessions x trials return matrix (``test_groups=1`` is purged k-fold).

    For each choice of test blocks, the trial with the best training Sharpe is selected and
    scored on the test blocks. Training sessions within ``horizon`` sessions *before* a test
    block are purged (positions opened there are held into it), and those within
    ``embargo`` sessions *after* it are embargoed (positions opened in it are held into
    them, and serial correlation). This is a selection-only cross-validation: the
    strategies have no fitted state beyond the chosen parameters, and each trial's returns
    come from one continuous replay, so a test block's returns can include positions opened
    just before it under the same parameters. It supplements, never replaces, the
    chronological walk-forward."""
    m = np.asarray(matrix, dtype=np.float64)
    if m.ndim != 2 or m.shape[1] < 1 or test_groups >= groups:
        return []
    rows = m.shape[0]
    blocks = _groups(rows, groups)
    out: list[CVSplit] = []
    for combo in itertools.combinations(range(groups), test_groups):
        test = np.zeros(rows, dtype=bool)
        for g in combo:
            test[blocks[g]] = True
        train = ~test
        purged = np.zeros(rows, dtype=bool)
        embargoed = np.zeros(rows, dtype=bool)
        for g in combo:
            lo, hi = int(blocks[g][0]), int(blocks[g][-1])
            purged[max(0, lo - horizon) : lo] = True
            embargoed[hi + 1 : min(rows, hi + 1 + embargo)] = True
        purged &= train
        embargoed &= train & ~purged
        train &= ~purged & ~embargoed
        if train.sum() < 2 or test.sum() < 2:
            continue
        is_s = _score(m[train])
        best = int(np.argmax(is_s))
        oos = m[test, best]
        out.append(
            CVSplit(
                test_groups=combo,
                selected=best,
                in_sample_sharpe=float(is_s[best]),
                oos_sharpe=sharpe(oos),
                oos_mean=float(oos.mean()),
                train_sessions=int(train.sum()),
                test_sessions=int(test.sum()),
                purged_sessions=int(purged.sum()),
                embargoed_sessions=int(embargoed.sum()),
            )
        )
    return out


# ---------------------------------------------------------------------- resampling
def stationary_bootstrap_indices(
    n: int, mean_block: float, rng: np.random.Generator
) -> NDArray[np.int64]:
    """One stationary-bootstrap resample (Politis & Romano): blocks of geometric length with
    mean ``mean_block`` wrapping around the end, preserving short-range dependence."""
    p = 1.0 / max(mean_block, 1.0)
    idx = np.empty(n, dtype=np.int64)
    idx[0] = rng.integers(n)
    for i in range(1, n):
        idx[i] = rng.integers(n) if rng.random() < p else (idx[i - 1] + 1) % n
    return idx


def block_bootstrap(
    returns: Sequence[float] | Array,
    *,
    mean_block: float,
    resamples: int,
    seed: int,
) -> dict[str, Any] | None:
    """Stationary block bootstrap of daily returns: 5/50/95% quantiles of the per-session
    mean, Sharpe (annualized) and total return, and P(mean <= 0). Assumes stationarity
    within the sample; it cannot create regimes the sample never saw."""
    r = _arr(returns)
    if r.size < 20:
        return None
    rng = np.random.default_rng(seed)
    means, sharpes, totals = [], [], []
    for _ in range(resamples):
        s = r[stationary_bootstrap_indices(r.size, mean_block, rng)]
        means.append(float(s.mean()))
        sr = sharpe(s)
        sharpes.append(float("nan") if sr is None else sr * math.sqrt(252))
        totals.append(float(np.prod(1 + s) - 1))
    q = (5, 50, 95)
    sharpe_arr = np.asarray(sharpes)
    sharpe_arr = sharpe_arr[np.isfinite(sharpe_arr)]
    return {
        "resamples": resamples,
        "mean_block_sessions": mean_block,
        "seed": seed,
        "observations": int(r.size),
        "mean_return_quantiles": dict(
            zip(map(str, q), np.percentile(means, q).tolist(), strict=True)
        ),
        "sharpe_annualized_quantiles": None
        if sharpe_arr.size == 0
        else dict(zip(map(str, q), np.percentile(sharpe_arr, q).tolist(), strict=True)),
        "total_return_quantiles": dict(
            zip(map(str, q), np.percentile(totals, q).tolist(), strict=True)
        ),
        "probability_mean_not_positive": float(np.mean(np.asarray(means) <= 0)),
    }


def max_drawdown_path(pnls: Array, capital: float) -> float:
    equity = capital + np.cumsum(pnls)
    peaks = np.maximum.accumulate(np.concatenate([[capital], equity]))[1:]
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peaks > 0, (peaks - equity) / peaks, 0.0)
    return float(dd.max()) if dd.size else 0.0


def monte_carlo_trades(
    pnls: Sequence[float] | Array, capital: float, *, resamples: int, seed: int
) -> dict[str, Any] | None:
    """Trade-sequence Monte Carlo on closed trades' net P/L. *Permutation* reorders the same
    trades (final P/L fixed; shows how much drawdown depends on luck of ordering);
    *bootstrap* draws trades with replacement (final P/L varies too). Assumes trades are
    exchangeable and independent: it ignores overlap in time and regime clustering, so it
    understates drawdown risk when losses cluster."""
    p = _arr(pnls)
    if p.size < 10:
        return None
    rng = np.random.default_rng(seed)
    perm_dd, boot_dd, boot_final = [], [], []
    for _ in range(resamples):
        perm_dd.append(max_drawdown_path(rng.permutation(p), capital))
        b = p[rng.integers(0, p.size, p.size)]
        boot_dd.append(max_drawdown_path(b, capital))
        boot_final.append(float(b.sum()))
    q = (5, 50, 95)
    return {
        "resamples": resamples,
        "seed": seed,
        "trades": int(p.size),
        "observed_max_drawdown": max_drawdown_path(p, capital),
        "observed_net_pnl": float(p.sum()),
        "permutation_max_drawdown_quantiles": dict(
            zip(map(str, q), np.percentile(perm_dd, q).tolist(), strict=True)
        ),
        "bootstrap_max_drawdown_quantiles": dict(
            zip(map(str, q), np.percentile(boot_dd, q).tolist(), strict=True)
        ),
        "bootstrap_net_pnl_quantiles": dict(
            zip(map(str, q), np.percentile(boot_final, q).tolist(), strict=True)
        ),
        "probability_net_loss": float(np.mean(np.asarray(boot_final) < 0)),
    }


# ---------------------------------------------------------------------- drift
def psi(
    reference: Sequence[float] | Array, current: Sequence[float] | Array, bins: int = 10
) -> float | None:
    """Population stability index with bins at the reference deciles (0.1/0.25 are the
    customary warning/breach levels; an empirical convention, not a test)."""
    ref = _arr(reference)
    cur = _arr(current)
    ref = ref[np.isfinite(ref)]
    cur = cur[np.isfinite(cur)]
    if ref.size < bins * 5 or cur.size < bins * 5:
        return None
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
    if edges.size < 3:
        return None
    edges[0], edges[-1] = -np.inf, np.inf
    r = np.histogram(ref, edges)[0] / ref.size
    c = np.histogram(cur, edges)[0] / cur.size
    eps = 1e-6
    r = np.clip(r, eps, None)
    c = np.clip(c, eps, None)
    return float(np.sum((c - r) * np.log(c / r)))


def ks_test(
    reference: Sequence[float] | Array, current: Sequence[float] | Array
) -> tuple[float, float] | None:
    """Two-sample Kolmogorov-Smirnov (statistic, p-value); assumes independent samples."""
    ref = _arr(reference)
    cur = _arr(current)
    ref = ref[np.isfinite(ref)]
    cur = cur[np.isfinite(cur)]
    if ref.size < 20 or cur.size < 20:
        return None
    res = sps.ks_2samp(ref, cur)
    return float(res.statistic), float(res.pvalue)


def expected_calibration_error(
    probabilities: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> float | None:
    p = _arr(probabilities)
    y = _arr(outcomes)
    if p.size == 0 or p.size != y.size:
        return None
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    total = 0.0
    for b in range(bins):
        mask = idx == b
        if mask.any():
            total += mask.mean() * abs(float(p[mask].mean()) - float(y[mask].mean()))
    return total


def welch_shift(
    reference: Sequence[float] | Array, current: Sequence[float] | Array
) -> tuple[float, float] | None:
    """Welch t-test that the current mean is *below* the reference mean: (t, one-sided p)."""
    ref = _arr(reference)
    cur = _arr(current)
    if ref.size < 2 or cur.size < 2:
        return None
    res = sps.ttest_ind(cur, ref, equal_var=False, alternative="less")
    if not math.isfinite(float(res.statistic)):
        return None
    return float(res.statistic), float(res.pvalue)


def proportion_drop(
    ref_hits: int, ref_n: int, cur_hits: int, cur_n: int
) -> tuple[float, float] | None:
    """Two-proportion z-test that the current rate is below the reference: (z, one-sided p)."""
    if ref_n == 0 or cur_n == 0:
        return None
    pooled = (ref_hits + cur_hits) / (ref_n + cur_n)
    se = math.sqrt(pooled * (1 - pooled) * (1 / ref_n + 1 / cur_n))
    if se == 0:
        return None
    z = (cur_hits / cur_n - ref_hits / ref_n) / se
    return z, float(sps.norm.cdf(z))


__all__ = [
    "CVSplit",
    "DeflatedSharpe",
    "PBOResult",
    "block_bootstrap",
    "bonferroni",
    "deflated_sharpe",
    "effective_trials",
    "expected_calibration_error",
    "expected_max_sharpe",
    "holm",
    "ks_test",
    "max_drawdown_path",
    "moments",
    "monte_carlo_trades",
    "pbo_cscv",
    "probabilistic_sharpe",
    "proportion_drop",
    "psi",
    "purged_combinatorial",
    "sharpe",
    "stationary_bootstrap_indices",
    "t_test_mean",
    "welch_shift",
]
