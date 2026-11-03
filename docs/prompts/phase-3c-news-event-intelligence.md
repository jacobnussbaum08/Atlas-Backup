# Phase 3C prompt (as given)

ATLAS AI TRADER — PHASE 3C
NEWS & EVENT INTELLIGENCE ENGINE
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase before implementation.
First verify Phase 3A and 3B, including their tests and database migrations. Review the latest code audit. Stop if unresolved P0/P1 problems or broken dependencies exist.
Preserve all working functionality.
OBJECTIVE
Build a provider-independent News & Event Intelligence Engine that collects, stores, organizes, and makes financial news and events available to future Atlas analytical systems.
Initially use Alpaca as the actual news provider, using existing credentials.
Official documentation:

* Historical news: https://docs.alpaca.markets/us/docs/historical-news-data
* News API: https://docs.alpaca.markets/us/reference/news-3
* Real-time news: https://docs.alpaca.markets/us/docs/streaming-real-time-news

Verify current endpoints, authentication, pagination, subscription limitations, and streaming behavior before implementation.
1. PROVIDER ARCHITECTURE
Create a reusable news-provider interface.
Implement:

* Historical news retrieval.
* Real-time news streaming.
* Authentication.
* Pagination.
* Subscription management.
* Provider health.
* Error handling.
* Graceful shutdown.

Implement Alpaca first.
Keep provider-specific logic inside its adapter.
Design interfaces that can support future financial news and economic-calendar providers without rewriting the core engine.
Do not add expensive subscriptions without approval.
Respect provider licensing and usage restrictions. Do not scrape third-party articles or retain full article content unless permitted.
2. HISTORICAL NEWS INGESTION
Use Alpaca's historical news REST endpoint.
Support:

* Asset-specific searches.
* Multiple symbols.
* Configurable date ranges.
* Pagination.
* Historical retrieval jobs.
* Duplicate detection.
* Source provenance.
* Error handling.

Retrieve and normalize available:

* Article ID.
* Headline.
* Summary.
* Author.
* Publisher/source.
* Original URL.
* Related symbols.
* Publication timestamp.
* Updated timestamp.
* Article content only where permitted.
* Provider metadata.

Do not assume every article contains a summary, full content, author, or related symbols.
Missing information must remain missing.
3. REAL-TIME NEWS STREAMING
Implement Alpaca's separate real-time news WebSocket connection.
Support:

* Authentication.
* Subscription confirmation.
* Article reception.
* Message parsing.
* Normalization.
* Database persistence.
* Connection health.
* Reconnection.
* Subscription restoration.
* Graceful shutdown.

Run the news stream in a background worker.
Do not create a new provider connection for every dashboard user.
Do not interfere with the existing stock market-data WebSocket worker.
Track received articles and reconnect interruptions independently.
After reconnection, use historical news retrieval with an overlapping time window to recover potentially missed articles where available.
Use idempotent ingestion to prevent duplicates.
4. NEWS DATABASE
Inspect existing database models before creating migrations.
Implement or extend tables for:

* news_providers
* news_articles
* news_article_revisions
* news_asset_mappings
* news_sources
* news_ingestion_jobs
* news_events
* event_classifications
* event_asset_relationships

Use appropriate primary keys, indexes, foreign keys, and uniqueness constraints.
Store:

* Internal article ID.
* Provider article ID.
* Provider.
* Source.
* Headline.
* Summary.
* URL.
* Related symbols.
* Mapped Atlas asset IDs.
* Published timestamp.
* Provider updated timestamp.
* First received timestamp.
* Processed timestamp.
* Available timestamp.
* Content hash.
* Revision information.
* Classification version.

Preserve original provider timestamps.
Do not confuse publication time with the time Atlas first received the article.
Historical retrieval must not pretend that the original real-time availability timestamp is known when it is not.
Preserve article revisions rather than silently overwriting historical records.
PostgreSQL remains the source of truth.
5. ASSET MAPPING
Map provider article symbols to existing Atlas asset IDs.
Handle:

* Multiple associated assets.
* Unknown symbols.
* Inactive assets.
* Ambiguous mappings.
* Symbol changes.
* Unmapped articles.

Do not force an ambiguous symbol onto an incorrect asset.
Preserve unlinked articles for broader market research.
Use effective-dated provider mappings where applicable.
6. EVENT CLASSIFICATION
Create a deterministic, versioned event taxonomy.
Initial categories:

* Earnings.
* Analyst ratings and price targets.
* Mergers and acquisitions.
* Corporate actions.
* Company announcements.
* Product launches.
* Management changes.
* Regulatory developments.
* Litigation.
* Macroeconomic developments.
* Federal Reserve and central-bank developments.
* Inflation.
* Employment.
* Geopolitical events.
* Industry developments.
* Cryptocurrency developments.
* Other / unclassified.

