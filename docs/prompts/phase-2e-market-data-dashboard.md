# Phase 2E prompt (as given)

ATLAS AI TRADER — PHASE 2E
MARKET DATA DASHBOARD, FRONTEND AND API INTEGRATION
You are the senior frontend engineer and full-stack software architect responsible for completing Deliverable 2E of Atlas AI Trader.
Read `ATLAS_MASTER_SPEC_V2.md` in its entirety.
Inspect the existing Phase 1 and Phase 2 implementation before changing anything.
This deliverable completes the initial Market Data Engine interface by connecting the existing React frontend to the real backend market-data infrastructure.
The objective is to turn the existing frontend shell into a professional, functional financial-market monitoring dashboard.
This is not a trading execution interface.
The system remains strictly paper-trading-only, and no order execution functionality should be implemented.
PART 1 — VALIDATE PREVIOUS DELIVERABLES
Before implementation, inspect:

* Phase 1 foundation.
* Deliverable 2A provider architecture.
* Deliverable 2B historical data.
* Deliverable 2C live WebSocket streaming.
* Deliverable 2D reliability and recovery.

Verify:

* Existing backend tests.
* Existing frontend tests.
* Database migrations.
* Historical candle retrieval.
* Live streaming.
* Data-quality handling.
* Backfill and recovery.
* Provider health monitoring.

Check Git status and confirm the appropriate Phase 2B–2D commits exist.
Do not overwrite existing work.
Do not recreate working backend functionality.
If critical backend functionality is unavailable, document the issue before proceeding.
PART 2 — DESIGN DIRECTION
Build Atlas as a professional quantitative trading terminal.
The visual direction should be:

* Dark interface.
* Clean typography.
* Professional financial software aesthetic.
* Dense but readable market information.
* Consistent spacing.
* Subtle borders.
* Clear visual hierarchy.
* High-quality charts.
* Minimal unnecessary decoration.

Avoid:

* Generic SaaS dashboard styling.
* Oversized cards.
* Excessively rounded components.
* Bright, distracting gradients.
* Unnecessary animations.
* Decorative charts with fabricated data.

The application should feel like a serious market-monitoring and quantitative research platform.
Use a consistent design system for:

* Colors.
* Typography.
* Spacing.
* Borders.
* Status indicators.
* Buttons.
* Tables.
* Charts.
* Loading states.
* Empty states.
* Error states.

Keep the interface responsive, prioritizing desktop use.
Use the existing React and TypeScript architecture.
Do not replace the frontend framework.
PART 3 — APPLICATION LAYOUT
Create a persistent application layout.
Sidebar navigation
Include:

* Overview
* Markets
* Data Health
* Ingestion
* Settings

Future sections may be shown as disabled or clearly marked as coming later:

* Strategies
* AI Intelligence
* Portfolio
* Risk Center
* Backtesting

Do not create nonfunctional pages that falsely imply these systems are operational.
Top navigation
Display:

* Atlas AI Trader branding.
* Current system status.
* Active market-data provider.
* Active market-data feed.
* Local time and date.
* Connection status.

Display an unmistakable label that Atlas is in market-monitoring / paper-only development mode.
Do not imply real-money trading is available.
PART 4 — OVERVIEW PAGE
Create a functional market overview page.
Show actual information from the backend.
Market status
Display:

* Current market status.
* Whether the NYSE session is open or closed.
* Next relevant market session where available.
* Market-data provider status.
* WebSocket connection state.
* Authentication state.
* Subscription state.
* Database/data pipeline health.

Use the existing market calendar and status logic where available.
Do not independently invent market-session calculations in frontend code.
Market-data summary
Display:

* Monitored assets.
* Active subscriptions.
* Latest market-data event.
* Last successful database persistence.
* Outstanding candle gaps.
* Pending backfill jobs.
* Failed ingestion jobs.

Initial asset cards or table
Display:

* AAPL.
* SPY.

Allow the asset list to expand as more instruments are configured.
For each asset display:

* Symbol.
* Asset name where available.
* Last price.
* Previous close where reliably available.
* Price change.
* Percentage change.
* Latest event timestamp.
* Provider.
* Feed.
* Data-quality status.

