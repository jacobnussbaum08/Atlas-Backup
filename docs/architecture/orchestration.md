# Autonomous paper-trading orchestration (Phases 10A-10B)

**Paper trading only.** The orchestrator connects the existing engines into one automated
lifecycle. It adds no trading logic and has no execution path of its own: every order is a
simulated paper order of the Phase 7A engine, filled by the isolated paper broker (7B) from
stored market data. Decisions D-204 to D-212 (10A) and D-213 to D-221 (10B).

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
| protection (10B) | any | assess the position monitor and the paper execution worker; alert; backstop them; open or close the entry gate (below) |
| strategy stage | any | one evaluation cycle: proposals only |
| decision stage | any | a LIVE coordinator run when LIVE proposals arrived since the last LIVE run, at most every `min_decision_interval_seconds` |
| intake | ENABLED, entry session, gate open | prioritized decisions of COMPLETED LIVE runs, recent (lookback) and unexpired, become intents + outbox events |
| delivery | any | the intent handler (above); a non-ENABLED mode or a closed gate records NOT_TRADED |
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
| DATA_FRESHNESS (10B) | every 300 s, regular session | open positions without a fresh price ─▶ `FEED_STALE` |
| SESSION_OPEN_CHECK (10B) | trading day, open − 30 min … open + 60 min | readiness checklist ─▶ `SESSION_NOT_READY` |
| MARKET_INTELLIGENCE | trading day, once its daily bars are final: midnight New York + 15 min (D-230) | a LIVE intelligence run |
| DECISION_OUTCOMES | as MARKET_INTELLIGENCE | decision evaluation against baselines |
| DEGRADATION_MONITOR (10B) | as MARKET_INTELLIGENCE | the 8B degradation pass (warns; suspends only under an approved rule) |
| SESSION_CLOSE_REVIEW (10B) | trading day, close + 15 min | the day's operational summary (intents, orders, fills, positions, alerts, jobs) |

States: RUNNING (leased) ─▶ SUCCEEDED, or FAILED (retry with back-off 30 s, 60 s, … ≤ 15 min)
─▶ DEAD after the maximum attempts (HIGH event; a dead letter an operator may requeue). A lease
that expires is a stuck job: reclaimed (WARNING event) in its own slot, or finished FAILED by
the sweep for a past slot. A failed reconciliation is a finding: the job succeeds with
`passed: false` and a HIGH/CRITICAL event.

**10B scheduling rules** (D-215, D-216): a kind never runs in two slots at once; a running
job's lease is renewed every lease/3; after a restart daily jobs catch up the last
`ORCHESTRATOR_CATCH_UP_DAYS` trading days (the session review per day, the others once for the
newest missed day, the older days SUPERSEDED); interval jobs are not back-filled, and the first
slot after a gap reports the missed slots (`ORCHESTRATION_SCHEDULE_GAP`); a FAILED interval job
of a slot that will never be offered again is SUPERSEDED.

## Position protection (Phase 10B)

```
position monitor ── worker_heartbeats (every pass) ──┐
paper execution  ── paper_broker_state heartbeat ────┴─▶ protection assessment (each tick)
                                                          ├─ both running: entry gate OPEN
                                                          └─ either not: gate CLOSED (no intake;
                                                             deliveries resolve NOT_TRADED)
monitor down + something open ──▶ the orchestrator runs the monitor's pass (backstop)
execution crashed (RUNNING, stale) + work waiting ──▶ one delivery + simulation + expiry pass
execution stopped cleanly ──▶ CRITICAL alert only (an operator's stop is never overridden)
```

The backstops never write the workers' heartbeats, so an outage stays visible
(`POSITION_MONITOR_DOWN`, `PAPER_EXECUTION_DOWN`, CRITICAL while anything is open). They run the
same code with the same idempotency keys and locks as the workers, so a worker that returns
mid-outage cannot double an exit. Exits are never blocked: not by the gate, not by an
emergency stop, not by any circuit breaker.

**Price feeds.** A stale feed leaves positions at their last mark, flagged STALE; the
DATA_QUALITY breaker halts entries; DATA_FRESHNESS raises `FEED_STALE`. No exit is executed at
a stale or invented price. Observations from the outage that arrive later (stream or backfill)
are evaluated against the protective levels, and a gap is priced by the execution model's gap
rule (version 2, D-217).

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
| position monitor crashed or hung (10B) | gate closed, CRITICAL alert, the orchestrator runs protective passes |
| paper execution worker crashed (10B) | gate closed, CRITICAL alert, the orchestrator delivers and simulates |
| database connections terminated (10B) | the failing step or tick is recorded; the next one reconnects; nothing duplicated (tested with `pg_terminate_backend`) |
| orchestrator down for days (10B) | daily jobs caught up, interval gaps reported |

Redis plays no part; PostgreSQL holds every state (tested: orchestration runs with every
Redis command failing).

## Operators (Phase 10B)

Named credentials (`operator_credentials`, D-218): issued and revoked from the CLI (the token is
written to a new file, never printed; only its SHA-256 digest is stored). The API's controls
record `operator:<name>`; only credentials issued with trading control may use them; in the
paper environment the shared token is refused for them. Alerts carry their action and can be
acknowledged (append-only). Reads: `/orchestration/protection`, `/alerts`, `/metrics`.

## Interfaces

* Worker: `python -m app.workers.trading_orchestrator [--once] [--duration S]` (off unless
  `ORCHESTRATOR_WORKER_ENABLED=true`); Compose service `orchestrator-worker`.
* CLI: `python -m app.orchestration.cli status|history|intents|jobs|enable|pause|resume|disable|emergency-stop|clear-emergency`.
* API: `GET /api/v1/orchestration/{status,controls/history,intents,intents/{id},jobs}`;
  `POST /api/v1/orchestration/controls/{pause,disable,emergency-stop}` (operator token).
* Dashboard: **Autonomous Trading** (`/autonomy`); the orchestrator also appears under System
  Health ▸ Background workers.

## Known limits

* One account per orchestrator process (`ORCHESTRATOR_ACCOUNT_NAME`, default the primary;
  explicit in the paper environment).
* Interval slots missed during an outage are reported, not back-filled (by design, D-216).
* The orchestrator backstops the paper execution worker and the position monitor but cannot
  restart their processes; a hung process is detected by its heartbeat, not killed.
* If the orchestrator itself is down, nothing backstops the other workers: their own restart
  policy and the dashboard's worker report are the protection.
* API reads are not authenticated (the API binds to 127.0.0.1 by default); writes are.
