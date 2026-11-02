# Phase 3B prompt (as given)

ATLAS AI TRADER — PHASE 3B
MARKET INTELLIGENCE ENGINE
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the existing codebase.
First verify Phase 3A and review the latest code audit. Stop if unresolved critical P0/P1 issues or broken dependencies exist.
Preserve all existing functionality and tests.
OBJECTIVE
Build Atlas's Market Intelligence Engine. It must analyze market conditions, trends, volatility, market structure, and cross-asset relationships using actual validated market data.
Initially support the US stocks and ETFs available through the existing Alpaca integration.
Do not fabricate data or pretend unsupported markets are operational.
1. TREND ANALYSIS
Build reusable, timestamp-aware trend measurements using:

* SMA and EMA relationships.
* Moving-average slopes.
* ADX and directional movement.
* Price structure.
* Higher highs and lower lows.
* Momentum and rate of change.
* Relative strength against appropriate benchmarks.

Classify trend conditions probabilistically:

* Bullish.
* Bearish.
* Sideways.
* Transitioning.
* Uncertain.

Support multiple timeframes rather than assuming one timeframe describes the entire market.
2. VOLATILITY ANALYSIS
Implement:

* ATR.
* Realized volatility.
* Rolling volatility.
* Volatility percentiles.
* Bollinger Band width.
* Range expansion and contraction.
* Volatility regime changes.

Classify volatility independently of trend:

* Low.
* Normal.
* Elevated.
* Extreme.

Use historical, point-in-time observations. Do not interpret every large price movement as a data error.
3. MARKET REGIME DETECTION
Create a configurable market-regime engine combining relevant measurements.
Evaluate:

* Trend direction.
* Trend strength.
* Volatility.
* Momentum.
* Market participation.
* Breadth where reliable data exists.

Produce separate outputs for trend, volatility, and market stability rather than forcing everything into one mutually exclusive label.
Use probabilistic classifications with:

* Regime probabilities.
* Classification timestamp.
* Supporting measurements.
* Model or algorithm version.
* Data-quality status.

Include an uncertain classification when evidence is insufficient.
Avoid arbitrary thresholds presented as scientifically optimal. Make thresholds configurable and document their initial rationale.
4. MARKET-WIDE INTELLIGENCE
Use SPY as an initial broad US equity-market reference where appropriate.
Analyze:

* Broad market trend.
* Market volatility.
* Participation among qualified assets.
* Advancing versus declining assets where reliable data supports it.
* Relative strength across monitored instruments.
* Concentration of market movement.
* Changes in market participation.

Do not claim comprehensive market breadth from an incomplete asset universe.
Clearly identify limitations caused by the IEX data feed and available universe.
5. CROSS-ASSET INTELLIGENCE
Calculate:

* Rolling correlation.
* Correlation changes.
* Relative performance.
* Benchmark-relative strength.
* Relationships between qualified assets.
* Concentration and common market exposure indicators.

Handle missing observations and insufficient overlapping history correctly.
Use configurable lookback windows.
Avoid treating correlation as causation or assuming historical relationships remain stable.
Apply minimum observation requirements and appropriate statistical safeguards.
Do not produce misleading correlations from insufficient data.
6. FEATURE AND REGIME STORAGE
Create or extend versioned database models for:

* Market features.
* Trend measurements.
* Volatility measurements.
* Regime classifications.
* Cross-asset relationships.
* Market-wide summaries.

Record:

* Asset or market identifier.
* Timeframe.
* Measurement window.
* Feature values.
* Probability estimates where applicable.
* Data timestamps.
* Feature availability timestamp.
* Algorithm version.
* Data provenance.
* Quality status.

Preserve historical results. Do not silently overwrite previous market-intelligence observations.
7. HISTORICAL CORRECTNESS
Prevent look-ahead bias.
Every calculation must use only observations available at its decision timestamp.
Respect:

* Candle completion.
* `available_at` timestamps.
* Missing data.
* Market sessions.
* Historical asset-universe membership.
* Corrected candle revisions.

A later candle correction must not retroactively change previously recorded intelligence outputs.
Support reproducible historical calculations.
8. API AND DASHBOARD
Expose market intelligence through FastAPI.
Add useful endpoints for:

* Market regime.
* Trend measurements.
* Volatility measurements.
* Market-wide conditions.
* Cross-asset correlations.
* Historical regime observations.

Extend the existing Atlas dashboard with a Market Intelligence page.
Display:

* Current broad-market conditions.
* Trend classifications.
* Volatility classifications.
* Regime probabilities.
* Market participation where available.
* Correlation matrix or heatmap.
* Relative-strength rankings.
* Historical regime changes.

Use actual backend data. Show uncertainty and data limitations clearly.
Do not fabricate chart values or labels.
9. TESTING
Test:

* Indicator calculations against known test vectors.
* Trend classification.
* Volatility classification.
* Regime probabilities.
* Insufficient-data behavior.
* Missing observations.
* Correlation calculations.
* Historical timestamp correctness.
* Look-ahead prevention.
* Asset-universe point-in-time correctness.
* API responses.
* Dashboard rendering.
* Database persistence.
* Concurrency where relevant.

Preserve all existing tests.
Distinguish actual data tests from fixture-based tests.
10. ENGINEERING REQUIREMENTS

* Reuse existing feature and market-data infrastructure.
* Use Polars where beneficial.
* Use NumPy where appropriate.
* Use Decimal for persisted financial quantities.
* Use UTC timestamps.
* Keep calculations modular and independently testable.
* Version algorithms and configurations.
* Avoid excessive indicator duplication.
* Keep PostgreSQL authoritative.
* Do not introduce unnecessary infrastructure or subscriptions.

11. SCOPE RESTRICTIONS
Implement Phase 3B only.
Do not build:

* Trading strategies.
* Buy/sell signals.
* AI trade decisions.
* LLM news analysis.
* ML-based return prediction.
* Portfolio management.
* Position sizing.
* Risk approval.
* Order execution.
* Brokerage integrations.

Market intelligence describes market conditions. It does not authorize trades.
12. COMPLETION REPORT
Report:

1. Architecture and algorithms implemented.
2. Files created and modified.
3. Database migrations.
4. Features and classifications produced.
5. Default parameters and configurable thresholds.
6. API endpoints.
7. Dashboard additions.
8. Test results.
9. Historical correctness tests.
10. Actual provider-data verification.
11. Known limitations.
12. Readiness for Phase 4.

Implement incrementally and test after each major component.
Do not rewrite working systems unnecessarily.
Do not claim a market condition is predictive or profitable without validation.
FINAL INSTRUCTION: Implement Phase 3B only. Build a statistically grounded, timestamp-correct, explainable Market Intelligence Engine that future Atlas strategies can consume.
