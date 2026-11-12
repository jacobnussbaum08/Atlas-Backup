Atlas AI Trader — Phase 11A: Extended Paper-Trading Validation
Read `ATLAS_MASTER_SPEC_V2.md` and the Phase 10A/10B completion reports. Inspect the actual codebase and existing validation architecture before making changes.
Preflight

* Confirm `main` is current and inspect the working tree.
* Verify Phases 1–10B and their tests.
* Review known limitations, historical-data coverage, and outstanding audit findings.
* Identify missing evidence required to evaluate strategy performance.
* Stop and report before implementation if any P0/P1 financial-integrity, execution-safety, or risk-control issue exists.

Objective
Establish whether Atlas is statistically credible, operationally reliable, and consistently profitable enough to justify further consideration. Do not assume profitability.
Implement

1. Historical validation
   * Expand historical coverage across multiple market regimes where data access and licensing permit.
   * Validate data quality, corporate actions, delisted assets, missing observations, and survivorship bias.
   * Prevent look-ahead bias and data leakage.
   * Use point-in-time data and reproducible datasets.
2. Strategy evaluation
   * Evaluate every strategy family independently and as part of the portfolio.
   * Measure long and short performance separately.
   * Compare against appropriate benchmarks and simple baseline strategies.
   * Analyze returns, volatility, Sharpe, Sortino, maximum drawdown, profit factor, win rate, expectancy, turnover, and transaction costs.
   * Include slippage, spreads, fees, borrow costs where applicable, and conservative execution assumptions.
3. Out-of-sample validation
   * Use walk-forward testing and CPCV where appropriate.
   * Keep training, validation, and test periods strictly separated.
   * Detect overfitting, regime dependence, and unstable parameters.
   * Preserve reproducibility and prevent optimization from contaminating final test results.
4. Live paper-trading observation
   * Run the existing automated paper-trading system under controlled conditions.
   * Collect durable evidence of signals, decisions, rejected trades, orders, fills, positions, exits, and portfolio results.
   * Track differences between expected and actual simulated execution.
   * Test market openings, closes, volatility spikes, data interruptions, and recovery.
   * Do not fabricate results or treat backtests as live paper-trading evidence.
5. Performance governance
   * Establish measurable promotion and rejection criteria before evaluating results.
   * Require minimum sample sizes and sufficient observation across different regimes.
   * Evaluate strategy degradation and champion/challenger stability.
   * Require explicit human approval for any model or strategy promotion.
   * Do not automatically relax risk limits or promote underperforming strategies.
6. Readiness reporting
   * Create a clear scorecard separating backtest, out-of-sample, and paper-trading evidence.
   * Identify statistically weak or unproven strategies.
   * Document sample sizes, confidence limitations, and data gaps.
   * Define objective criteria for continued paper trading versus rejection.

Required tests

* Full backend and frontend regression suites.
* Reproducibility of historical results.
* Look-ahead and leakage tests.
* Out-of-sample isolation tests.
* Transaction-cost and slippage sensitivity tests.
* Long/short and portfolio-level risk tests.
* Market-regime and drawdown stress tests.
* Paper-trading lifecycle and reconciliation tests.
* Verify that insufficient evidence cannot trigger promotion.

Strict restrictions

* Paper trading only.
* No live brokerage orders or real-money trading.
* No automatic strategy or model promotion.
* No weakening risk controls to improve reported performance.
* No fabricated historical or paper-trading data.
* Do not claim profitability without sufficient evidence.
* Do not declare readiness based solely on passing software tests.
* Do not begin cloud deployment in this phase.

Completion report
Report:

1. Historical data coverage and limitations.
2. Backtest and out-of-sample results by strategy.
3. Paper-trading observation duration and sample size.
4. Risk, drawdown, and execution-quality results.
5. Statistical confidence and overfitting findings.
6. Strategy promotion/rejection decisions and justification.
7. Full test results.
8. Outstanding risks.
9. Whether Atlas has enough evidence to proceed to Phase 11B.

Work only within Phase 11A. Do not begin Phase 11B automatically.
