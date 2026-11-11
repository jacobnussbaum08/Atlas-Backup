// Phase 9 test fixtures shaped like real API responses (TEST DATA, not real system state).
import type { WorkersReport, WorkerStatus } from "../types/system";

const NOT_INITIALIZED = {
  status: 404,
  body: {
    error: {
      code: "not_found",
      message: "no primary paper account exists yet: run `python -m app.accounting.cli init` to create it",
    },
  },
};

function worker(overrides: Partial<WorkerStatus> & Pick<WorkerStatus, "worker" | "title">): WorkerStatus {
  return {
    module: `app.workers.${overrides.worker}`,
    purpose: "fixture worker",
    observation: "HEARTBEAT",
    source: "fixture_table",
    health: "RUNNING",
    detail: "Heartbeat current; recorded state SUBSCRIBED.",
    enabled_in_api_env: true,
    enabled_reported: true,
    state: "SUBSCRIBED",
    last_heartbeat_at: "2026-03-10T14:59:55Z",
    heartbeat_age_seconds: 5,
    stale_after_seconds: 180,
    last_activity_at: null,
    last_activity: null,
    last_error: null,
    last_error_at: null,
    ...overrides,
  };
}

export const WORKERS_FIXTURE: WorkersReport = {
  checked_at: "2026-03-10T15:00:00Z",
  summary: { RUNNING: 1, STALE: 1, NO_HEARTBEAT: 1 },
  workers: [
    worker({ worker: "stream-worker", title: "Market-data stream" }),
    worker({
      worker: "recovery-worker",
      title: "Market-data recovery",
      health: "STALE",
      state: "IDLE",
      detail: "No heartbeat for 10800 s (threshold 120 s): the process is probably not running.",
      last_heartbeat_at: "2026-03-10T12:00:00Z",
      heartbeat_age_seconds: 10800,
      last_error: "provider timeout (fixture)",
      last_error_at: "2026-03-10T11:59:00Z",
    }),
    worker({
      worker: "strategy-worker",
      title: "Strategy evaluation",
      observation: "ACTIVITY",
      health: "NO_HEARTBEAT",
      state: "COMPLETED",
      enabled_in_api_env: true,
      enabled_reported: null,
      last_heartbeat_at: null,
      heartbeat_age_seconds: null,
      stale_after_seconds: null,
      last_activity_at: "2026-03-10T14:30:00Z",
      last_activity: "momentum evaluation COMPLETED, 0 proposal(s)",
    }),
  ],
  notes: ["Workers are separate processes (fixture note)."],
};

/** Endpoints the Phase 9 panels read, answered as an uninitialized system would. */
export const PHASE9_ROUTES = {
  "/api/v1/risk/state": NOT_INITIALIZED,
  "/api/v1/risk/limits": NOT_INITIALIZED,
  "/api/v1/portfolio/overview": NOT_INITIALIZED,
  "/api/v1/portfolio/snapshots": NOT_INITIALIZED,
  "/api/v1/system/workers": { body: WORKERS_FIXTURE },
  "/api/v1/paper-execution/models": { body: [] },
  "/api/v1/validation/runs": { body: [] },
  "/api/v1/intelligence/market": {
    body: { available: false, run: null, as_of: null, benchmark_symbol: null, classifications: [], participation: null, notes: ["No run recorded (fixture)."] },
  },
  "/api/v1/assets/watchlist": { body: { items: [], total: 0, limit: 50, offset: 0, notes: [] } },
  "/api/v1/news/providers": { body: [] },
  "/api/v1/market-data/recovery-jobs": { body: [] },
};
