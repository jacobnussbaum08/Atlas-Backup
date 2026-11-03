ATLAS AI TRADER — PHASE 4B
IMPLEMENTATION OF SIX TRADING STRATEGIES
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the existing Strategy Laboratory.
First verify Phase 4A, Phases 1–3, database migrations, and existing tests. Resolve critical P0/P1 issues before proceeding. Preserve working code.
OBJECTIVE
Implement six independent, configurable trading strategies using actual validated Atlas market data and features.
Every strategy must generate standardized, timestamped trade proposals through the Phase 4A interface.
Initially support US stocks and ETFs. Support LONG and SHORT proposals where instrument eligibility permits.
1. MOMENTUM
Evaluate:

* Rate of change across multiple periods.
* Relative strength.
* EMA confirmation.
* Relative volume.
* Momentum acceleration.

Identify sustained upward and downward price acceleration.
2. TREND FOLLOWING
Evaluate:

* EMA alignment.
* SMA relationships.
* ADX and directional movement.
* Trend slope.
* Pullbacks within established trends.
* Trend continuation.

Avoid treating weak or sideways conditions as strong trends.
3. MEAN REVERSION
Evaluate:

* RSI extremes.
* Bollinger Bands.
* Statistical distance from moving averages.
* Historical price deviations.
* Reversal confirmation.

Use market-regime information to avoid blindly trading against strong trends.
An extreme RSI reading alone must not generate a proposal.
4. BREAKOUT
Evaluate:

* Rolling support and resistance.
* Previous highs and lows.
* Consolidation ranges.
* Volatility compression.
* Relative volume expansion.
* Breakout confirmation.

Exclude the current incomplete candle from historical breakout-level calculations.
Consider failed breakouts and false signals.
5. VOLATILITY
Evaluate:

* ATR expansion and contraction.
* Realized volatility.
* Volatility percentiles.
* Bollinger Band width.
* Range expansion.
* Directional price confirmation.

Look for opportunities arising from changes in volatility regimes.
Do not interpret high volatility alone as a directional signal.
6. EVENT DRIVEN
Consume available Phase 3C news and event records.
Evaluate:

* Earnings announcements.
* Corporate announcements.
* Analyst changes.
* M&A developments.
* Regulatory events.
* Relevant macroeconomic events.
* Event-related price and volume reactions.

Require actual validated event data and price confirmation.
Respect publication and `available_at` timestamps.
Do not fabricate event information or trade solely because an article has positive or negative language.
If Phase 3C functionality is incomplete, clearly identify the missing dependency rather than creating fake news-driven signals.
7. STRATEGY REQUIREMENTS
For every strategy:

* Use the Phase 4A standardized interface.
* Use existing feature calculations.
* Avoid duplicated indicator logic.
* Support configurable parameters.
* Support relevant timeframes.
* Support long and short directions.
* Use completed candles where required.
* Check asset eligibility.
* Check feature quality and freshness.
* Include entry reference, invalidation level, and target where meaningful.
* Include signal expiration.
* Record supporting evidence.
* Record strategy and feature versions.

All thresholds must be configurable and versioned.
Use sensible initial defaults, but do not describe them as optimized or profitable.
Each strategy must independently support enable/disable functionality.
A strategy failure must not interrupt other strategies.
8. SIGNAL QUALITY AND CONFLICTS
Distinguish:

* No signal.
* Valid proposal.
* Insufficient data.
* Invalid data.
* Expired proposal.

Prevent duplicate proposals for the same strategy, asset, timeframe, direction, and evaluation event.
Different strategies may legitimately propose opposing trades.
Record those conflicts without inventing a final portfolio decision. The AI decision coordinator will handle proposal competition in Phase 5.
Signal strength is not a calibrated probability unless statistically validated.
9. ENTRY, INVALIDATION AND TARGETS
Define documented, strategy-specific methods for:

* Reference entry price.
* Invalidation level.
* Potential target.
* Expected holding horizon.

Use ATR or other appropriate volatility measurements where useful.
Reject invalid price relationships.
Do not calculate authoritative position sizes or override future risk controls.
Proposals are analytical hypotheses, not approved orders.
10. DATABASE, API AND DASHBOARD
Reuse Phase 4A models and interfaces.
Persist:

* Strategy evaluation.
* Strategy version.
* Configuration version.
* Feature snapshot.
* Generated proposals.
* Rejection/no-signal reason.
* Evaluation timestamp.

Add or extend API endpoints to inspect strategy configurations, evaluation history, and proposals.
Extend the Strategy Laboratory dashboard with:

* Six strategy modules.
* Strategy status.
* Configuration.
* Recent proposals.
* Direction.
* Entry, invalidation, and target references.
* Supporting evidence.
* Evaluation history.

Do not fabricate performance statistics.
11. TESTING
Test every strategy independently using deterministic historical fixtures.
Verify:

* Long and short logic.
* Indicator calculations.
* Configurable thresholds.
* Incomplete candles.
* Missing and stale data.
* Invalid prices.
* Expired proposals.
* Duplicate prevention.
* Conflicting proposals.
* Historical timestamp correctness.
* No look-ahead bias.
* Asset eligibility.
* API and dashboard functionality.

Preserve all previous tests.
Use real provider data in opt-in smoke tests where appropriate, but keep ordinary tests independent of external APIs.
12. STRICT RESTRICTIONS
Do not implement:

* AI trade coordination.
* Predictive ML models.
* Portfolio management.
* Position sizing.
* Risk authorization.
* Order execution.
* Brokerage integration.
* Automatic live trading.

Do not claim strategies are profitable. Formal backtesting and comparative validation belong to Phase 8.
13. COMPLETION REPORT
Report:

1. Each strategy's implemented methodology.
2. Default parameters.
3. Supported assets and timeframes.
4. Signal-generation examples.
5. Database and API changes.
6. Dashboard additions.
7. Files created and modified.
8. Test results for each strategy.
9. Known limitations.
10. Readiness for Phase 5.

Implement incrementally and preserve all existing functionality.
FINAL INSTRUCTION: Implement Phase 4B only. Build six independent, explainable, configurable strategy modules that generate reproducible trading proposals without executing trades.
