# Phase 2 — Market Data Engine (instructions as given)

> Recorded verbatim from the user's Phase 2 brief. It refines `ATLAS_MASTER_SPEC_V2.md` §7 and
> §27 "PHASE 2 — MARKET DATA". Implementation had not started when this was recorded; see
> [`../phase-reports/phase-2-preflight-report.md`](../phase-reports/phase-2-preflight-report.md).

---

ATLAS AI TRADER — PHASE 2 IMPLEMENTATION
Market Data Engine
You are the senior backend engineer and quantitative data infrastructure engineer responsible for implementing Phase 2 of Atlas AI Trader.
Read `ATLAS_MASTER_SPEC_V2.md` completely before beginning.
Phase 1 — Foundation should already be implemented.
Your responsibility is to build Phase 2 only: the Market Data Engine.
This is a real market-data integration project. Do not fabricate market data, simulate successful provider connections, or substitute mock functionality for actual integrations.
The initial provider will be Alpaca.
The initial supported market will be US equities and ETFs.
The system must remain paper-trading-only. You are implementing market-data ingestion, NOT brokerage execution.
PART 1 — PHASE 1 VALIDATION
Before modifying anything:

1. Inspect the existing repository.
2. Read the current README.
3. Inspect the Python backend architecture.
4. Inspect the database models and migrations.
5. Inspect environment configuration.
6. Inspect the Docker Compose configuration.
7. Inspect the existing API.
8. Inspect frontend structure.
9. Run the existing automated test suite.
10. Verify PostgreSQL and Redis connectivity where applicable.

Determine whether Phase 1 acceptance criteria are satisfied.
If critical Phase 1 infrastructure is missing or broken, report the specific issue before continuing. Do not build Phase 2 on top of an unreliable foundation.
Do not recreate working Phase 1 infrastructure.
Preserve existing conventions unless a documented technical reason justifies changing them.
PART 2 — PROVIDER ARCHITECTURE
Build a reusable market-data provider architecture.
Alpaca must be the first implemented provider, but the overall design must permit additional providers without rewriting the Market Data Engine.
Create a standardized provider interface.
The interface should support applicable operations such as:

* Provider identification
* Provider capability discovery
* Authentication validation
* Historical candle retrieval
* Live market-data streaming
* Connection health checking
* Subscription management
* Graceful connection closure

Use typed interfaces and clearly documented contracts.
Implement an appropriate provider capability model.
Atlas must be able to determine which markets, feeds, instruments, timeframes, and data types a provider actually supports.
Do not assume every provider supports every capability.
Future providers should be able to implement the same internal contract.
Potential future providers include:

* Polygon
* OANDA
* Twelve Data
* Coinbase
* CCXT-supported exchanges

Do not implement these additional providers during Phase 2.
Avoid hardcoding provider-specific logic throughout other parts of the application.
Provider-specific details belong inside their respective adapter modules.
PART 3 — ALPACA INTEGRATION
Implement an actual Alpaca market-data adapter.
Before implementation, consult the current official Alpaca documentation.
Use documented, supported endpoints and protocols.
Verify current:

* Authentication requirements
* Historical market-data endpoints
* WebSocket endpoints
* Subscription formats
* Feed availability
* Pagination behavior
* Rate limits
* Error responses
* Supported data types

Do not assume that API examples found in old tutorials are current.
Official documentation:
https://docs.alpaca.markets/us/docs/about-market-data-api
https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data
https://docs.alpaca.markets/us/docs/streaming-market-data
Initial provider scope
Implement US stocks and ETFs.
Start with a small configurable test universe:

* SPY
* QQQ
* AAPL
* MSFT
* NVDA

Do not hardcode this list into the data engine.
Load it from configuration or the appropriate database records.
The future asset discovery system will manage the wider universe.
Feed handling
Support explicit provider feed configuration.
Do not silently switch between feeds.
Store the selected feed with the corresponding data.
The system must clearly distinguish IEX from SIP.
The free Alpaca Basic plan provides IEX real-time equity data, not complete consolidated market coverage.
If the configured subscription does not support a requested feed, return a meaningful error and record the provider capability limitation.
Never silently downgrade data quality or pretend that IEX represents all US market activity.
The system should be capable of supporting SIP later without major architectural changes.
PART 4 — HISTORICAL MARKET DATA
Implement historical market-data retrieval through Alpaca REST endpoints.
Initially support:

* 1-minute candles
* 5-minute candles
* 15-minute candles
* 1-hour candles
* 1-day candles

Design timeframe handling so additional intervals can be added.
Required functionality:

1. Retrieve historical bars for one or multiple configured symbols.
2. Handle API pagination.
3. Respect provider rate limits.
4. Validate provider responses.
5. Normalize records.
6. Deduplicate observations.
7. Persist records.
8. Report failed or incomplete retrievals.
9. Allow retrieval of missing historical ranges.
10. Support configurable start and end timestamps.

Use UTC internally.
Handle provider timestamps correctly.
Preserve the distinction between:

* Market event time
* Data retrieval time
* Data availability time
* Database persistence time

Do not assume that requesting data for several symbols returns complete results for all symbols.
Handle empty responses and partial responses explicitly.
Do not silently mark an incomplete historical retrieval as successful.
PART 5 — LIVE MARKET DATA STREAMING
Implement Alpaca's WebSocket market-data integration.
The initial implementation should support applicable live streams for:

* Bars
* Trades
* Quotes

Use only channels actually available to the configured subscription.
Implement:

1. Authentication.
2. Connection establishment.
3. Subscription.
4. Message parsing.
5. Normalization.
6. Persistence.
7. Connection health monitoring.
8. Heartbeat monitoring where supported.
9. Automatic reconnection.
10. Subscription restoration after reconnect.
11. Graceful shutdown.

Use asynchronous processing where appropriate.
Do not block the main FastAPI application while receiving market data.
A background worker should own the persistent market-data stream.
Connection reliability
Handle:

* Disconnections
* Network interruptions
* Authentication failures
* Subscription errors
* Malformed messages
* Duplicate events
* Out-of-order events
* Unexpected provider shutdowns
* Rate limits
* Temporary database failures

Use bounded exponential backoff for recoverable connection failures.
Do not retry permanently invalid authentication indefinitely.
Log every significant connection-state change.
Missing-data recovery
A WebSocket outage must not silently create an incomplete market history.
Track:

* Last received event
* Last persisted event
* Last completed candle
* Connection interruption time
* Recovery time
* Missing intervals where detectable

After reconnecting, use historical REST requests to backfill missing intervals where provider data is available.
Ensure that backfilled data and streamed data do not create duplicate records.
Candle corrections
Some providers may send updated bars after their original emission.
Do not silently overwrite raw observations.
Preserve the original event and track revisions or corrections to normalized candle state.
Make it possible to identify which version of a candle was available at a historical decision timestamp.
PART 6 — NORMALIZED DATA MODELS
Create standardized internal data models.
The rest of Atlas must not need to understand Alpaca-specific response formats.
Implement appropriately typed models for:

* MarketAsset
* MarketBar
* MarketQuote
* MarketTrade
* MarketDataEvent
* ProviderStatus
* ProviderCapabilities
* DataQualityEvent
* DataIngestionJob

Use Pydantic models or appropriate typed domain models.
A normalized candle should support:

* Internal asset identifier
* Provider symbol
* Timeframe
* Open
* High
* Low
* Close
* Volume
* VWAP when available
* Trade count when available
* Event timestamp
* Received timestamp
* Available timestamp
* Provider
* Feed
* Data-quality status
* Session information where available
* Revision information

For quotes:

* Asset identifier
* Bid price
* Ask price
* Bid size
* Ask size
* Event timestamp
* Received timestamp
* Provider
* Feed

For trades:

* Asset identifier
* Price
* Quantity
* Exchange information where available
* Conditions where available
* Event timestamp
* Received timestamp
* Provider
* Feed

Do not make unsupported fields mandatory.
Missing volume must remain NULL or an explicitly missing value, not be replaced with zero.
A genuine zero-volume observation must remain distinguishable from missing volume.
Store provider-specific identifiers separately from Atlas's internal identifiers.
Ticker symbols must not be the primary identity of an asset because symbols can change and can be reused.
PART 7 — DATABASE IMPLEMENTATION
Use PostgreSQL as the authoritative source of persisted market-data state.
Use TimescaleDB where it provides meaningful benefits.
Do not force every table into a hypertable.
Review the existing Phase 1 schema before adding migrations.
Use Alembic migrations.
Potential tables include:
Assets
Store:

* Internal asset ID
* Symbol
* Asset class
* Exchange
* Currency
* Status
* Instrument characteristics

Asset provider mappings
Store:

* Asset ID
* Provider
* Provider-specific symbol
* Exchange identifier
* Effective dates
* Mapping status

Raw market events
Where appropriate, retain:

* Original provider payload
* Provider event identifier
* Event timestamp
* Received timestamp
* Provider
* Feed
* Processing status

Preserve raw provider information where practical for auditability and normalization debugging.
Candles
Store:

* Asset ID
* Timeframe
* Timestamp
* OHLC
* Volume
* VWAP
* Trade count
* Provider
* Feed
* Quality status
* Revision information

Quotes
Store:

* Asset ID
* Bid
* Ask
* Sizes
* Provider
* Feed
* Timestamp

Trades
Store:

* Asset ID
* Price
* Quantity
* Exchange
* Provider
* Feed
* Timestamp

Provider status
Track:

* Provider
* Connection status
* Feed
* Last event received
* Last successful retrieval
* Last error
* Subscription status

Data quality events
Track:

* Missing intervals
* Stale data
* Duplicate events
* Invalid prices
* Rejected messages
* Provider outages
* Recovery events

Ingestion jobs
Track:

* Requested symbols
* Timeframe
* Start/end timestamps
* Status
* Records retrieved
* Records stored
* Missing records
* Errors
* Start/completion timestamps

Design appropriate:

* Primary keys
* Foreign keys
* Unique constraints
* Query indexes
* Database numeric precision
* Time-based indexes
* TimescaleDB hypertable keys where applicable

If using TimescaleDB, ensure uniqueness constraints are compatible with the hypertable partitioning design.
Avoid duplicate observations caused by reconnects and overlapping historical retrievals.
Use idempotent ingestion.
Do not overwrite the raw source event when handling corrected data.
PART 8 — DATA QUALITY ENGINE
Implement an explicit data-quality validation layer.
Check:

* Missing required fields
* Invalid prices
* Negative volume
* Impossible OHLC relationships
* Duplicate records
* Out-of-order events
* Stale timestamps
* Incomplete candles
* Invalid bid/ask relationships
* Unexpected symbol mappings
* Provider outages

Validate the OHLC relationship:
High must be greater than or equal to Open and Close.
Low must be less than or equal to Open and Close.
High must be greater than or equal to Low.
Do not silently correct invalid records.
Reject them or quarantine them with an explanation.
Freshness rules
Do not implement one universal 60-second stale-data threshold.
Freshness requirements must depend on:

* Market
* Timeframe
* Data type
* Provider
* Exchange session
* Expected provider behavior

For example, a daily candle during a weekend should not automatically be considered a failure.
Distinguish market closure from an actual data-feed interruption.
The data-quality engine should return explicit states such as:

* VALID
* STALE
* INCOMPLETE
* MISSING
* INVALID
* QUARANTINED

Do not allow invalid or unreliable data to silently appear as normal validated market data.
PART 9 — HISTORICAL/LIVE DATA CONSISTENCY
Historical REST data and live WebSocket data must use the same normalized schema.
They must be distinguishable by provenance.
Implement consistent deduplication and conflict handling.
When a live candle and a historical candle overlap:

1. Identify the same asset and timeframe.
2. Identify the event timestamp and feed.
3. Compare source revisions where supported.
4. Preserve original observations.
5. Update the normalized current view according to documented precedence rules.
6. Record any correction.