Do not fabricate previous-close values.
If the necessary comparison data is unavailable, display an appropriate placeholder.
Clearly distinguish:

* Real-time provider data.
* Delayed data.
* Historical data.
* Missing information.

Every price must have its source and feed available in the interface.
PART 5 — MARKETS PAGE
Build a market-monitoring page with a functional asset table.
Columns should include:

* Symbol.
* Name.
* Asset class.
* Exchange.
* Latest price.
* Price change.
* Percentage change.
* Latest event.
* Feed.
* Data freshness.
* Quality status.

Support:

* Search by symbol.
* Sortable columns.
* Configurable visible columns where practical.
* Asset selection.
* Refresh.
* Loading states.
* Error states.
* Empty states.

Use the existing asset and provider mapping systems.
Do not hardcode market prices or treat the initial two-asset test universe as the permanent universe.
Use the backend as the authoritative source of asset information.
PART 6 — INDIVIDUAL ASSET DETAIL
Create a dedicated asset detail view.
For example:
`/markets/AAPL`
and:
`/markets/SPY`
Display:
Asset header

* Symbol.
* Name.
* Exchange.
* Currency.
* Latest available price.
* Percentage change where supported.
* Provider and feed.
* Data freshness.

Interactive price chart
Use TradingView Lightweight Charts if compatible with the existing project.
Support:

* Candlestick chart.
* Volume display.
* Historical time range.
* Timeframe selection.
* Live updates where available.
* Loading indicators.
* Empty historical periods.
* Missing-data markers or warnings.

Initial timeframes:

* 1 minute.
* 5 minutes.
* 15 minutes.
* 1 hour.
* 1 day.

Historical chart data must come from the actual backend.
Do not generate placeholder candles.
Ensure candles are ordered correctly and use the correct timestamps.
Handle:

* Incomplete candles.
* Candle revisions.
* Missing intervals.
* Historical/live overlaps.
* Market closures.
* Timezone display.

All internal timestamps remain UTC.
Display timestamps in a clearly identified timezone, with a consistent user-facing default.
Do not allow a corrected provider candle to retroactively modify an earlier historical decision record. The chart may display the latest normalized candle while preserving source revision information.
Chart updating
Live updates should use the existing backend streaming architecture.
Do not create a new Alpaca WebSocket connection from each browser session.
The backend should distribute updates to connected frontend clients through an API-level mechanism.
Evaluate the existing architecture and use:

* WebSockets,
* Server-Sent Events,
* Or a carefully controlled polling mechanism,

depending on what is already implemented.
If Redis Pub/Sub is used for transient frontend notifications, PostgreSQL remains the authoritative source of persisted market data.
The frontend must not use Redis directly.
PART 7 — QUOTES AND TRADES
If actual quote and trade records are available from the backend, expose them in the asset detail page.
Provide sections for:

* Latest bid.
* Latest ask.
* Bid size.
* Ask size.
* Latest trade price.
* Latest trade quantity.
* Event timestamps.

Do not assume that historical quote recovery is enabled.
If quote or trade history is unavailable:

* Show the limitation clearly.
* Distinguish unavailable historical information from a live connection failure.
* Do not fill the gap with fabricated values.

Preserve feed identity.
PART 8 — DATA HEALTH PAGE
Build a dedicated operational data-health page.
Display separate health dimensions:

1. Provider connectivity.
2. Authentication.
3. Subscription.
4. Message reception.
5. Processing.
6. Database persistence.
7. Historical completeness.
8. Backfill activity.

For each dimension show:

* Status.
* Last successful event.
* Last update.
* Relevant errors.
* Warnings.
* Recovery information.

Include:

* Current outstanding candle gaps.
* Recovered intervals.
* Unavailable intervals.
* Invalid observations.
* Quarantined records.
* Duplicate suppression events.
* Provider interruptions.
* Database interruptions.

Allow users to inspect relevant historical events.
A connected WebSocket must not automatically mean the entire market-data pipeline is healthy.
Use actual backend health information.
PART 9 — INGESTION PAGE
Create a historical-data ingestion and recovery monitoring page.
Display:

* Historical retrieval jobs.
* Backfill jobs.
* Job ID.
* Assets.
* Timeframe.
* Requested date range.
* Feed.
* Status.
* Pages retrieved.
* Records retrieved.
* Records stored.
* Rejected records.
* Duplicate records.
* Outstanding gaps.
* Start and completion timestamps.

Support filtering by:

* Status.
* Asset.
* Timeframe.
* Date.

If historical data retrieval is already exposed through a backend API, provide an interface to submit valid requests.
Allow users to choose:

* Asset.
* Timeframe.
* Start date.
* End date.

Use the actual backend schema.
Validate that the selected date range is valid.
Do not create a second ingestion engine inside the frontend.
Do not allow the frontend to bypass backend validation or provider restrictions.
Provide clear feedback when an ingestion request is accepted, running, partially completed, or failed.
PART 10 — SETTINGS PAGE
Build the initial market-data settings interface.
Display:

* Configured provider.
* Feed.
* Enabled streaming status.
* Configured symbols.
* Supported timeframes.
* Provider capability limitations.
* Data freshness configuration where exposed.
* System environment status where safe.

Do not display API keys or secret values.
Do not expose private environment variables.
Do not provide a frontend field that reveals or prints API credentials.
Do not silently switch data providers or feeds.
Sensitive configuration changes must follow existing backend authorization and audit controls.
PART 11 — BACKEND API INTEGRATION
Inspect all existing market-data API endpoints.
Reuse existing endpoints rather than duplicating them.
Identify any missing API functionality required for the dashboard.
Implement additional endpoints only when necessary.
Potential endpoints include:
GET `/api/v1/market-data/status`
GET `/api/v1/market-data/providers`
GET `/api/v1/market-data/assets`
GET `/api/v1/market-data/candles`
GET `/api/v1/market-data/quotes`
GET `/api/v1/market-data/trades`
GET `/api/v1/market-data/quality`
GET `/api/v1/market-data/ingestion-jobs`
GET `/api/v1/market-data/ingestion-jobs/{job_id}`
GET `/api/v1/market-data/recovery-jobs`
GET `/api/v1/market-data/health`
Use the established API conventions and schemas.
Support pagination for large result sets.
Use consistent error response formats.
Do not expose provider credentials.
Do not expose unrestricted database access.
Do not create trading or brokerage endpoints.
PART 12 — FRONTEND DATA ARCHITECTURE
Organize frontend code into reusable components and services.
Separate:

* API clients.
* Data types.
* Hooks.
* Chart components.
* Market tables.
* Status indicators.
* Layout.
* Pages.

Use TypeScript types aligned with the backend response schemas.
Implement appropriate request handling:

* Loading.
* Success.
* Empty.
* Error.
* Retry.

Prevent stale frontend requests from overwriting more recent results.
Avoid excessive API polling.
Use a reasonable refresh strategy based on each data type.
For streaming data:

* Update only the relevant UI elements.
* Avoid unnecessary full-page rerenders.
* Clean up WebSocket or SSE subscriptions when components unmount.
* Handle reconnection.
* Avoid duplicate frontend subscriptions.

Do not store API secrets in frontend environment variables.
Do not fetch Alpaca data directly from the browser.
PART 13 — CHART CORRECTNESS
This requires particular attention.
A financial chart must not merely look correct.
It must display correct data.
Validate:

* Timestamp ordering.
* Candle uniqueness.
* Correct OHLC values.
* Timeframe alignment.
* Missing intervals.
* Volume availability.
* Revision handling.
* Historical/live boundaries.

Use actual persisted candle data for historical chart loading.
For live updates, update the current candle according to the documented Alpaca bar semantics.
Do not append duplicate candles for repeated updates to the same candle period.
Where an update revises an existing candle, update the current chart representation appropriately without overwriting historical decision snapshots.
Handle extended-hours data consistently with the feed and configured session display.
Clearly label IEX as the selected feed.
PART 14 — TESTING
Use the existing frontend and backend test architecture.
Frontend tests
Test:

