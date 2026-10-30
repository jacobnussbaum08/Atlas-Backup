# Phase 2C — Live market data streaming (instructions as given)

> Recorded from the user's Deliverable 2C brief; bullet lists are condensed into
> semicolon-separated lines, wording otherwise unchanged. It refines `ATLAS_MASTER_SPEC_V2.md` §7 and the
> Phase 2 brief ([`phase-2-market-data-engine.md`](phase-2-market-data-engine.md), Part 5). The
> implementation report is
> [`../phase-reports/phase-2c-live-streaming-report.md`](../phase-reports/phase-2c-live-streaming-report.md).

---

ATLAS AI TRADER — PHASE 2C
LIVE MARKET DATA STREAMING

We are beginning Deliverable 2C of Atlas AI Trader.
Read `ATLAS_MASTER_SPEC_V2.md` and the Phase 2 Market Data Engine specification completely.
First inspect the completed implementations of Deliverables 2A and 2B.
Before proceeding, verify:

* The Alpaca provider architecture works.
* Historical AAPL and SPY candles can be retrieved.
* Historical data persists correctly in PostgreSQL.
* Pagination works.
* Feed identity is preserved.
* Data-quality validation works.
* Existing Phase 1, 2A, and 2B tests pass.

If any critical acceptance criteria from 2A or 2B remain unmet, stop and report the issue.
Do not recreate existing functionality.
Your task is to implement actual live market-data streaming through Alpaca WebSockets.
This is a real provider integration, not a simulation.

1. OFFICIAL DOCUMENTATION
Consult the current official Alpaca documentation before implementing.
Verify: WebSocket URLs; authentication protocol; subscription messages; available channels;
feed restrictions; message formats; connection limits; heartbeat behavior; error messages;
reconnection requirements.
Use the existing Alpaca adapter and normalized market-data architecture.
Do not introduce a separate provider architecture.

2. INITIAL LIVE UNIVERSE
Start with AAPL and SPY. Use existing database asset mappings and provider configuration.
Do not hardcode symbols throughout the streaming system. Keep the subscription universe
configurable so QQQ, MSFT, and NVDA can be added later.
Use the configured feed: `ALPACA_DATA_FEED=iex`. Do not silently switch to SIP or another feed.
Clearly identify the feed used for every observation. Do not pretend IEX represents the complete
consolidated US market.

3. WEBSOCKET CLIENT
Implement an asynchronous Alpaca WebSocket client. It must support the live data channels
available to the configured account: bars; updated bars, if supported; trades; quotes.
Implement: connection establishment; authentication; authentication confirmation; subscription;
subscription confirmation; message parsing; message normalization; data validation; persistence;
connection-health reporting; graceful disconnection.
Do not assume that a successful TCP/WebSocket connection means authentication or subscription
succeeded. Track these states separately.

4. BACKGROUND WORKER
The WebSocket connection must run independently of FastAPI request handling. Implement it as a
dedicated background worker using the existing project architecture. The backend API must remain
responsive while the stream is operating.
The worker must: maintain its own connection lifecycle; load configured assets; subscribe to the
necessary channels; process incoming messages; persist validated observations; report
operational health; shut down gracefully.
Do not start a separate WebSocket connection every time a dashboard page is opened. There should
be one managed streaming process per configured provider/feed connection, with controlled
subscriptions.

5. NORMALIZATION AND PERSISTENCE
Use the existing normalized schemas from Deliverables 2A and 2B. Do not create duplicate candle,
quote, or trade models.
Every incoming observation must preserve: internal asset ID; provider symbol; provider; feed;
data type; market event timestamp; received timestamp; processing timestamp; availability
timestamp; data-quality status; source event identifier where available.
For live data, `available_at` should represent when the validated information became available
to Atlas for downstream use. Do not confuse it with the original exchange event timestamp.
Use UTC. Preserve decimal precision appropriately. Missing fields must remain missing rather than
being fabricated.

6. LIVE BAR HANDLING
Implement live candle processing using the provider's documented message semantics.
Distinguish: initial bar messages; updated bar messages; finalized or completed bars, where
supported. Do not assume every received bar is final. Preserve original observations and track
revisions where necessary. A corrected candle must not silently rewrite the information that
Atlas actually observed at an earlier decision time. Ensure historical REST ingestion and live
WebSocket ingestion remain consistent with the same normalized data model.

7. REAL-TIME QUOTES AND TRADES
Implement quote and trade normalization where the configured feed and subscription support them.
Quotes should retain: bid; ask; bid size; ask size; timestamp; provider; feed.
Trades should retain: price; quantity; exchange and conditions where available; timestamp;
provider; feed.
Validate incoming messages. Reject or quarantine invalid records. Do not invent unavailable
fields. Do not build trading indicators or strategy calculations in this deliverable.

