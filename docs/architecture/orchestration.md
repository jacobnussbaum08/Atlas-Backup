# Autonomous paper-trading orchestration (Phase 10A)

**Paper trading only.** The orchestrator connects the existing engines into one automated
lifecycle. It adds no trading logic and has no execution path of its own: every order is a
simulated paper order of the Phase 7A engine, filled by the isolated paper broker (7B) from
stored market data. Decisions D-204 to D-212.

## Lifecycle

```
strategies (4A/4B) ──proposals──▶ AI coordinator (5B) ──PRIORITIZE_FOR_REVIEW decisions──▶
   trade_intents (inbox, UNIQUE per account+decision) + outbox event      [only when ENABLED]
        │  delivery (one transaction, account lock, idempotency keys)
        ▼
   mode still ENABLED? ── no ─▶ NOT_TRADED
   decision expired?   ── yes ▶ NOT_TRADED
   6C reserve_in (risk engine decides) ── not APPROVED ─▶ RISK_REJECTED (no-trade decision)
   7A submit_in ENTRY (LIMIT at the reserved worst entry price)
        ├─ refused ─▶ release reservation (NOT_SUBMITTED) ─▶ EXECUTION_REFUSED
        └─ created ─▶ ORDER_CREATED
        ▼
paper execution worker: outbox ─▶ paper broker ─▶ simulated fills ─▶ 6A ledger, reservation consumed
position monitor: marks, stops, targets, exits (simulated paper orders) ─▶ 6A ledger
```

## One tick

| Step | Mode | Effect |
|---|---|---|
| heartbeat | any | `orchestrator_status` (also renewed every min(tick, 30 s) by a separate task) |
| expire reservations | any | 6C expiry (never one an order depends on) |
| strategy stage | any | one evaluation cycle: proposals only |
| decision stage | any | a LIVE coordinator run when LIVE proposals arrived since the last LIVE run, at most every `min_decision_interval_seconds` |
| intake | ENABLED, entry session | prioritized decisions of COMPLETED LIVE runs, recent (lookback) and unexpired, become intents + outbox events |
| delivery | any | the intent handler (above); a non-ENABLED mode records NOT_TRADED |
| sweep | any | finishes RUNNING jobs of past slots whose lease expired |
| scheduled jobs | any | see below |

Each step is isolated: a failing step is recorded (tick report, status DEGRADED, `last_error`
redacted) and the next tick retries. Running the strategy and decision stages while trading is
disabled is shadow operation: proposals and decisions are recorded, nothing is reserved or
ordered.

## Operator control

| Action | From | To | Where |
|---|---|---|---|
| ENABLE | DISABLED | ENABLED | CLI only |
| PAUSE | ENABLED | PAUSED | CLI, API |
| RESUME | PAUSED | ENABLED | CLI only |
| DISABLE | ENABLED, PAUSED | DISABLED | CLI, API |
| EMERGENCY_STOP | any | EMERGENCY_STOP | CLI, API |
| CLEAR_EMERGENCY | EMERGENCY_STOP | DISABLED | CLI only |

No row means DISABLED. Every change is a numbered, append-only `trading_control_events` row
(trigger-checked sequence and starting mode; operators only; reason required) plus an audit
log entry. An emergency stop records the mode, engages the 6B kill switch and requests
cancellation of every working ENTRY order (idempotency key per order and stop). Exit orders and
protective orders are never cancelled and new exits remain possible: halting entries never
prevents reducing risk. Clearing an emergency does not release the kill switch.

## Scheduled jobs

One `orchestration_jobs` row per (kind, slot), claimed under a lease:

| Job | Slot | Body |
|---|---|---|
| RISK_MONITOR | every 300 s | `RiskService.monitor` (records state, trips/recovers breakers) |
| RESERVATION_RECONCILE | every 900 s | 6C reconciler |
| EXECUTION_RECONCILE | every 1800 s | 7A reconciler (writes its own CRITICAL event on findings) |
| ACCOUNTING_RECONCILE | every 3600 s | 6A ledger reconciliation |
| WORKER_HEALTH | every 300 s | stale or failed workers ─▶ `WORKER_UNHEALTHY` (once per worker per hour) |
| MARKET_INTELLIGENCE | trading day, close + 65 min | a LIVE intelligence run |
| DECISION_OUTCOMES | trading day, close + 65 min | decision evaluation against baselines |

States: RUNNING (leased) ─▶ SUCCEEDED, or FAILED (retry with back-off 30 s, 60 s, … ≤ 15 min)
─▶ DEAD after the maximum attempts (HIGH event). A lease that expires is a stuck job: reclaimed
(WARNING event) in its own slot, or finished FAILED by the sweep for a past slot. A failed
reconciliation is a finding: the job succeeds with `passed: false` and a HIGH/CRITICAL event.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| crash during delivery | the transaction rolls back (no reservation, no order); the intent stays RECEIVED with its event PENDING; the retry uses the same keys |
| crash between intake and delivery | the next tick (any orchestrator) delivers it |
| delivery fails every attempt | intent DEAD, nothing reserved or ordered, HIGH event |
| two orchestrators, or a duplicate event | UNIQUE intents, idempotency keys and row locks: one reservation and one order per decision |
| database unreachable | the tick fails and is counted; the worker keeps running and retries; the heartbeat goes stale, which the dashboard reports |
| a stage fails (e.g. the coordinator) | recorded; other stages and trading work continue |
| worker stopped | SIGTERM finishes the step in hand, records STOPPED; restart resumes from the database |
| trading paused/stopped with intents pending | they resolve as NOT_TRADED |

Redis plays no part; PostgreSQL holds every state.

## Interfaces

* Worker: `python -m app.workers.trading_orchestrator [--once] [--duration S]` (off unless
  `ORCHESTRATOR_WORKER_ENABLED=true`); Compose service `orchestrator-worker`.
* CLI: `python -m app.orchestration.cli status|history|intents|jobs|enable|pause|resume|disable|emergency-stop|clear-emergency`.
* API: `GET /api/v1/orchestration/{status,controls/history,intents,intents/{id},jobs}`;
  `POST /api/v1/orchestration/controls/{pause,disable,emergency-stop}` (operator token).
* Dashboard: **Autonomous Trading** (`/autonomy`); the orchestrator also appears under System
  Health ▸ Background workers.

## Known limits (Phase 10A)

* One account per orchestrator process (`ORCHESTRATOR_ACCOUNT_NAME`, default the primary).
* Slots missed during a long outage are not back-filled; the next slot runs.
* The orchestrator relies on the paper execution worker and the position monitor running; it
  reports their staleness but cannot start them.
* Operator identity on the API is the shared token (`operator:api`); named operators use the CLI.
