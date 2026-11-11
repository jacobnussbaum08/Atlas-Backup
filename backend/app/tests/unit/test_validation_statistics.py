"""Phase 8B: statistics of the validation laboratory on deterministic test vectors."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.learning import statistics as st


def test_sharpe_and_moments_on_a_known_vector() -> None:
    r = [0.01, -0.005, 0.002, 0.004, -0.001]
    mean = sum(r) / 5
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / 4)
    assert st.sharpe(r) == pytest.approx(mean / sd)
    assert st.sharpe([0.01]) is None
    assert st.sharpe([0.01, 0.01]) is None  # zero volatility
    skew, kurt = st.moments(np.random.default_rng(0).normal(size=20000))
    assert skew == pytest.approx(0, abs=0.05)
    assert kurt == pytest.approx(3, abs=0.1)


def test_probabilistic_sharpe_matches_the_normal_formula() -> None:
    # Normal returns (skew 0, kurtosis 3): z = SR sqrt(n-1) / sqrt(1 + SR^2 / 2).
    sr, n = 0.1, 252
    z = sr * math.sqrt(n - 1) / math.sqrt(1 + sr * sr / 2)
    expected = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    assert st.probabilistic_sharpe(sr, n) == pytest.approx(expected)
    assert st.probabilistic_sharpe(0.0, n) == pytest.approx(0.5)


def test_expected_maximum_sharpe_under_the_null_matches_simulation() -> None:
    rng = np.random.default_rng(7)
    trials, var = 50, 0.01
    simulated = np.mean([rng.normal(0, math.sqrt(var), trials).max() for _ in range(4000)])
    assert st.expected_max_sharpe(trials, var) == pytest.approx(simulated, rel=0.05)
    assert st.expected_max_sharpe(1, var) == 0.0


def test_deflation_lowers_confidence_as_trials_grow() -> None:
    rng = np.random.default_rng(1)
    returns = rng.normal(0.0008, 0.01, 252)
    trial_sharpes = list(rng.normal(0, 0.05, 40))
    one = st.deflated_sharpe(returns, trial_sharpes, trials=1)
    many = st.deflated_sharpe(returns, trial_sharpes, trials=40)
    lots = st.deflated_sharpe(returns, trial_sharpes, trials=400)
    assert one is not None
    assert many is not None
    assert lots is not None
    assert one.dsr is not None
    assert many.dsr is not None
    assert lots.dsr is not None
    assert one.dsr > many.dsr > lots.dsr
    assert many.expected_max_null > 0


def test_effective_trials_count_correlated_trials_less() -> None:
    rng = np.random.default_rng(2)
    base = rng.normal(size=(500, 1))
    identical = np.hstack([base] * 10)
    independent = rng.normal(size=(500, 10))
    n_same, rho_same = st.effective_trials(identical)
    n_ind, rho_ind = st.effective_trials(independent)
    assert rho_same == pytest.approx(1.0)
    assert n_same == pytest.approx(1.0)
    assert n_ind > 9
    assert rho_ind < 0.05


def test_holm_and_bonferroni_adjustments() -> None:
    assert st.holm([0.01, 0.04, 0.03, None]) == pytest.approx([0.03, 0.06, 0.06, None])
    assert st.bonferroni(0.01, 20) == pytest.approx(0.2)
    assert st.bonferroni(0.2, 20) == 1.0


def test_pbo_is_low_for_a_genuinely_better_trial_and_high_for_noise() -> None:
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.01, size=(400, 12))
    edge = noise.copy()
    edge[:, 5] += 0.004  # one trial is better in every period
    good = st.pbo_cscv(edge, groups=8)
    random = st.pbo_cscv(noise, groups=8)
    assert good is not None
    assert random is not None
    assert good.combinations == 70
    assert good.pbo < 0.1
    assert random.pbo > 0.25  # selecting on noise is overfitting about half the time
    assert st.pbo_cscv(noise[:, :1]) is None  # one trial: not applicable
    assert st.pbo_cscv(noise[:20]) is None  # too few sessions per block


def test_cpcv_purges_before_and_embargoes_after_each_test_block() -> None:
    m = np.random.default_rng(4).normal(size=(60, 3))
    splits = st.purged_combinatorial(m, groups=6, test_groups=2, horizon=3, embargo=2)
    assert len(splits) == 15  # C(6, 2)
    for s in splits:
        assert s.train_sessions + s.test_sessions + s.purged_sessions + s.embargoed_sessions == 60
        assert s.purged_sessions > 0 or s.test_groups[0] == 0
    kfold = st.purged_combinatorial(m, groups=5, test_groups=1, horizon=3, embargo=2)
    assert len(kfold) == 5
    first = kfold[0]  # the first block has no training before it to purge
    assert first.purged_sessions == 0
    assert first.embargoed_sessions == 2


def test_resampling_is_reproducible_from_its_seed() -> None:
    r = list(np.random.default_rng(5).normal(0.0005, 0.01, 120))
    a = st.block_bootstrap(r, mean_block=5, resamples=300, seed=11)
    b = st.block_bootstrap(r, mean_block=5, resamples=300, seed=11)
    c = st.block_bootstrap(r, mean_block=5, resamples=300, seed=12)
    assert a == b
    assert a != c
    pnls = list(np.random.default_rng(6).normal(10, 100, 40))
    m1 = st.monte_carlo_trades(pnls, 100_000, resamples=300, seed=1)
    m2 = st.monte_carlo_trades(pnls, 100_000, resamples=300, seed=1)
    assert m1 == m2
    assert m1 is not None
    # Permutation keeps the final P/L: only the order (and so the drawdown) changes.
    assert m1["observed_net_pnl"] == pytest.approx(sum(pnls))
    assert st.monte_carlo_trades(pnls[:5], 100_000, resamples=10, seed=1) is None


def test_stationary_bootstrap_keeps_blocks_contiguous() -> None:
    idx = st.stationary_bootstrap_indices(1000, 20.0, np.random.default_rng(0))
    steps = np.diff(idx)
    continuing = np.mean((steps == 1) | (steps == -999))
    assert 0.9 < continuing < 0.99  # about 1 - 1/20 of steps continue a block


def test_drift_statistics_on_known_distributions() -> None:
    rng = np.random.default_rng(8)
    ref = rng.normal(0, 1, 2000)
    same = rng.normal(0, 1, 2000)
    shifted = rng.normal(1, 1, 2000)
    psi_same, psi_shift = st.psi(ref, same), st.psi(ref, shifted)
    assert psi_same is not None
    assert psi_shift is not None
    assert psi_same < 0.05 < 0.25 < psi_shift
    assert st.psi(ref[:10], same[:10]) is None  # too few observations
    ks_same, ks_shift = st.ks_test(ref, same), st.ks_test(ref, shifted)
    assert ks_same is not None
    assert ks_shift is not None
    assert ks_same[1] > 0.01
    assert ks_shift[1] < 1e-10
    welch = st.welch_shift(ref, ref - 0.5)
    assert welch is not None
    assert welch[1] < 1e-10
    drop = st.proportion_drop(60, 100, 40, 100)
    assert drop is not None
    assert drop[1] < 0.01


def test_expected_calibration_error_on_known_vectors() -> None:
    assert st.expected_calibration_error([0.5] * 10, [1] * 5 + [0] * 5) == pytest.approx(0.0)
    assert st.expected_calibration_error([0.9] * 10, [0] * 10) == pytest.approx(0.9)
