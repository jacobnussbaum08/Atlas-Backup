// Mirrors backend/app/monitoring/workers.py (GET /api/v1/system/workers, Phase 9).

export type WorkerObservation = "HEARTBEAT" | "ACTIVITY";

export type WorkerHealth =
  | "RUNNING"
  | "DEGRADED"
  | "STALE"
  | "STOPPED"
  | "FAILED"
  | "DISABLED"
  | "NOT_REPORTED"
  | "NO_HEARTBEAT";

export interface WorkerStatus {
  worker: string;
  title: string;
  module: string;
  purpose: string;
  observation: WorkerObservation;
  source: string;
  health: WorkerHealth;
  detail: string;
  enabled_in_api_env: boolean | null;
  enabled_reported: boolean | null;
  state: string | null;
  last_heartbeat_at: string | null;
  heartbeat_age_seconds: number | null;
  stale_after_seconds: number | null;
  last_activity_at: string | null;
  last_activity: string | null;
  last_error: string | null;
  last_error_at: string | null;
}

export interface WorkersReport {
  checked_at: string;
  summary: Partial<Record<WorkerHealth, number>>;
  workers: WorkerStatus[];
  notes: string[];
}