Do not overwrite historical decision-time snapshots.
A corrected candle must not retroactively alter a trading decision that already occurred.
This requirement becomes critical when Atlas eventually performs backtesting.
PART 10 — API ENDPOINTS
Expose the market-data system through FastAPI.
Create versioned endpoints following existing Phase 1 conventions.
Potential endpoints:
GET `/api/v1/market-data/status`
GET `/api/v1/market-data/providers`
GET `/api/v1/market-data/assets`
GET `/api/v1/market-data/assets/{asset_id}`
GET `/api/v1/market-data/candles`
GET `/api/v1/market-data/quotes`
GET `/api/v1/market-data/trades`
GET `/api/v1/market-data/quality`
GET `/api/v1/market-data/ingestion-jobs`
POST `/api/v1/market-data/historical-fetch`
Use proper validation, pagination, and error responses.
Do not expose provider API credentials through any endpoint.
Protect operational endpoints appropriately.
Do not create trading, order-submission, or brokerage execution endpoints.
PART 11 — DASHBOARD INTEGRATION
Implement a basic Market Data dashboard section.
It should display:
Provider status

* Provider name
* Connection status
* Feed
* Authentication status
* Last successful update
* WebSocket status

Active assets

* Symbol
* Exchange
* Asset class
* Last known price
* Last event timestamp
* Data freshness
* Quality status

Historical data

* Selected asset
* Timeframe
* Historical candle records
* Data availability
* Missing intervals

Data ingestion

* Active ingestion jobs
* Completed jobs
* Failed jobs
* Number of records retrieved
* Number of records persisted

Data quality

* Stale feed warnings
* Missing data
* Invalid records
* Provider errors
* Recovery events

Use existing frontend architecture.
Do not redesign the entire dashboard.
Use actual API-backed information.
If a feature is not implemented, clearly show that it is unavailable rather than displaying fake market data.
PART 12 — ENVIRONMENT CONFIGURATION
Add the necessary environment variables to `.env.example`.
Potential variables:

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_DATA_FEED=iex

MARKET_DATA_PROVIDER=alpaca

MARKET_DATA_SYMBOLS=SPY,QQQ,AAPL,MSFT,NVDA

MARKET_DATA_STREAM_ENABLED=false

MARKET_DATA_RECONNECT_MAX_DELAY=
MARKET_DATA_HEARTBEAT_TIMEOUT=

