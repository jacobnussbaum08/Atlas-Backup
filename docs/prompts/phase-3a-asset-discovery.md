# Phase 3A prompt (as given)

ATLAS AI TRADER — PHASE 3A
DYNAMIC ASSET DISCOVERY & UNIVERSE MANAGEMENT
You are the senior quantitative systems engineer responsible for implementing Phase 3A of Atlas AI Trader.
Read `ATLAS_MASTER_SPEC_V2.md` completely before beginning.
This deliverable establishes Atlas's ability to discover, qualify, rank, and organize financial instruments automatically.
Atlas must no longer depend on a permanently hardcoded list of tickers.
The goal is to create a dynamic, provider-independent asset universe that can eventually support stocks, ETFs, forex, and cryptocurrency.
For this initial implementation, actual discovery and qualification will focus on US stocks and ETFs supported by the existing Alpaca integration.
Do not fabricate support for markets that do not yet have functioning data providers.
PART 1 — VALIDATE PREVIOUS PHASES
Before implementation, inspect:

* Phase 1 foundation.
* Deliverables 2A–2E.
* Existing asset database models.
* Provider reference-data capabilities.
* Alpaca adapter.
* Historical data ingestion.
* Live market-data streaming.
* Data-quality system.
* Recovery and backfill systems.
* Existing dashboard.

Inspect Git status and verify the Phase 2 checkpoints.
Run the existing backend and frontend test suites.
Verify that actual historical market data and live market-data functionality remain operational.
Do not overwrite existing work.
Do not recreate functionality that already exists.
If a critical dependency is broken, report it before implementation.
PART 2 — ASSET IDENTITY AND REFERENCE DATA
Build a proper instrument identity system.
Do not rely on ticker symbols as permanent asset identifiers.
Symbols can change, be reused, or represent different instruments on different exchanges.
Each asset must have a stable internal Atlas ID.
Store relevant metadata:

* Internal asset ID.
* Symbol.
* Asset name.
* Asset class.
* Exchange.
* Exchange identifier.
* Currency.
* Country.
* Instrument type.
* Active status.
* Tradability status where supplied.
* Margin eligibility where supplied.
* Shortability where supplied.
* Borrow availability where supplied.
* Fractional-trading eligibility where supplied.
* Tick size where available.
* Lot-size rules where available.
* Provider identifiers.
* First discovered timestamp.
* Last verified timestamp.
* Metadata source.

Use provider-specific asset mappings.
One Atlas asset may have mappings to multiple providers in the future.
Preserve the effective dates of provider symbol mappings.
Support asset lifecycle events such as:

* New listing.
* Symbol change.
* Exchange change.
* Trading suspension.
* Delisting.
* Reactivation.

Do not delete historical instruments simply because they become inactive.
Historical instrument identity must remain available for backtesting.
Use immutable history or appropriately versioned metadata records for changes that affect historical interpretation.
PART 3 — PROVIDER REFERENCE DATA INTEGRATION
Use the existing provider abstraction.
Implement actual instrument-directory discovery through the existing Alpaca adapter.
Consult current official provider documentation before implementing additional endpoints.
Retrieve asset reference information supported by the account and API.
Support:

* Listing available instruments.
* Retrieving asset metadata.
* Identifying supported exchanges.
* Identifying asset classes.
* Identifying tradability.
* Identifying active/inactive status.
* Refreshing asset metadata.

Do not place provider-specific HTTP logic in the core discovery engine.
The discovery engine must consume standardized internal asset records.
Do not implement Polygon, OANDA, Coinbase, or other provider integrations during this deliverable.
However, the provider interface must remain extensible so additional market providers can be introduced later.
PART 4 — DYNAMIC DISCOVERY POOL
Implement the first universe layer: Discovery Pool.
The Discovery Pool should contain instruments discovered through supported provider directories.
It must support:

* Initial full discovery.
* Incremental discovery.
* Periodic metadata refresh.
* Detection of newly listed instruments.
* Detection of inactive instruments.
* Provider mapping updates.
* Discovery job history.
* Discovery failures and retries.