8. CONNECTION RELIABILITY
Implement robust connection management. Handle: unexpected disconnection; network interruption;
authentication failure; subscription failure; invalid messages; provider shutdown; connection
timeout; duplicate events; out-of-order events; temporary database failures.
Use bounded exponential backoff for recoverable failures. Do not retry permanently invalid
authentication forever.
Track: connection state; last successful authentication; last successful subscription; last
message received; last message persisted; last error; reconnection count.
Subscription restoration must occur after a successful reconnection. Do not silently report a
healthy stream when the connection is disconnected. Use bounded queues and backpressure handling.
Do not silently discard market data when queues fill or processing falls behind. Record dropped
or rejected messages and expose the condition through health monitoring.
PostgreSQL remains the source of truth for persisted data. Redis must not become the
authoritative store for market observations.

9. HISTORICAL AND LIVE DATA CONSISTENCY
Live data and historical REST data must share the same normalized schemas.
When live observations overlap with historical records: identify the asset; identify the
timeframe; identify the event timestamp; identify the provider and feed; preserve raw
provenance; apply documented revision rules; avoid duplicate records.
Do not retroactively alter historical decision-time snapshots. If the connection has an outage,
record the interruption interval. The detailed automated historical backfill and recovery
workflow will be handled in Deliverable 2D. For this deliverable, ensure the architecture
exposes enough information to support that recovery process.

10. CONFIGURATION
Use existing settings. Relevant variables include: `MARKET_DATA_PROVIDER=alpaca`,
`ALPACA_DATA_FEED=iex`, `MARKET_DATA_SYMBOLS=AAPL,SPY`, `MARKET_DATA_STREAM_ENABLED=false`.
Add other configuration fields only where necessary. Validate configuration using the existing
Pydantic settings architecture. Keep streaming disabled by default unless explicitly enabled
locally. Never print API keys or secret values. Do not request that I paste credentials into
Claude Code.

11. HEALTH AND MONITORING
Expose streaming status through the existing FastAPI architecture. Provide information such as:
provider; feed; connection state; authentication state; subscription state; subscribed symbols;
active channels; last received message; last persisted message; reconnection count; recent
errors. Use explicit status states. A connected stream is not necessarily a healthy stream if
it has stopped receiving or persisting data unexpectedly. Respect market closures and
provider-specific feed behavior when determining staleness. Do not implement the complete
dashboard in this deliverable. The dashboard integration belongs in Deliverable 2E.

12. TESTING
Use pytest and the existing testing architecture.
Unit tests for: WebSocket authentication flow; subscription messages; authentication responses;
subscription responses; message parsing; candle normalization; quote normalization; trade
normalization; timestamp handling; duplicate detection; invalid message handling;
connection-state transitions.
Integration tests for: worker initialization; streaming message processing; database
persistence; shutdown; error handling; reconnection; subscription restoration.
Use mocks and controlled test fixtures for ordinary automated tests. Clearly label mock tests.
Do not describe mock WebSocket tests as real Alpaca connectivity tests. Create opt-in
real-provider smoke tests using the local credentials, disabled by default. If credentials are
available, verify actual authentication and data subscription against Alpaca. Do not expose
credentials in test output. Do not submit brokerage orders. All existing Phase 1, 2A, and 2B
tests must continue passing.

13. PERFORMANCE
Prioritize correctness and reliability. Implement: asynchronous message handling; bounded
queues; controlled database writes; appropriate bulk persistence; efficient connection
management; processing-latency metrics; safe shutdown. Do not introduce unnecessary distributed
infrastructure. Do not build an ultra-low-latency trading system.

14. SCOPE RESTRICTIONS
Implement Deliverable 2C only. Do not implement: automatic asset discovery; market-regime
detection; technical indicators; trading strategies; machine learning; LLM news analysis;
portfolio management; risk approval; order execution; brokerage integration; complete dashboard
redesign; full historical gap recovery. These belong to later deliverables and phases.

15. ACCEPTANCE CRITERIA
Deliverable 2C is complete only when:
1. The existing 2A and 2B architecture remains functional.
2. Alpaca WebSocket authentication succeeds.
3. The configured IEX feed is used explicitly.
4. AAPL and SPY subscriptions work where supported.
5. Live market messages are received.
6. Supported bar, trade, and quote messages are normalized.
7. Actual received observations can be persisted in PostgreSQL.
8. Event and availability timestamps are preserved.
9. Invalid observations are rejected or quarantined.
10. Duplicate handling works.
11. Connection status is observable.
12. Reconnection works.
13. Subscriptions are restored after reconnection.
14. FastAPI remains responsive while streaming operates.
15. Shutdown does not leave orphaned workers.
16. Existing regression tests pass.
17. New automated tests pass.
18. A real-provider smoke test is performed where credentials and subscription access permit.
If a data channel is not available under the configured subscription, document the limitation
instead of pretending it works.

16. COMPLETION REPORT
Provide: architecture summary; files created; files modified; WebSocket connection design; worker
lifecycle; channels supported; feed limitations; database changes; API endpoints; unit test
results; integration test results; regression test results; real-provider smoke-test results;
any errors or limitations; instructions to start and stop streaming locally; how to inspect
provider health; whether Atlas is ready for Deliverable 2D.
Do not claim actual streaming works unless it has been verified against Alpaca.

FINAL INSTRUCTION: Implement Deliverable 2C only. Prioritize accurate live data, reliable
connections, safe persistence, and preservation of historical information.