HISTORICAL_DATA_BATCH_SIZE=
HISTORICAL_DATA_MAX_RETRIES=
```

Use configuration values with appropriate validation and documented defaults.
Do not store credentials in source files.
Do not print credentials in logs.
Do not require actual credentials to run the ordinary test suite.
Do not ask me to paste API keys into the Claude Code conversation.
When real credentials are available, I will configure them locally.
The system should start safely with market-data streaming disabled until explicitly configured.
PART 13 — TESTING
Build tests alongside implementation.
Use pytest.
Unit tests
Test:

* Provider response normalization
* Candle models
* Quote models
* Trade models
* Timestamp handling
* Data-quality rules
* Duplicate detection
* OHLC validation
* Missing-volume handling
* Symbol mapping
* Feed identification
* Provider errors

Provider adapter tests
Use recorded or carefully constructed fixture responses clearly labeled as test data.
Mock network requests in ordinary unit tests.
Verify:

* Authentication headers
* Endpoint parameters
* Pagination
* Error handling
* Rate-limit handling
* WebSocket message parsing
* Subscription handling
* Reconnection behavior

Mocks must never be represented as real provider data.
Database tests
Verify:

* Migrations
* Persistence
* Unique constraints
* Duplicate-event handling
* Timestamp precision
* Numeric precision
* Query performance for expected access patterns
* Correct asset/provider mappings

Integration tests
Test:
Provider response
-> Provider adapter
-> Normalization
-> Data validation
-> Database persistence
-> API retrieval
-> Dashboard display
Failure tests
Simulate:

* Provider disconnection
* Authentication errors
* Malformed messages
* Missing volume
* Invalid OHLC
* Duplicate WebSocket events
* Out-of-order events
* Historical pagination failure
* Rate limits
* Database interruption
* Reconnection
* Historical backfill
* Corrected candles

Verify that no missing or invalid market data is silently accepted.
Optional real-provider smoke tests
If credentials are available, create opt-in integration tests.
These must be disabled by default and clearly separated from ordinary automated testing.
Do not fail the ordinary test suite merely because API credentials are unavailable.
Never use order-submission endpoints for testing.
PART 14 — PERFORMANCE AND RESOURCE MANAGEMENT
The initial system is not an ultra-low-latency trading platform.
Prioritize correctness, reliability, and maintainability.
Use Polars for analytical processing where beneficial.
Use NumPy and compatible ML libraries where appropriate.
Avoid unnecessary conversions between data formats.
Implement:

* Efficient bulk inserts
* Historical pagination
* Bounded queues
* Controlled concurrency
* Database connection pooling
* Backpressure handling
* Appropriate indexes
* Configurable subscription limits

Do not download entire market histories into memory unnecessarily.
Do not implement complex distributed infrastructure prematurely.
Record basic metrics for:

* Ingestion latency
* Processing latency
* Database write performance
* Records received
* Records rejected
* Reconnection frequency
* Missing data intervals

PART 15 — SECURITY AND PAPER-ONLY RESTRICTIONS
This remains a market-data implementation project.
The system must not:

* Submit real orders
* Submit paper brokerage orders
* Connect to brokerage trading endpoints
* Modify brokerage accounts
* Enable live trading
* Store unnecessary brokerage permissions

Use only the market-data operations needed for this phase.
Keep provider credentials in environment configuration.
Do not expose keys to frontend code.
Do not include credentials in logs, exceptions, or API responses.
Market-data credentials may be distinct from future execution credentials.
Do not implement the future execution architecture during Phase 2.
PART 16 — PHASE 2 ACCEPTANCE CRITERIA
Phase 2 is complete only when all applicable criteria pass.
Provider architecture

* A reusable provider interface exists.
* Alpaca implements that interface.
* Provider capabilities are discoverable.
* Provider-specific logic is isolated.

Historical data

* Historical candles can be requested.
* Pagination works.
* Responses are normalized.
* Records are persisted.
* Duplicate observations are handled.
* Incomplete retrievals are identified.

Live data

* WebSocket authentication works.
* Subscriptions work for the configured feed.
* Messages are parsed and normalized.
* Records are persisted.
* Reconnection works.
* Subscriptions are restored.
* Missing intervals can be detected and backfilled where possible.

Database

* Migrations are successful.
* Market records persist correctly.
* Asset mappings work.
* Unique constraints prevent duplicates.
* Raw source information is retained where designed.
* Historical records remain traceable.

Data quality

* Invalid candles are rejected or quarantined.
* Missing values remain distinguishable.
* Freshness is context-aware.
* Feed identity is preserved.
* Data outages are recorded.

API

* Market data can be queried.
* Provider health can be queried.
* Data-quality events can be queried.
* Errors are handled properly.

Dashboard

* Provider status is visible.
* Active asset information is visible.
* Actual market data can be inspected.
* Data-quality warnings are visible.

Testing

* Unit tests pass.
* Database tests pass.
* Provider adapter tests pass.
* Integration tests pass.
* Failure tests pass.
* Existing Phase 1 tests still pass.

PART 17 — PHASE 2 COMPLETION REPORT
After implementation, provide a detailed report containing:

1. Summary of work completed.
2. Final architecture.
3. Files created.
4. Files modified.
5. Database changes and migrations.
6. Provider integrations.
7. API endpoints.
8. Dashboard additions.
9. Environment variables.
10. Dependencies added.
11. Commands executed.
12. Automated test results.
13. Any tests that were skipped and why.
14. Remaining known limitations.
15. Any Alpaca subscription restrictions encountered.
16. Instructions to configure credentials locally.
17. Instructions to start the data engine.
18. Instructions to verify live connectivity.
19. Instructions to retrieve historical data.
20. Phase 3 readiness assessment.

Do not claim live ingestion works unless it has actually been tested against the provider.
If credentials are unavailable, distinguish tested adapter behavior from unverified live connectivity.
Do not claim the phase is complete if critical acceptance criteria have failed.
FINAL INSTRUCTION
Build Phase 2 only.
The immediate objective is to establish a reliable, real market-data pipeline that Atlas can trust for all future quantitative analysis.
Do not begin asset discovery, strategy development, market-regime classification, machine learning, portfolio management, or execution.
Those are subsequent phases.
Prioritize data correctness, source provenance, timestamp accuracy, fault recovery, and reliable storage.
Atlas's future decisions will only be as good as the market data it receives.
Build the data foundation correctly.
