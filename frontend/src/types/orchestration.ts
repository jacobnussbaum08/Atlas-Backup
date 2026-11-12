/** Phase 10A orchestration payloads (shapes of /api/v1/orchestration responses). */

export type TradingMode = "DISABLED" | "ENABLED" | "PAUSED" | "EMERGENCY_STOP";

export type IntentStatus =
  | "RECEIVED"
  | "ORDER_CREATED"
  | "RISK_REJECTED"
  | "EXECUTION_REFUSED"
  | "NOT_TRADED"
  | "INVALID"
  | "DEAD";

export type JobStatus = "RUNNING" | "SUCCEEDED" | "FAILED" | "DEAD" | "SUPERSEDED";

/** Phase 10B: who manages open positions, and whether new entries may be taken in. */
export interface Protection {
  at: string;
  gate_open: boolean;
  reasons: { code: string; message: string }[];
  position_monitor: { alive: boolean; detail: string };
  paper_execution: { alive: boolean; state: string | null; detail: string };
  open_positions: number;
  working_orders: number;
  pending_execution_events: number;
}

/** Phase 10B: an operational alert (a system health event) with the action to take. */
export interface OpsAlert {
  id: number;
  component: string;
  event_type: string;
  severity: "INFO" | "WARNING" | "HIGH" | "CRITICAL";
  summary: string;
  subject_kind: string | null;
  subject_id: string | null;
  occurred_at: string;
  action: string | null;
  acknowledged: boolean;
  acknowledgements: { actor: string; note: string; at: string }[];
}

export interface ControlState {
  account_id: string;
  mode: TradingMode;
  entries_allowed: boolean;
  sequence: number;
  since: string | null;
  actor: string | null;
  reason: string | null;
  /** No operator ever changed it: the DISABLED default. */
  default: boolean;
}

export interface OrchestratorStatus {
  health: string;
  detail: string;
  worker_enabled_in_api_env: boolean | null;
  stale_after_seconds: number;
  worker_id: string | null;
  state: string | null;
  trading_mode_seen: TradingMode | null;
  heartbeat_at: string | null;
  heartbeat_age_seconds: number | null;
  started_at: string | null;
  stopped_at: string | null;
  last_tick_at: string | null;
  last_tick_ms: number | null;
  counters: Record<string, number>;
  last_error: string | null;
  last_error_at: string | null;
}

export interface OrchestrationStatus {
  account_id: string;
  account_name: string;
  simulated: true;
  control: ControlState;
  orchestrator: OrchestratorStatus;
  intents: Record<IntentStatus, number>;
  jobs: Record<JobStatus, number>;
  outbox: { counts: Record<string, number>; oldest_pending_at: string | null };
  notes: string[];
}

export interface ControlEvent {
  sequence: number;
  action: string;
  from_mode: TradingMode;
  to_mode: TradingMode;
  actor: string;
  reason: string;
  occurred_at: string;
  details: Record<string, unknown>;
}

export interface TradeIntent {
  id: string;
  account_id: string;
  ai_decision_id: string;
  decision_run_id: string;
  asset_id: string | null;
  symbol: string | null;
  direction: string | null;
  status: IntentStatus;
  trading_mode: TradingMode | null;
  reservation_id: string | null;
  reservation_decision: string | null;
  order_id: string | null;
  execution_request_id: string | null;
  reasons: { code?: string; message?: string }[];
  error: string | null;
  decision_expires_at: string | null;
  received_at: string | null;
  decided_at: string | null;
}

export interface OrchestrationJob {
  id: number;
  kind: string;
  slot: string;
  status: JobStatus;
  attempts: number;
  reclaimed: number;
  lease_owner: string | null;
  lease_expires_at: string | null;
  next_attempt_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  result: Record<string, unknown>;
  error: string | null;
}

export interface ControlResult {
  account_id: string;
  action: string;
  changed: boolean;
  control: ControlState;
}

export interface EmergencyResult extends ControlResult {
  kill_switch_engaged_now: boolean;
  cancel_requested: string[];
  cancel_failed: { order_id: string; error: string }[];
  clear: string;
}
