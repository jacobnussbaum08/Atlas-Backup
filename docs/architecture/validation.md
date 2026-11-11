# Validation laboratory and controlled learning (Phase 8B)

Chronological walk-forward validation, robustness and overfitting controls, controlled
parameter experimentation, ML candidate comparison, champion/challenger governance and
degradation monitoring. Decisions D-185 to D-199 in [`decisions.md`](decisions.md); operations
in [`../operations.md`](../operations.md#validation-and-governance-phase-8b).

**Evidence and candidates, not trading authority.** Validation replays stored history through
the Phase 8A engine (the paper-trading rules). Nothing here submits an order, changes a risk
limit, or promotes a strategy or model by itself. Out-of-sample means data that was not used to
choose the parameters; it is not evidence of future performance.

## Modules

| Module | Role |
|---|---|
| `app/learning/windows.py` | Session layouts: rolling/expanding training windows, optional validation windows, test windows, refit schedule, embargo from the longest label, the reserved holdout; `check_layout` re-verifies every invariant; `purge_trades` |
| `app/learning/search.py` | Parameter spaces (values or stepped ranges, cross-parameter constraints, the strategy's own parameter model), GRID / RANDOM / BAYESIAN search, neighbours, default spaces for the six families |
| `app/learning/evaluation.py` | Window metrics from a replay (net and gross returns, drawdown, Sharpe, Sortino, costs, turnover, benchmark-relative), trade statistics, segmentation, findings |
| `app/learning/statistics.py` | Sharpe, PSR, expected maximum Sharpe, deflated Sharpe, effective trials, Holm/Bonferroni, PBO (CSCV), CPCV and purged k-fold, stationary block bootstrap, Monte Carlo trade sequences, PSI, KS, ECE, Welch and proportion tests |
| `app/learning/regimes.py` | Point-in-time trend and volatility labels from the benchmark's own closes |
| `app/learning/walkforward.py` | `ExperimentConfig`, `WalkForwardRunner` (trials, folds, out-of-sample replays, robustness, conclusion), `derive` and `stressed_model` |
| `app/learning/service.py` | Create (pin and plan), execute, store, reproduce, holdout evaluation, prior-trial counting |
| `app/learning/models.py` | ML candidate comparison over stored 5A training runs |
| `app/learning/governance.py` | Champion/challenger lifecycle, evidence gates, approval, rejection, retirement, rollback, shadow evaluation |
| `app/learning/monitoring.py` | Degradation monitoring, versioned monitoring rules, alerts, rule-bound suspension |
| `app/learning/queries.py`, `cli.py`, `runtime.py` | Read models, CLI, API wiring (one validation thread per API process) |
| `app/api/v1/validation.py` | The REST API under `/api/v1/validation` |
| `app/database/models/learning.py` | 14 tables (migration 0019) |
| `frontend/src/pages/Validation*.tsx` | `/research` and `/research/runs/:runId` |

## Walk-forward method

```
| development ............................................ | embargo | holdout |
| train | embargo | [validation | embargo] | test |
        | train | embargo | ...              | test |            (ROLLING)
```

1. **Pin.** The base replay configuration is resolved once at creation: strategy version,
   risk configuration, execution model and the dataset's knowledge time. The experiment
   configuration stored with the run carries the pinned version numbers and knowledge time.
2. **Plan.** The layout is built and the holdout dates are written into the run row when it is
   created, before anything runs. A database trigger keeps them unchanged afterwards.
3. **Trials.** Trial 0 is always the stored (champion) parameter set. The search adds feasible
   points up to `max_trials - 1`. Each trial is replayed **once over the development period**,
   and its dataset is loaded only up to the development end, so holdout bars are never read.
4. **Folds.** Each trial's training-window score comes from the slice of its development replay:
   mark-to-market daily returns up to the window end, and trades purged at the boundaries. A
   test (`test_earlier_sessions_are_identical_whatever_comes_later`) checks that a replay through
   T and a replay through T + 15 sessions agree on every session up to T. The ranking score is
   the **median of the trial's score and its evaluated neighbours' scores**, which favours
   plateaus over isolated peaks. A candidate needs `min_selection_trades` closed trades to be
   scored. The best `top_k` are scored on the validation window (when one is configured). Ties
   go to the training score, then the trial number. With no scorable candidate, the stored
   parameters are kept and the run records why.
5. **Out of sample.** The selected parameters, and the stored ones as a champion baseline, are
   replayed fresh over each test window, with data loaded only up to that window's end. Test
   windows are stitched in order; each starts flat.
6. **Robustness** (below), **findings** and a **verdict**: INSUFFICIENT_EVIDENCE, NOT_ROBUST or
   CANDIDATE_FOR_REVIEW. There is no "proven" verdict.

EVALUATION experiments replay fixed configurations (one strategy, or several together and
each alone) over the same test windows, with no search.

### Purging and embargo

* **Embargo** = the longest label among all candidates: (holding horizon + signal time to live)
  × sessions per period. Weekly labels count five sessions per period. It separates
  train → validation, validation → test and development → holdout. A configured value below the
  minimum is refused.
* **Purge**: a window's trade statistics count only trades opened in it and closed by its last
  session. Trades opened before the window, or still open at its end, are counted as purged and
  excluded.
* CPCV and purged k-fold purge `embargo` training sessions **before** each test block (positions
  held into it) and embargo the same number **after** it.
* The ML engine's observation-level purge and embargo (5A `splits.py`) are unchanged and are what
  the model comparison relies on.

### The holdout

The final `holdout_sessions` sessions (default 20%, at least one test window) are reserved
before anything runs. Selection, search, robustness and regime labelling never load them.
`POST /validation/runs/{id}/holdout`, allowed only for a COMPLETED run, replays the run's
frozen final parameters there once per request. Each read is numbered across all runs of the
same strategies with an overlapping holdout. After the first read, the holdout is labelled as
no longer an untouched test. Governance accepts only a first read as evidence.

## Robustness and overfitting controls

| Method | Implementation | Assumptions / limitations (stored with every result) |
|---|---|---|
| PBO | CSCV over the trials' development returns, `pbo_groups` contiguous blocks, every half/half split; Sharpe as the measure | Same sessions for every trial; noisy with short blocks |
| CPCV | `cpcv_groups` blocks, every choice of `cpcv_test_groups`; selection on purged/embargoed training blocks, scored on the test blocks | Selection-only (no fitted state); lets later data select for earlier tests; supplements, never replaces, the walk-forward |
| Purged k-fold | CPCV with one test block | As CPCV |
| Deflated Sharpe | PSR against the expected maximum Sharpe of N null trials; N = effective trials of this run (correlation-adjusted) + every earlier trial of the strategy on overlapping dates | Returns assumed stationary and serially independent; optimistic with overlapping positions |
| Multiple testing | One-sided t-tests of each trial's mean daily return with Holm adjustment; the out-of-sample p-value with Bonferroni over all trials | Approximate under overlap |
| Parameter sensitivity | Neighbours of the final selection, one step along one parameter, replayed on the development period; the full surface of evaluated trials | Development period includes the selection windows |
| OOS degradation | In-sample vs out-of-sample net return and Sharpe per fold, and the champion's out-of-sample result | Short windows give noisy Sharpe ratios |
| Monte Carlo trades | Permutation (order only) and bootstrap of closed out-of-sample trades: drawdown and P/L quantiles | Trades treated as independent; ignores clustering |
| Block bootstrap | Stationary bootstrap of stitched out-of-sample daily returns, mean block = max(embargo, n^(1/3)) | Cannot create unseen regimes |
| Cost stress | The final parameters replayed fresh on every test window, with commissions, fees and borrow at 0×, 1×, 2×, 3×; breakeven multiplier | Fills unchanged; cost sensitivity, not walk-forward |
| Slippage stress | The same, with slippage, assumed half spread and impact at 1×, 2×, 4× | As above |
| Regime segmentation | Out-of-sample returns and trades by benchmark trend and volatility regime known the session before | One benchmark for every asset |

Every trial is recorded (`validation_trials`), including failed ones and sensitivity
neighbours. Later runs count them: `prior_trials` is the number of trials of earlier completed
(non-reproduction) runs of the same strategies on overlapping dates, and it enters the deflated
Sharpe. A reproduction inherits its original's count.

## Findings

`INSUFFICIENT_EVIDENCE` (fewer than 30 closed out-of-sample trades or 60 sessions, both
configurable), `UNPROFITABLE`, `UNPROFITABLE_AFTER_COSTS` (gross positive, net not),
`EXCESSIVE_DRAWDOWN`, `DEPENDS_ON_FEW_TRADES` (top five winners > 50% of net profit),
`DEGRADES_OUT_OF_SAMPLE` (out-of-sample Sharpe < half of in-sample), `REGIME_DEPENDENT`
(profitable in some trend regimes and losing in others, each with enough trades),
`LIKELY_OVERFIT` (PBO > 0.5), `COST_SENSITIVE` (profit gone at 2× costs), `PARAMETER_UNSTABLE`
(under half of the neighbours profitable). `CONSISTENT` requires sufficient evidence, positive
net P/L in most folds, a profit factor of at least 1.2 (spec 20.1), a deflated Sharpe of at
least 0.95, and none of the negative findings. CANDIDATE_FOR_REVIEW needs CONSISTENT and no
negative finding. Otherwise the verdict is NOT_ROBUST, or INSUFFICIENT_EVIDENCE whenever that
finding is present.

## ML model validation

`app/learning/models.py` compares the stored 5A training runs per dataset version: the same
data and the same untouched test period. Predictive figures (log loss and Brier against the
constant base rate, or R² for regression; walk-forward folds beating the baseline; validation →
test ECE) are kept apart from financial ones (the model's trade/no-trade policy against taking
every observation and taking none, net of the stated round-trip cost). **Incremental value**
needs all of the following:

* predictive improvement;
* a positive mean net return per observation, above take-all;
* still positive at twice the cost;
* at least 100 effective observations (trades ÷ mean label concurrency);
* significance after Bonferroni over every hyperparameter candidate tried on that dataset.

Classification accuracy is never used.

## Champion / challenger

| | Strategy | Model |
|---|---|---|
| Champion | The latest stored strategy version, which the live engine uses. Governed when an APPROVED candidate exists, otherwise shown as "INCUMBENT (not validated under 8B)" | The 5A APPROVED version |
| Challenger | A validated parameter set held in `learning_candidates`. **Not** a strategy version, so creating one changes nothing that trades | A 5A model version (CANDIDATE) |
| SHADOW gate | A COMPLETED run of the strategy alone, verdict CANDIDATE_FOR_REVIEW, frozen final parameters equal to the candidate's, and a **first** holdout read | 5A verdict PASSED and an 8B comparison showing incremental value; the 5A status moves CANDIDATE → VALIDATED → SHADOW |
| Shadow evidence | Challenger and champion replayed side by side over sessions completed **after** the candidate entered SHADOW (forward data); at least `min_shadow_sessions` | Shadow predictions recorded since SHADOW began; at least `min_shadow_predictions` |
| Approval | An operator, with a reason, naming the champion it replaces (`replaces`): the champion is retired in the same transaction and a **new strategy version** is created with the candidate's parameters | The 5A champion is RETIRED and the challenger APPROVED |
| Rollback | A RETIRED candidate that was APPROVED before is re-approved; the current champion is retired; a new strategy version with its parameters is created | 5A `reinstate` (RETIRED → APPROVED only for a version that was APPROVED before) |

Lifecycle: CANDIDATE → VALIDATING → SHADOW → APPROVED → RETIRED; CANDIDATE, VALIDATING and
SHADOW → REJECTED. REJECTED is final, and RETIRED → APPROVED happens only through rollback.
Events form a chain (one successor per event). Every change is an operator request with a reason
and is written to the audit log. The SYSTEM actor (the monitor) can never change a status.
Every decision writes a `learning_approvals` row holding the evidence it relied on and what it
replaced. An operator may also *adopt* the live version as the governed champion without
changing it, so that it can be rolled back to later.

Retiring a strategy champion without a replacement also disables the strategy (the safe
direction); re-enabling it is a separate, explicit operator action.

## Degradation monitoring

See the table in `app/learning/monitoring.py`. Reference: the out-of-sample trades of the newest
completed validation run of the strategy. Current: closed paper positions (7B) since the
champion was approved. Measurements below their minimum samples (30 trades, 200 feature
observations, 100 predictions) are INSUFFICIENT_DATA and never alert. Live snapshots overlap heavily, so KS p-values are tiny for trivial shifts: a KS measurement alerts only when significant *and* its statistic is at least 0.1 (warning) / 0.2 (breach); PSI uses 0.1 / 0.25. Without an
operator-approved rule the monitor only warns. Under an approved, versioned SUSPEND rule, a
BREACH of a *performance* measurement (expectancy, win rate, drawdown) disables the strategy.
Feature or prediction drift never suspends, because drift is not failure. The monitor never
enables anything, never changes a risk limit and never approves.

## Data and limits

* Statistics are analytical floats derived from the Decimal replays (D-194). Money of record
  stays in the 8A in-memory ledger and the paper ledger.
* The replay is slow, about 0.25–0.5 s per session per strategy on 21 assets, so searches are
  bounded (60 trials at most; the defaults are 10–20). Runs execute sequentially on one
  thread per process.
* Executable history in the development database is short: RAW daily bars start on 2026-06-25,
  so the replays have 88 sessions. Every real experiment therefore ends with insufficient
  evidence. See the Phase 8B report.