Use provider metadata where available and deterministic classification rules where necessary.
Record classification method, version, evidence, and confidence information.
Distinguish provider-supplied categories from Atlas-generated classifications.
Do not claim a classification is verified when it is only a keyword-based estimate.
Do not fabricate event importance, economic surprise, market impact, or sentiment.
7. ECONOMIC EVENT ARCHITECTURE
Design a standardized schema for scheduled economic events.
Support future storage of:

* Event name.
* Country.
* Currency.
* Scheduled timestamp.
* Previous value.
* Forecast.
* Actual value.
* Event importance.
* Source.
* Revision history.

Create an extensible economic-calendar provider interface.
Do not fabricate an economic calendar or pretend Alpaca's news stream is a scheduled macroeconomic calendar.
Initially, document the absence of an independently verified calendar provider unless an existing supported source is available.
Keep provider selection and any additional paid subscription separate from this deliverable.
8. NEWS RELEVANCE AND RECENCY
Implement basic, explainable relevance calculations.
Consider:

* Direct asset association.
* Number and quality of mapped asset relationships.
* Article recency.
* Provider metadata.
* Source information.

Keep relevance separate from sentiment and predicted price impact.
Do not assign arbitrary scores that imply predictive accuracy.
Version the ranking methodology and store its supporting factors.
9. HISTORICAL CORRECTNESS
Preserve:

* Published timestamp.
* Provider update timestamp.
* First received timestamp.
* Feature availability timestamp.

Ensure future backtests only use information that was available at the simulated historical decision time.
Historical news retrieved after publication must not automatically be treated as though Atlas received it at the original publication timestamp.
Overlapping historical retrieval and live streaming must not create duplicate articles.
Article corrections or updates must preserve revision history.
10. API AND DASHBOARD
Extend the existing FastAPI and React architecture.
Add endpoints for:

* News feed.
* News article details.
* Asset-specific news.
* News ingestion jobs.
* Event classifications.
* Events associated with assets.
* News provider status.
* News stream health.
* Available economic calendar status.

Add a professional News & Events dashboard page.
Display:

* Recent headlines.
* Publisher.
* Publication time.
* Related assets.
* Event category.
* Source link.
* Article summary where available.
* Provider status.
* Historical and real-time data indicators.

Add filtering by asset, source, event category, and time range.
Sanitize external text and safely handle HTML. Do not render untrusted article HTML directly.
Do not expose credentials.
Use actual backend data, not sample headlines presented as real news.
11. TESTING
Test:

* REST authentication.
* Historical pagination.
* WebSocket authentication.
* Streaming subscriptions.
* Message normalization.
* Article deduplication.
* Article revisions.
* Asset mapping.
* Unknown symbols.
* Event classification.
* Timestamp correctness.
* Availability-time handling.
* Provider outages.
* Reconnection.
* Historical news backfill.
* Database persistence.
* API endpoints.
* Dashboard rendering.
* Security and content sanitization.

Use clearly labeled fixtures for ordinary automated tests.
Keep actual Alpaca smoke tests opt-in and separate from the normal test suite.
Never print credentials.
Preserve all previous phase tests.
12. SCOPE RESTRICTIONS
Implement Phase 3C only.
Do not implement:

* LLM news interpretation.
* AI sentiment analysis.
* Predictive event-impact models.
* Trading signals.
* Event-driven trading strategies.
* Portfolio management.
* Risk approval.
* Order execution.
* Brokerage integrations.

Those functions will be developed later in the AI and Strategy phases.
News must remain supporting information, never direct authorization to trade.
13. ACCEPTANCE CRITERIA
Phase 3C is complete when:

1. Alpaca historical news retrieval works.
2. Historical pagination works.
3. Real-time news streaming works where subscription access permits.
4. Articles are normalized and persisted.
5. Duplicate articles are prevented.
6. Revisions are preserved.
7. Articles are mapped to Atlas assets where unambiguous.
8. Event classifications are versioned and explainable.
9. Publication and availability timestamps are distinguished.
10. Provider failures and reconnection are handled.
11. API endpoints work.
12. The dashboard displays actual news data.
13. Unavailable economic-calendar functionality is clearly identified.
14. Existing backend and frontend tests pass.
15. New tests pass.
16. No trading or execution functionality is introduced.

14. COMPLETION REPORT
Provide:

* Architecture.
* Provider functionality.
* Historical and real-time ingestion results.
* Database migrations.
* Event taxonomy.
* Asset-mapping methodology.
* API endpoints.
* Dashboard additions.
* Files created and modified.
* Test results.
* Actual Alpaca connectivity results.
* Subscription limitations.
* Licensing limitations.
* Unimplemented functionality.
* Readiness for Phase 4.

Do not claim real-time news works unless actually verified.
Do not claim an economic calendar exists unless it has been connected to a real provider.
Do not claim news classifications are predictive.
FINAL INSTRUCTION: Implement Phase 3C only. Build a reliable, timestamp-correct, auditable News & Event Intelligence foundation that future Atlas strategies and AI models can consume.
