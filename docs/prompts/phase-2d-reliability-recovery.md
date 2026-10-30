# Phase 2D — Data reliability, recovery, backfill and quality hardening (instructions as given)

> Recorded from the user's Deliverable 2D brief; bullet lists are condensed into
> semicolon-separated lines, wording otherwise unchanged. It refines `ATLAS_MASTER_SPEC_V2.md` §7 and the
> Phase 2 brief ([`phase-2-market-data-engine.md`](phase-2-market-data-engine.md)). The
> implementation report is
> [`../phase-reports/phase-2d-reliability-recovery-report.md`](../phase-reports/phase-2d-reliability-recovery-report.md).

---

ATLAS AI TRADER — PHASE 2D
DATA RELIABILITY, RECOVERY, BACKFILL & QUALITY HARDENING

We are beginning Deliverable 2D of Atlas AI Trader.
Read `ATLAS_MASTER_SPEC_V2.md` and the complete Phase 2 Market Data Engine specification.
Inspect the completed Deliverables 2A, 2B, and 2C before making changes.
This deliverable focuses on ensuring Atlas can maintain reliable, consistent, recoverable
market-data records despite interruptions, missing observations, provider failures, database
outages, and inconsistent event delivery.
Do not rebuild working functionality.
Do not begin Phase 2E dashboard development or Phase 3.

PART 1 — VALIDATE PREVIOUS DELIVERABLES
Before implementation, inspect and test:
Deliverable 2A: provider abstraction; Alpaca adapter; provider capabilities; normalized schemas;
asset-provider mappings; database migrations.
Deliverable 2B: historical REST retrieval; pagination; feed selection; data normalization;
historical persistence; ingestion-job management; duplicate protection.
Deliverable 2C: WebSocket authentication; subscriptions; live message handling; normalization;
live persistence; connection monitoring; reconnection behavior.
Run all existing backend and frontend tests.
Verify real-provider behavior where credentials and subscription access permit.
If a critical earlier deliverable is broken, report it before implementing 2D.
Do not proceed by hiding or bypassing existing failures.

PART 2 — DATA COMPLETENESS AND GAP DETECTION
Build an explicit data-completeness monitoring system.
Atlas must be able to identify when expected market data is missing.
Implement gap detection primarily for OHLCV candles.
A missing candle must be distinguished from: a market closure; a weekend; an exchange holiday;
a trading halt; an instrument with no eligible trading activity; a delayed provider message; an
actual ingestion failure.
Do not assume that every minute during a trading day must contain a candle for every asset.
Use appropriate market-session and provider information to determine expected observations.
Implement context-aware gap detection based on: asset; market; exchange; timeframe; feed;
trading session; provider behavior.
Create a system that identifies: missing candle intervals; incomplete historical ranges;
unexpected gaps; late-arriving observations; duplicate observations; data received but not
persisted; previously identified gaps that have been recovered.
For quotes and trades, do not assume a continuous event must exist at every timestamp. Use
connection health, provider sequence information where available, and other documented evidence
to identify potential loss.
Do not fabricate missing market data.

PART 3 — AUTOMATIC HISTORICAL BACKFILL
Implement automated recovery of missing historical candles through the existing Alpaca REST
adapter.
When an outage or missing interval is identified: 1. Determine the affected asset and timeframe.
2. Determine the expected missing time range. 3. Verify that the interval was within the
relevant market session. 4. Create a backfill job. 5. Retrieve the missing historical
observations. 6. Validate the returned data. 7. Normalize the observations. 8. Persist them
idempotently. 9. Update the completeness record. 10. Record the recovery result.
Reuse the Deliverable 2B historical retrieval system.
Do not create a separate historical-data ingestion architecture.
Backfill must support: multiple affected assets; multiple timeframes; partial recovery; provider
pagination; rate limits; retried jobs; interrupted jobs; overlapping recovery requests.
Do not assume the provider has all requested data.
If the requested history is unavailable, record the unresolved gap accurately.
A backfill job must not be marked successful unless the requested recoverable intervals have
actually been addressed.
Support explicit statuses: PENDING; RUNNING; COMPLETED; PARTIALLY_COMPLETED; FAILED.
Keep a clear distinction between a recovered interval and an interval that remains unavailable.

PART 4 — WEBSOCKET INTERRUPTION RECOVERY
Harden the existing Deliverable 2C connection management.
Track: disconnection timestamp; last successfully received event; last successfully persisted
event; last completed candle; reconnection attempts; reconnection success; subscription
restoration; potential data-loss interval; recovery completion.
After reconnecting: 1. Verify connection establishment. 2. Authenticate again when required.
3. Restore subscriptions. 4. Confirm subscription state. 5. Determine what data may have been
missed. 6. Schedule appropriate historical recovery. 7. Verify recovered observations. 8. Record
unresolved gaps.
Do not report a successful recovery merely because the WebSocket connection was restored.
Recovery of candles, trades, and quotes must be evaluated separately according to the
historical endpoints and actual provider data availability.
If historical trade or quote recovery is unavailable under the subscription, explicitly record
that limitation.
Never claim complete trade or quote recovery without evidence.