Do not assume every discovered instrument is eligible for analysis.
The Discovery Pool is only a catalog of known instruments.
Do not automatically subscribe to every instrument.
Do not automatically download unlimited historical data for every discovered instrument.
Respect provider limits and use bounded, controlled discovery jobs.
Make discovery refresh frequency configurable.
Use appropriate scheduling rather than continuously re-downloading a complete asset directory.
PART 5 — QUALIFIED UNIVERSE
Implement the second layer: Qualified Universe.
The Qualified Universe contains only instruments passing Atlas eligibility requirements.
Create configurable qualification rules.
Evaluate the following where data is available:
Instrument eligibility

* Supported asset type.
* Supported exchange.
* Active trading status.
* Valid provider mapping.
* Supported market-data capabilities.
* Adequate historical coverage.
* Sufficient data quality.

Liquidity

* Average daily volume.
* Average dollar volume.
* Bid/ask spread.
* Available quote quality.
* Liquidity consistency.

Price characteristics

* Current price.
* Historical volatility.
* Average true range where available.
* Recent trading activity.
* Minimum price requirements where configured.

Data reliability

* Missing candle frequency.
* Stale data.
* Invalid observations.
* Historical completeness.
* Provider availability.

Short eligibility
Where provider metadata supports it, preserve:

* Shortability.
* Borrow availability.
* Easy-to-borrow status.
* Margin eligibility.

An asset being shortable according to reference data does not guarantee that borrow will remain available later.
Short eligibility must be revalidated at the time of any future proposed short trade.
Do not hardcode universal thresholds that assume all markets behave identically.
Use asset-class-specific configurable rules.
For initial US equities and ETFs, make qualification parameters configurable and document the default values.
Do not reject an otherwise legitimate asset solely because an optional data field is unavailable. Instead, distinguish mandatory requirements from optional information.
PART 6 — HISTORICAL REQUIREMENTS AND LIQUIDITY CALCULATIONS
Build reusable calculations for asset qualification.
Potential measurements:

* Average daily volume.
* Average dollar volume.
* Rolling median dollar volume.
* Average quoted spread.
* Spread percentage.
* ATR.
* Realized volatility.
* Missing-data percentage.
* Historical data coverage.

Use appropriate lookback windows.
Avoid calculating qualification from incomplete or unreliable observations.
Clearly distinguish:

* Missing data.
* True zero volume.
* Market closure.
* Trading halt.
* Provider interruption.

For each measurement, retain:

* Asset ID.
* Calculation window.
* As-of timestamp.
* Source data range.
* Feature version.
* Result.
* Data-quality status.

Use Polars for columnar analytical work where beneficial.
Avoid excessive DataFrame conversions.
Use appropriate decimal precision where financial quantities are persisted.
Do not use future observations when calculating historical qualification.
PART 7 — ACTIVE WATCHLIST
Implement the third layer: Active Watchlist.
The Active Watchlist should prioritize qualified assets for further analysis.
Create a configurable ranking system.
Potential ranking factors:

* Relative trading volume.
* Recent volatility.
* Dollar volume.
* Price momentum.
* Relative strength.
* Spread quality.
* Data reliability.
* Liquidity.
* Recent market activity.

Do not assume higher volatility automatically means a better trading opportunity.
Do not confuse ranking with a trading signal.
The ranking system must only determine analytical priority.
It must not:

* Open positions.
* Approve trades.
* Override risk controls.
* Assign executable position sizes.
* Declare an asset profitable.

Create a scoring system that records:

* Asset.
* Individual factor scores.
* Overall score.
* Ranking.
* Score timestamp.
* Scoring algorithm version.
* Explanatory metadata.

Make ranking weights and thresholds configurable and versioned.
Avoid pretending that a high ranking means an asset is guaranteed to perform well.
PART 8 — POINT-IN-TIME UNIVERSE HISTORY
This is a critical requirement.
Preserve historical universe membership.
Atlas must be able to determine which instruments were:

* Discovered.
* Qualified.
* Disqualified.
* Added to the active watchlist.
* Removed from the active watchlist.
* Inactive.
* Delisted.

Record effective timestamps and the underlying reason for each change.
Historical backtests must be able to reconstruct the asset universe that was actually available at a specific historical time.
Do not allow today's active asset universe to replace historical universe membership.
Prevent survivorship bias.
Use immutable membership events or effective-dated records.
Do not delete historical membership when an asset becomes inactive.
PART 9 — MARKET SESSIONS AND CALENDAR INTEGRATION
Extend the existing market calendar architecture.
Do not replace the NYSE calendar functionality that already exists.
Make market-session logic exchange-aware.
Support:

* Regular trading sessions.
* Weekends.
* Holidays.
* Early closes.
* Daylight-saving changes.
* Market-specific time zones.
* Trading halts where data is available.
* Overnight markets where supported in the future.

Do not assume all US exchanges always share identical session behavior.
Use reliable calendar data or maintained calendar libraries where appropriate.
Avoid manually hardcoding annual holiday lists.
All internal timestamps remain UTC.
Market-session calculations must use the correct exchange timezone and date.
The discovery engine must not classify a market as inactive simply because it is currently outside its trading session.
PART 10 — DATABASE DESIGN
Inspect existing Phase 1 and Phase 2 database schemas before creating migrations.
Reuse existing asset and provider mapping tables where possible.
Do not create duplicate tables with conflicting responsibilities.
Extend the database to support:

* Asset metadata.
* Asset lifecycle history.
* Provider asset mappings.
* Discovery jobs.
* Discovery results.
* Qualification rules.
* Qualification evaluations.
* Qualified universe membership.
* Watchlist membership.
* Watchlist scores.
* Scoring versions.
* Universe membership history.
* Market-session metadata where necessary.

Use:

* Foreign keys.
* Appropriate uniqueness constraints.
* Indexes.
* UTC timestamps.
* Versioned configuration.
* Decimal/database numeric types where financial measurements require them.

Preserve the original raw provider reference data where practical.
Database changes must be managed through Alembic migrations.
Do not delete historical records.
PART 11 — DISCOVERY AND QUALIFICATION WORKERS
Implement background processing using the existing worker architecture.
Build separate logical jobs for:

1. Provider directory discovery.
2. Asset metadata refresh.
3. Qualification evaluation.
4. Watchlist scoring.
5. Universe membership updates.

Use bounded concurrency.
Prevent overlapping jobs from corrupting membership records.
Use database transactions for membership changes.
Jobs must be idempotent.
If a worker crashes, the system must recover without duplicating asset records or destroying historical membership.
Record job statuses:

* PENDING.
* RUNNING.
* COMPLETED.
* PARTIALLY_COMPLETED.
* FAILED.

Expose job history and errors.
PART 12 — API ENDPOINTS
Expose dynamic asset discovery through the existing FastAPI architecture.
Potential endpoints:
GET `/api/v1/assets`
GET `/api/v1/assets/{asset_id}`
GET `/api/v1/assets/discovery-pool`
GET `/api/v1/assets/qualified`
GET `/api/v1/assets/watchlist`
GET `/api/v1/assets/{asset_id}/qualification`
GET `/api/v1/assets/{asset_id}/history`
GET `/api/v1/discovery/jobs`
GET `/api/v1/discovery/jobs/{job_id}`
GET `/api/v1/discovery/rankings`
GET `/api/v1/market-sessions`
POST `/api/v1/discovery/run`
POST `/api/v1/discovery/refresh`
Validate all parameters.
Add pagination and filtering.
Use consistent response schemas.
Protect operational endpoints.
The frontend must not directly access provider credentials or private provider APIs.
PART 13 — DASHBOARD INTEGRATION
Extend the existing Atlas dashboard without redesigning working pages.
Add an Asset Discovery page.
Display:

* Total discovered assets.
* Qualified asset count.
* Active watchlist count.
* Newly discovered assets.
* Recently disqualified assets.
* Inactive instruments.
* Last discovery run.
* Discovery job status.

Add a Discovery Pool table.
Add a Qualified Universe table.
Add an Active Watchlist table.
For each asset show relevant metadata and qualification information.
Allow the user to:

* Search.
* Filter.
* Sort.
* Inspect asset details.
* View qualification reasons.
* Inspect ranking factors.
* View historical membership.

Display why an asset was rejected or disqualified.
Avoid misleading numerical scores without explanatory information.
Add a configurable discovery-settings page if the backend already supports safe configuration changes.
All configuration changes must be audited.
Do not introduce any buy, sell, or order-submission controls.
PART 14 — FAILURE HANDLING AND TESTING
Create comprehensive tests.
Unit tests

