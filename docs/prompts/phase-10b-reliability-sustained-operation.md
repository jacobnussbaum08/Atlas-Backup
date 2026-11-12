Atlas AI Trader — Phase 10B: Reliability & Sustained Operation
Read `ATLAS_MASTER_SPEC_V2.md` and the Phase 10A completion report first. Inspect the actual codebase before making changes.
Preflight

* Confirm current branch is `main` and inspect working-tree status.
* Verify Phase 10A implementation and its tests.
* Identify remaining reliability gaps from the integration audit.
* Preserve all existing risk, accounting, execution, and paper-trading invariants.
* Stop and report before implementation if any P0/P1 financial-integrity or execution-safety issue exists.

Objective
Make Atlas's automated paper-trading system reliable during prolonged operation, interruptions, and recovery.
Implement

1. Scheduled operations
   * Durable scheduling for risk monitoring, position monitoring, expired reservation cleanup, reconciliation, and market-session tasks.
   * Ensure missed schedules recover after restart.
   * Prevent overlapping duplicate jobs.
2. Worker reliability
   * Health monitoring, heartbeats, graceful shutdown, restart recovery, stuck-job detection, and dead-letter handling.
   * Durable PostgreSQL-backed job/event state; Redis must not be the sole source of truth.
   * Idempotent retries and transactional event handling.
3. Risk and position protection
   * Ensure monitoring failures cannot silently leave positions unmanaged.
   * Define and test safe behavior when price feeds or monitoring workers become unavailable.
   * Preserve existing stop-loss and take-profit rules.
   * Improve conservative gap-through-stop fill simulation without pretending fills are guaranteed at stop prices.
   * Entry breakers must not prevent necessary risk-reducing exits.
4. Operations and security
   * Secure operator authentication and authorization.
   * Require explicit configuration for non-development environments.
   * Keep autonomous paper trading disabled by default until an authorized operator enables it.
   * Add safe pause/resume and emergency kill-switch behavior.
   * Never expose secrets in logs or API responses.
5. Monitoring and data quality
   * Automate shadow/degradation monitoring where appropriate.
   * Detect stale feeds, missing data, failed reconciliation, and abnormal worker behavior.
   * Review the historical-data limitation identified in the audit (88 tradeable sessions). Expand coverage where provider access and licensing permit; document remaining limitations.
6. Validation
   * Review whether backtests appropriately represent the AI coordinator's role. Fix inconsistencies or document and justify the architecture.
   * Add operational metrics and actionable alerts.
   * Add a sustained-operation/soak test.

Required tests
Use disposable test accounts and databases. Test:

* Worker crashes and restarts.
* Database and Redis interruptions.
* Duplicate and delayed events.
* Missed schedules and overlapping jobs.
* Expired reservations and stuck jobs.
* Monitoring and price-feed outages.
* Stop-loss gaps and position exits.
* Risk breakers and emergency kill switch.
* Reconciliation and ledger integrity after recovery.
* Extended operation without duplicate orders, fills, reservations, or ledger entries.

Run the full backend and frontend regression suites.
Strict restrictions

* Paper trading only.
* No live brokerage connections or live orders.
* Do not change risk limits automatically.
* Do not weaken risk checks to make tests pass.
* Do not alter existing paper balances or production data.
* Do not enable autonomous trading by default.
* Do not perform destructive migrations or database resets.

Completion report
Report:

1. Reliability improvements implemented.
2. Scheduling and recovery architecture.
3. Failure scenarios tested and results.
4. Security and operator-control changes.
5. Database migrations and configuration changes.
6. Full test results.
7. Remaining risks and limitations.
8. Whether Atlas is ready for Phase 11A extended paper-trading validation.

Work only within Phase 10B. Do not begin Phase 11A automatically.