PART 5 — DATABASE INTERRUPTION AND PERSISTENCE SAFETY
Test the behavior of the streaming worker when PostgreSQL becomes temporarily unavailable.
The system must not silently discard received data.
Evaluate and implement an appropriate recovery design.
Requirements: bounded buffering; controlled retries; backpressure handling; database
reconnection; idempotent persistence; duplicate-event prevention; clear data-loss reporting;
safe worker shutdown.
For recoverable candle data, historical REST backfill may restore missing intervals.
For trades and quotes, recognize that historical recovery may be limited.
If a persistent local spool is justified, design it as an explicitly bounded, recoverable
component with: safe writes; replay support; duplicate protection; storage limits; corruption
detection; cleanup after successful persistence.
Do not introduce a complicated persistence system unnecessarily.
If Atlas cannot safely accept incoming messages, expose the failure state and prevent the
system from falsely reporting a healthy data pipeline.
Do not silently drop observations when memory or queue limits are exceeded.
Do not let Redis become the authoritative source of persisted market data.

PART 6 — DUPLICATE AND OUT-OF-ORDER EVENT HANDLING
Harden duplicate detection.
Handle: repeated WebSocket messages; reconnect duplicates; historical/live overlap; repeated
backfill requests; out-of-order observations; corrected provider candles; replayed events after
a worker restart.
Use stable event identities where available.
Where the provider does not supply a stable event identifier, document the composite identity
and its limitations.
Do not assume two messages with the same timestamp are always identical.
Preserve source provenance.
Maintain the distinction between: original raw events; normalized current observations;
historical revisions; decision-time snapshots.
A corrected candle must never retroactively change what Atlas knew during an earlier historical
decision.
Implement database-level uniqueness constraints where appropriate.
All retry and replay operations must be idempotent.

PART 7 — CANDLE REVISION MANAGEMENT
Build or complete a revision-aware candle handling system.
Provider candles may be updated after their initial emission.
The system should preserve: original observation; updated observation; revision timestamp;
provider; feed; revision source; processing status.
The normalized current candle may reflect the latest accepted revision.
Historical decision snapshots must retain the exact version that was available at the time.
Do not silently overwrite raw observations.
Document the precedence rules used when historical REST and live WebSocket records overlap.

PART 8 — DATA-QUALITY ENGINE HARDENING
Expand the existing data-quality system.
Support states such as: VALID; STALE; INCOMPLETE; MISSING; INVALID; QUARANTINED; RECOVERED;
UNAVAILABLE.
Implement checks for:
Candles: invalid OHLC relationships; missing required fields; negative volume; duplicate
observations; unexpected timestamps; incorrect timeframe alignment; unexpected price changes;
incomplete candles; feed inconsistencies.
Quotes: negative prices; invalid bid/ask relationships; invalid sizes; stale observations;
duplicate messages; incorrect asset mappings.
Trades: invalid prices; invalid quantities; missing required timestamps; duplicate messages;
out-of-order events; incorrect asset mappings.
Do not assume every abnormal market movement is an error.
A genuine market gap or extreme price movement must not automatically be rejected simply because
it looks unusual.
Use validation rules that distinguish data errors from legitimate market events.
Missing values must remain missing.
Do not replace missing volume with zero.
Never silently correct invalid records.
Invalid observations must be rejected or quarantined with a recorded explanation.

PART 9 — PROVIDER HEALTH AND PIPELINE HEALTH
Distinguish provider connectivity from complete data-pipeline health.
Track separate health dimensions: 1. Provider connection. 2. Authentication. 3. Subscription.
4. Message reception. 5. Message processing. 6. Data validation. 7. Database persistence.
8. Historical completeness. 9. Backfill activity.
A WebSocket connection can be healthy while database persistence is failing.
The system must not report full pipeline health when an essential stage is malfunctioning.
Use explicit health states.
Record: last successful message; last successful write; last successful backfill; current
outstanding gaps; pending recovery jobs; failed jobs; recent provider errors.
Market closures and periods of inactivity must not automatically be interpreted as provider
outages.

PART 10 — RECOVERY JOB MANAGEMENT
Implement durable recovery-job tracking.
Each recovery job should contain: job ID; source outage or gap; asset; timeframe; provider;
feed; missing interval; requested range; retry count; records recovered; records rejected;
remaining gaps; status; error details; created timestamp; started timestamp; completed
timestamp.
Prevent duplicate recovery jobs for the same unresolved interval where appropriate.
If two recovery requests overlap, coordinate them safely.
A worker restart must not lose the overall recovery-job state.
Do not mark a job completed simply because a background task ended successfully.
Its actual data-completeness requirements must be evaluated.