* Symbol normalization.
* Asset identity.
* Provider mapping.
* Metadata updates.
* Qualification filters.
* Liquidity calculations.
* Spread calculations.
* Missing-data handling.
* Watchlist ranking.
* Membership history.
* Market-session calculations.

Integration tests

* Alpaca asset retrieval.
* Database asset persistence.
* Repeated discovery.
* Metadata updates.
* Qualification pipeline.
* Watchlist scoring.
* Historical membership records.
* API responses.
* Dashboard integration.

Concurrency tests

* Simultaneous discovery jobs.
* Concurrent metadata refreshes.
* Duplicate asset ingestion.
* Concurrent qualification evaluations.
* Conflicting membership changes.

Failure tests

* Provider outage.
* Partial reference-data response.
* Invalid provider metadata.
* Missing historical observations.
* Inactive instruments.
* Delisted instruments.
* Symbol changes.
* Database interruption.
* Worker restart.
* Repeated discovery requests.

Look-ahead and survivorship tests
Verify that historical qualification and universe membership are reconstructed only from information available at the applicable historical timestamp.
Do not use current metadata to retroactively determine past tradability.
All existing tests from previous phases must continue passing.
Do not weaken tests to obtain successful results.
PART 15 — INITIAL MARKET SCOPE
This deliverable must support the existing US stocks and ETFs data pipeline.
Design the domain architecture for future:

* Forex.
* Cryptocurrency.
* Additional equity markets.

However, do not fabricate functioning integrations for unsupported markets.
Do not automatically discover assets outside the capabilities of configured providers.
Create clear provider capability checks.
Do not implement new provider subscriptions during this deliverable.
The architecture must permit future multi-market expansion without rewriting the discovery engine.
PART 16 — STRICT SCOPE RESTRICTIONS
Implement Phase 3A only.
Do not implement:

* Market-regime classification.
* Cross-asset correlation analysis.
* AI-generated market intelligence.
* Machine learning predictions.
* Trading strategies.
* Trading signals.
* Portfolio management.
* Position sizing.
* Risk approval.
* Order execution.
* Brokerage integration.

These belong to later development phases.
The Active Watchlist is an analytical prioritization tool, not a trading strategy.
PART 17 — ACCEPTANCE CRITERIA
Phase 3A is complete only when:

1. Atlas can discover supported instruments automatically.
2. Asset metadata is normalized.
3. Stable internal asset identities are maintained.
4. Provider-specific symbol mappings work.
5. Asset lifecycle changes are recorded.
6. Discovery jobs are tracked.
7. Qualified-universe filtering works.
8. Liquidity calculations work.
9. Missing information is handled honestly.
10. Active Watchlist ranking works.
11. Ranking factors are explainable.
12. Historical membership is preserved.
13. Market-session calculations are correct.
14. Duplicate discovery does not create duplicate instruments.
15. Historical membership does not suffer from present-day survivorship leakage.
16. New API endpoints function.
17. Dashboard pages show actual backend data.
18. Existing Phase 1 and Phase 2 tests pass.
19. New automated tests pass.
20. No trading or execution functionality has been introduced.

PART 18 — COMPLETION REPORT
At completion, report:

1. Architecture summary.
2. Asset identity design.
3. Provider reference-data implementation.
4. Discovery-pool functionality.
5. Qualification rules and defaults.
6. Liquidity calculation methodology.
7. Active Watchlist scoring methodology.
8. Historical membership design.
9. Market-session implementation.
10. Database changes.
11. API endpoints.
12. Dashboard additions.
13. Files created.
14. Files modified.
15. Unit-test results.
16. Integration-test results.
17. Concurrency-test results.
18. Historical bias tests.
19. Real-provider test results.
20. Known limitations.
21. Instructions for running discovery manually.
22. Whether Atlas is ready for Phase 3B.

Be transparent about actual provider capabilities and the limits of the discovered universe.
Do not claim that every publicly traded security has been discovered if the configured provider does not expose every instrument.
Do not claim a liquidity threshold is optimal without empirical validation.
Do not claim that a highly ranked asset represents a profitable opportunity.
FINAL INSTRUCTION: Implement Phase 3A only. Build a provider-independent, historically accurate, dynamically maintained asset universe. Prioritize asset identity, eligibility, liquidity, data quality, and point-in-time history.