* Sidebar navigation.
* Route loading.
* API response rendering.
* Market-data table.
* Asset selection.
* Chart initialization.
* Timeframe selection.
* Loading states.
* Empty states.
* Error states.
* Provider-status indicators.
* Data-quality warnings.
* Ingestion-job display.
* Live-update handling.
* Component cleanup.

Backend tests
Test any newly added API endpoints:

* Successful requests.
* Invalid parameters.
* Missing assets.
* Empty results.
* Pagination.
* Authentication and authorization where relevant.
* Provider failures.
* Database failures.
* Response consistency.

Integration tests
Verify:
PostgreSQL market data
-> FastAPI endpoints
-> React API client
-> Dashboard display
Also verify:
Live market event
-> Existing background worker
-> Backend update distribution
-> Connected frontend view
Use actual provider data in opt-in smoke tests where appropriate.
Do not treat test fixtures as real market data.
All previous Phase 1 and Phase 2A–2D tests must remain passing.
PART 15 — RESPONSIVENESS AND PERFORMANCE
Prioritize desktop layout but ensure the dashboard remains usable on smaller screens.
Implement:

* Efficient chart updates.
* Table pagination or virtualization when appropriate.
* Efficient API queries.
* Controlled data refresh.
* Component reuse.
* Cleanup of subscriptions.
* Proper error handling.

Do not download an unlimited historical dataset when a chart only needs a selected range.
Do not request the entire trade history every time a page refreshes.
Benchmark before adding unnecessary performance infrastructure.
PART 16 — STRICT SCOPE RESTRICTIONS
Implement Deliverable 2E only.
Do not implement:

* Strategy generation.
* Market-regime classification.
* Technical indicators beyond those already available.
* Machine learning.
* LLM analysis.
* Portfolio accounting UI beyond existing data.
* Risk management engine.
* Buy/sell controls.
* Order submission.
* Brokerage integration.
* Real-money trading.

This deliverable is about visualizing and monitoring the existing Market Data Engine.
Do not make the dashboard imply that future systems are operational.
PART 17 — ACCEPTANCE CRITERIA
Deliverable 2E is complete only when:

1. The dashboard loads successfully.
2. Sidebar navigation works.
3. Provider status displays actual backend information.
4. AAPL and SPY appear using actual asset records.
5. Historical candles load from PostgreSQL through FastAPI.
6. Candlestick charts display real historical data.
7. Timeframe selection works.
8. Live chart updates work where streaming data is available.
9. The backend distributes market updates without creating duplicate provider connections.
10. Feed information is clearly displayed.
11. Data-quality warnings appear correctly.
12. Provider interruptions are visible.
13. Ingestion jobs are viewable.
14. Recovery jobs and missing intervals are viewable.
15. Unavailable quote-history recovery is not misrepresented as complete.
16. API keys never reach frontend code.
17. Loading, empty, and failure states work.
18. The dashboard does not display fabricated prices or candles.
19. Existing regression tests pass.
20. New frontend and backend tests pass.
21. The system remains paper-trading-only.

PART 18 — COMPLETION REPORT
At completion, provide:

1. Summary of the dashboard implementation.
2. Screenshots or a description of each completed page.
3. Final frontend architecture.
4. Backend API changes.
5. Files created.
6. Files modified.
7. Components created.
8. Chart integration details.
9. Real-time update architecture.
10. Data-health functionality.
11. Ingestion functionality.
12. Frontend test results.
13. Backend test results.
14. Regression test results.
15. Actual provider smoke-test results where performed.
16. Known limitations.
17. How to launch Atlas locally.
18. The exact URL for the dashboard.
19. The exact URL for API documentation.
20. Confirmation that no live execution functionality was introduced.

Do not claim that a page or chart is functional unless it is connected to actual backend data.
Do not claim real-time functionality unless live updates have actually been verified.
Do not claim Phase 2 is complete while critical acceptance criteria remain unmet.
FINAL INSTRUCTION: Implement Deliverable 2E only. Build a polished, professional, data-accurate market-monitoring interface using the existing Atlas architecture.
