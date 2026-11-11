ATLAS AI TRADER — PHASE 8A
EVENT-DRIVEN HISTORICAL BACKTESTING ENGINE
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase.
First verify Phases 1–7B, especially:

* Six strategy implementations.
* Market intelligence and news features.
* ML inference interfaces.
* Double-entry accounting.
* Risk controls and reservations.
* Paper execution and fill simulation.

Review existing tests and audit findings. Stop if unresolved P0/P1 issues, accounting discrepancies, or execution-integrity failures exist.
Preserve all working functionality.
OBJECTIVE
Build a reproducible, event-driven historical backtesting engine that simulates how Atlas's existing strategies would have behaved using historical market data.
Reuse existing strategy, portfolio, accounting, risk, and paper-execution interfaces wherever possible. Do not build a disconnected second trading system.
Backtesting must be isolated from live market streaming and brokerage execution.
1. HISTORICAL REPLAY
Implement chronological replay of historical market observations.
Support:

* Multiple assets.
* Multiple timeframes.
* Multiple strategy modules.
* Long and short positions.
* Concurrent positions.
* Historical market sessions.
* Configurable date ranges.
* Configurable starting capital.
* Historical bars and quotes where available.

Process events in chronological order.
At each simulated decision time, only expose information that would actually have been available at that time.
Support repeatable runs with identical inputs and configurations.
2. LOOK-AHEAD AND DATA LEAKAGE PREVENTION
Treat this as a critical financial-correctness requirement.
Enforce:

* Point-in-time market data.
* Feature `available_at` timestamps.
* Completed-candle requirements.
* Historical asset-universe membership.
* Historical news availability.
* Strategy and model version snapshots.
* Correct event ordering.
* No future prices in current decisions.
* No future corporate-action information.

Never assume a historical REST retrieval timestamp represents the original historical availability time.
If original availability is unknown, apply an explicitly documented conservative policy or exclude the observation from tests requiring precise availability.
Test that future observations cannot enter historical decisions.
3. STRATEGY INTEGRATION
Run all six existing strategies through their existing interfaces:

* Momentum.
* Trend following.
* Mean reversion.
* Breakout.
* Volatility.
* Event driven.

Use historical strategy configurations and immutable versions.
Allow strategies to be tested:

* Individually.
* In combinations.
* Across assets.
* Across timeframes.
* Long versus short.

Record every proposal, rejection, expiration, and resulting trade.
Do not rewrite strategy logic simply to accommodate backtesting.
4. PORTFOLIO AND RISK SIMULATION
Integrate the existing portfolio and risk systems.
Simulate:

* Starting capital.
* Cash balances.
* Open positions.
* Position sizing.
* Long and short exposure.
* Risk limits.
* Reservations.
* Fees.
* Realized and unrealized P/L.
* Equity changes.
* Drawdowns.

Use the same deterministic risk rules as paper trading.
Avoid differences between historical simulation and the live-data paper architecture.
Record the precise risk configuration used for each run.
5. EXECUTION SIMULATION
Reuse Phase 7B's isolated paper execution engine where possible.
Support:

* Market orders.
* Limit orders.
* Stop orders.
* Stop-limit orders.
* Take-profit.
* Trailing stops.
* Partial fills.
* Bid/ask spreads.
* Slippage.
* Commissions.
* Liquidity constraints.
* Market gaps.

Do not assume historical candle prices guarantee fills.
If both stop and target are touched within the same candle and finer-grained data cannot establish the order of events, use the conservative stop-first assumption.
Do not invent intrabar price paths.
Clearly document limitations caused by unavailable historical quotes and trades, particularly with IEX data.
6. BACKTEST CONFIGURATION
Allow configurable:

* Date range.
* Initial capital.
* Assets or point-in-time universe.
* Strategies.
* Strategy versions.
* Timeframes.
* Parameters.
* Risk configuration.
* Execution assumptions.
* Transaction costs.
* Benchmark.
* Historical data source.

Every configuration must be stored with the resulting backtest.
Use a fixed seed wherever randomness is involved.
7. PERFORMANCE METRICS
Calculate:

* Total return.
* Net profit/loss.
* Annualized return where appropriate.
* Maximum drawdown.
* Sharpe ratio.
* Sortino ratio.
* Win rate.
* Average winner.
* Average loser.
* Profit factor.
* Expectancy.
* Average holding period.
* Turnover.
* Fees and transaction costs.
* Long versus short performance.
* Strategy-specific results.
* Asset-specific results.

Handle insufficient sample sizes honestly.
Do not present annualized or risk-adjusted metrics where the data length does not support them.
Distinguish gross returns from net returns after costs.
8. BENCHMARKS
Support appropriate benchmarks.
Initially support SPY for relevant US equity strategies.
Make benchmark selection configurable.
Compare:

* Atlas portfolio performance.
* Benchmark performance.
* Buy-and-hold reference.
* Individual strategy results.

Ensure benchmark comparisons use equivalent date ranges and appropriate return calculations.
9. RESULT STORAGE AND REPRODUCIBILITY
Create or extend database models for:

* Backtest runs.
* Backtest configurations.
* Historical event cursors.
* Simulated orders.
* Simulated fills.
* Simulated trades.
* Equity snapshots.
* Performance metrics.
* Benchmark results.
* Rejection records.
* Run errors.

Store:

* Code version.
* Strategy versions.
* Model versions.
* Dataset references.
* Data sources.
* Universe definition.
* Risk configuration.
* Execution model version.
* Parameters.
* Start and end timestamps.
* Random seeds.

Historical records must remain immutable.
A completed run must be reproducible from its stored inputs and configuration.
10. API AND DASHBOARD
Add backtesting functionality to the existing dashboard.
Allow users to:

* Configure a backtest.
* Select strategies.
* Select assets.
* Select timeframes.
* Select date ranges.
* Submit runs.
* Monitor progress.
* Inspect completed results.
* Compare strategies.

Display:

* Equity curve.
* Drawdown chart.
* Trade history.
* Entry and exit details.
* Performance metrics.
* Benchmark comparison.
* Costs.
* Strategy breakdown.

Use actual calculated results.
Never fabricate or hardcode profitable example results.
11. TESTING
Test:

* Chronological event processing.
* Multiple simultaneous assets.
* Multiple timeframes.
* Strategy integration.
* Position sizing.
* Risk limits.
* Execution costs.
* Partial fills.
* Stop and target behavior.
* Accounting reconciliation.
* Look-ahead prevention.
* Survivorship-bias prevention.
* Historical news availability.
* Reproducibility.
* Benchmark calculations.
* Performance metrics.

Use deterministic fixtures for ordinary tests.
Preserve all existing tests.
Run financial and concurrency-critical tests against PostgreSQL.
12. SCOPE RESTRICTIONS
Implement Phase 8A only.
Do not implement:

* Automated model promotion.
* Advanced walk-forward optimization.
* CPCV experimentation framework.
* Hyperparameter optimization at scale.
* Autonomous strategy modification.
* Live brokerage execution.

These belong to Phase 8B or remain outside project scope.
13. ACCEPTANCE CRITERIA
Phase 8A is complete when:

1. Historical events replay chronologically.
2. All six strategies can be evaluated.
3. Existing risk rules are enforced.
4. Simulated execution includes realistic documented costs.
5. Portfolio accounting reconciles throughout the run.
6. Look-ahead safeguards pass tests.
7. Historical universe membership is respected.
8. Results are reproducible.
9. Benchmark comparisons work.
10. Performance metrics are correct.
11. API and dashboard backtesting functionality works.
12. Existing regression tests pass.
13. New financial correctness and historical integrity tests pass.

14. COMPLETION REPORT
Report:

* Backtesting architecture.
* Replay methodology.
* Strategy integration.
* Risk integration.
* Execution assumptions.
* Cost model.
* Look-ahead safeguards.
* Database changes.
* API and dashboard changes.
* Files created and modified.
* Test results.
* Actual backtest examples, clearly identifying the dataset and configurations.
* Known limitations.
* Readiness for Phase 8B.

Do not claim a strategy is profitable based on an in-sample backtest.
Do not present historical results as guarantees of future performance.
Implement incrementally and preserve all existing functionality.
FINAL INSTRUCTION: Implement Phase 8A only. Prioritize historical correctness, realistic execution, financial reconciliation, and complete reproducibility.