PART 11 — OBSERVABILITY AND METRICS
Expose operational metrics through the existing monitoring architecture.
Track:
Ingestion: messages received; messages processed; messages persisted; messages rejected;
duplicate observations; processing latency.
Reliability: provider disconnections; reconnection attempts; successful reconnects; database
interruptions; worker crashes; retry counts.
Completeness: outstanding missing candle intervals; backfill jobs; recovered intervals;
unavailable intervals; incomplete historical ranges.
System performance: queue depth; processing backlog; database write latency; backfill duration;
worker health.
Do not add a new monitoring platform unless needed.
Expose metrics through existing application monitoring and API conventions.

PART 12 — ALERTS AND AUDIT LOGGING
Create auditable recovery events.
Record: outage detected; data gap identified; backfill initiated; backfill succeeded; backfill
partially succeeded; backfill failed; database failure; persistence recovery; duplicate
suppression; invalid data quarantine; unrecoverable data interval.
Use the existing audit and system-health architecture.
Do not send excessive notifications for ordinary market inactivity.
High-priority notifications should be reserved for genuine data integrity problems or prolonged
outages.

PART 13 — TESTING
Build comprehensive tests.
Unit tests: gap detection; market-session-aware expectations; duplicate handling; out-of-order
event handling; candle revision handling; quality classification; recovery state transitions;
retry calculations; job deduplication.
Integration tests: WebSocket interruption; historical backfill; database interruption; worker
restart; historical/live overlap; repeated recovery requests; recovery of partial intervals;
recovery failure; persistence replay where implemented.
Concurrency tests: multiple simultaneous recovery jobs; overlapping missing intervals; duplicate
event replay; concurrent ingestion and backfill; database constraint enforcement.
Failure tests — simulate: provider outage; network failure; WebSocket disconnect; database
disconnect; Redis interruption; worker crash; invalid historical response; rate limit; missing
historical interval; duplicate messages; out-of-order messages; corrected candle; queue
exhaustion.
Verify that Atlas never silently labels incomplete data as complete.
All existing Phase 1, 2A, 2B, and 2C tests must continue passing.
Do not weaken existing tests.

PART 14 — SCOPE RESTRICTIONS
Implement Deliverable 2D only.
Do not build: asset discovery; technical indicators; market-regime detection; trading
strategies; quantitative ML; LLM analysis; portfolio management; risk approvals; order
execution; real brokerage integrations; complete dashboard redesign.
Deliverable 2E will handle the market-data dashboard integration.

PART 15 — ACCEPTANCE CRITERIA
Deliverable 2D is complete only when:
1. Missing expected candle intervals can be identified.
2. Market closures are not incorrectly classified as missing data.
3. Historical backfill works through the existing Alpaca adapter.
4. Recovery jobs are tracked durably.
5. Repeated backfills do not create duplicate observations.
6. WebSocket interruptions are recorded.
7. Reconnection and restoration preserve data integrity.
8. Recoverable historical intervals are backfilled.
9. Unavailable intervals remain explicitly identified.
10. Database failures do not silently result in reported healthy ingestion.
11. Duplicate events are handled safely.
12. Out-of-order messages are handled safely.
13. Corrected candles preserve revision history.
14. Historical decision-time information is not retroactively changed.
15. Provider health and pipeline health are distinguished.
16. Recovery metrics and audit events are available.
17. All existing regression tests pass.
18. New tests pass.
19. Real-provider recovery is verified where credentials and provider capabilities permit.
Do not claim that every historical trade and quote can be recovered if the provider does not
make those observations available.

PART 16 — COMPLETION REPORT
At the end, provide: 1. Summary of reliability improvements. 2. Architecture changes. 3. Files
created. 4. Files modified. 5. Database changes. 6. Gap-detection behavior. 7. Backfill
workflow. 8. Recovery-job design. 9. Duplicate and revision handling. 10. Database interruption
behavior. 11. Metrics and health indicators. 12. Unit-test results. 13. Integration-test
results. 14. Concurrency-test results. 15. Failure-test results. 16. Real-provider recovery
results. 17. Known limitations. 18. Outstanding risks. 19. How to test an outage and recovery
locally. 20. Whether Atlas is ready for Deliverable 2E.
Be exact about what has actually been tested.
Do not claim successful recovery of data that was never retrieved.
Do not claim full data integrity if unresolved gaps remain.

FINAL INSTRUCTION: Implement Deliverable 2D only. The objective is to make Atlas's market-data
pipeline recoverable, auditable, and honest about data completeness.
