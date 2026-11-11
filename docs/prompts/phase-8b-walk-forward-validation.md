ATLAS AI TRADER — PHASE 8B
WALK-FORWARD VALIDATION, STRATEGY OPTIMIZATION & MODEL LEARNING
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase.
First verify the Phase 7A/7B baseline and Phase 8A backtesting engine. Confirm that the integrated Git history is correct, migrations work, financial reconciliation passes, and the backtesting engine produces reproducible results.
Run existing tests. Stop if unresolved P0/P1 issues, look-ahead vulnerabilities, accounting discrepancies, or execution-integrity failures exist.
Preserve all working functionality.
OBJECTIVE
Build Atlas's validation and controlled-learning laboratory.
Determine whether strategies and ML models demonstrate robust out-of-sample performance across different assets, timeframes, and market regimes.
Develop candidate improvements without automatically replacing approved strategies or models.
1. WALK-FORWARD VALIDATION
Implement chronological walk-forward testing.
Support configurable:

* Training windows.
* Validation windows.
* Out-of-sample test windows.
* Rolling or expanding training windows.
* Prediction horizons.
* Embargo periods.
* Refit schedules.

Ensure every test period occurs after its training and validation data.
Prevent overlapping labels from contaminating adjacent datasets.
Use purging and embargo periods based on label horizons and strategy holding periods.
Preserve an untouched final evaluation period that is never used for parameter selection.
2. ADDITIONAL ROBUSTNESS METHODS
Support where appropriate:

* Purged time-series cross-validation.
* Combinatorial Purged Cross-Validation (CPCV).
* Parameter sensitivity analysis.
* Monte Carlo trade-sequence analysis.
* Bootstrap analysis with appropriate time-series safeguards.
* Transaction-cost stress testing.
* Slippage stress testing.
* Market-regime segmentation.

CPCV must supplement chronological walk-forward testing, not replace it.
Document assumptions and limitations for every validation method.
3. STRATEGY OPTIMIZATION
Build a controlled parameter experimentation system for all six strategy families.
Support:

* Parameter ranges.
* Parameter constraints.
* Experiment configurations.
* Historical test windows.
* Asset groups.
* Timeframes.
* Long/short separation.
* Regime-specific analysis.

Compare parameter configurations using net performance after realistic execution costs.
Avoid exhaustive brute-force searches that create unnecessary computational expense.
Use suitable search approaches such as:

* Grid search for small parameter spaces.
* Random search.
* Bayesian optimization where justified.

Every experiment must be recorded and reproducible.
Do not select strategies solely because they have the highest historical return.
Evaluate stability across neighboring parameter values and different market conditions.
4. OVERFITTING AND MULTIPLE-TESTING CONTROLS
This is critical.
Track the number of experiments and parameter combinations tested.
Identify selection bias caused by repeatedly searching historical data.
Where appropriate, implement or report:

* Probability of Backtest Overfitting.
* Deflated Sharpe Ratio.
* Multiple-testing considerations.
* Parameter stability.
* Out-of-sample degradation.
* Sensitivity to transaction costs.

Do not present a high historical Sharpe ratio as reliable evidence without accounting for strategy selection and repeated experimentation.
Keep the final untouched test period protected from all optimization.
5. STRATEGY EVALUATION
Evaluate every strategy independently and in combination.
Measure:

* Net return.
* Expectancy.
* Profit factor.
* Maximum drawdown.
* Sharpe ratio.
* Sortino ratio.
* Win rate.
* Average win/loss.
* Turnover.
* Transaction costs.
* Trade count.
* Holding period.
* Benchmark-relative performance.

Break results down by:

* Asset.
* Asset class.
* Timeframe.
* Long versus short.
* Market regime.
* Volatility environment.
* Testing period.

Identify strategies that:

* Perform consistently.
* Work only in certain regimes.
* Degrade out of sample.
* Depend on a small number of trades.
* Become unprofitable after realistic costs.
* Exhibit excessive drawdown.

Insufficient evidence must remain classified as insufficient evidence.
6. ML MODEL VALIDATION
Integrate the existing Phase 5A model registry and training pipeline.
Evaluate candidate models using the same chronological discipline.
Compare:

* Logistic regression.
* Random Forest.
* XGBoost.
* Existing approved models.
* Simple baseline predictions.

Evaluate predictive performance and downstream financial usefulness separately.
Track probability calibration and degradation.
Never promote a candidate solely because its classification accuracy improved.
A model must demonstrate incremental out-of-sample value after realistic transaction costs.
7. CHAMPION / CHALLENGER SYSTEM
Implement a controlled lifecycle.
Champion:

* Currently approved strategy or model version.

Challenger:

* Candidate version undergoing evaluation.

Lifecycle states:

* CANDIDATE.
* VALIDATING.
* SHADOW.
* APPROVED.
* REJECTED.
* RETIRED.

Support:

* Candidate creation.
* Evaluation.
* Comparison.
* Shadow testing.
* Approval.
* Rejection.
* Retirement.
* Rollback to a previous approved version.

Do not automatically promote candidates.
Require explicit human approval before replacing an approved strategy or model.
Preserve historical versions and decision provenance.
8. AUTOMATIC DEGRADATION MONITORING
Implement monitoring for approved strategies and models.
Detect:

* Performance deterioration.
* Distribution shifts.
* Feature drift.
* Prediction calibration changes.
* Increasing drawdown.
* Reduced trade quality.
* Increased transaction costs.
* Regime-specific failures.

Use statistically justified thresholds and minimum observation requirements.
The system may issue warnings or suspend a strategy under previously approved safety rules.
It may not modify hard risk limits or promote replacement models autonomously.
9. DATABASE AND API
Reuse existing backtest and model-registry infrastructure.
Implement necessary structures for:

* Validation runs.
* Walk-forward folds.
* Parameter experiments.
* Experiment configurations.
* Optimization results.
* Robustness metrics.
* Strategy comparisons.
* Champion/challenger states.
* Drift measurements.
* Approval records.

Expose APIs for:

* Validation jobs.
* Walk-forward results.
* Strategy comparisons.
* Parameter sensitivity.
* Model comparisons.
* Candidate evaluations.
* Drift monitoring.
* Promotion history.

Preserve immutable historical results.
10. DASHBOARD
Extend Atlas with a Strategy Research and Validation page.
Display:

* Walk-forward results.
* In-sample versus out-of-sample performance.
* Equity curves.
* Drawdowns.
* Parameter sensitivity.
* Regime breakdowns.
* Transaction-cost sensitivity.
* Strategy comparisons.
* Model comparisons.
* Champion/challenger status.
* Degradation alerts.

Clearly distinguish historical backtest performance from live paper-trading performance.
Never display fabricated results.
11. TESTING
Test:

* Chronological split correctness.
* Purging and embargo behavior.
* Overlapping labels.
* Final test-set isolation.
* Parameter-search reproducibility.
* Multiple-testing calculations.
* Walk-forward metrics.
* Parameter sensitivity.
* Model calibration.
* Drift detection.
* Candidate lifecycle.
* Approval requirements.
* Rollback.
* Database persistence.
* API and dashboard functionality.

Use deterministic datasets and test vectors.
Verify look-ahead prevention independently.
Preserve all existing tests.
12. SCOPE RESTRICTIONS
Implement Phase 8B only.
Do not implement:

* Autonomous real-money trading.
* Automatic promotion of strategies or models.
* Autonomous changes to hard risk limits.
* Live brokerage integration.
* Uncontrolled strategy self-modification.
* Automatic deployment of challenger models.

This phase develops evidence and candidates, not unrestricted trading authority.
13. ACCEPTANCE CRITERIA
Phase 8B is complete when:

1. Chronological walk-forward testing works.
2. Purging and embargo rules work.
3. Untouched final test data remains isolated.
4. CPCV is available where appropriate.
5. Parameter experimentation is reproducible.
6. Overfitting and multiple-testing risks are addressed.
7. Strategies are evaluated across regimes.
8. ML candidates are compared against baselines.
9. Champion/challenger lifecycle works.
10. Degradation monitoring works.
11. Human approval is required for promotion.
12. Historical results are immutable.
13. Existing Phase 8A results remain reproducible.
14. All previous tests pass.
15. New statistical, financial, and integration tests pass.

14. COMPLETION REPORT
Report:

1. Validation architecture.
2. Walk-forward methodology.
3. CPCV implementation.
4. Optimization methodology.
5. Overfitting safeguards.
6. Strategy evaluation results.
7. ML evaluation results.
8. Champion/challenger architecture.
9. Degradation monitoring.
10. Database and API changes.
11. Dashboard additions.
12. Files created and modified.
13. Test results.
14. Actual out-of-sample findings.
15. Known limitations.
16. Readiness for Phase 9.

Do not claim a strategy has a proven edge without sufficient independent evidence.
Do not confuse optimized historical performance with future predictive ability.
Implement incrementally and preserve existing functionality.
FINAL INSTRUCTION: Implement Phase 8B only. Prioritize out-of-sample robustness, statistical discipline, experiment reproducibility, and controlled model improvement.
