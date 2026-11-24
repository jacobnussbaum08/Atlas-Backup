/** Shapes of the Phase 6B /api/v1/risk responses. Decimals are strings, never floats. */

export type AssessmentResult = "PASS_FOR_RESERVATION" | "REJECTED" | "INSUFFICIENT_DATA" | "RISK_HALTED";
export type BreakerStatus = "CLEAR" | "TRIPPED";

export interface BreakerView {
  breaker: string;
  status: BreakerStatus;
  recovery: "OPERATOR_RESET" | "AUTO_WHEN_CLEAR";
  since: string | null;
  last_event: "TRIPPED" | "RESET" | null;
  actor: string | null;
  reason: string | null;
  events: number;
}

export interface BreakerEvent {
  id: number;
  breaker: string;
  sequence: number;
  event: "TRIPPED" | "RESET";
  previous_status: BreakerStatus;
  new_status: BreakerStatus;
  occurred_at: string;
  actor_kind: "OPERATOR" | "SYSTEM";
  actor: string;
  reason: string;
  measurements: Record<string, unknown>;
  response: Record<string, string>;
  config_version_id: string | null;
  exposure_snapshot_id: string | null;
}

export interface ConfigVersion {
  id: string;
  version: number;
  change_kind: string;
  previous_version_id: string | null;
  hard_limits: Record<string, unknown>;
  preferences: Record<string, unknown>;
  hard_limits_sha256: string;
  preferences_sha256: string;
  changes: { section: string; field: string; previous: unknown; new: unknown }[];
  actor: string;
  reason: string;
  created_at: string;
}

export interface Limits {
  account_id: string;
  configured: boolean;
  message: string | null;
  active: ConfigVersion | null;
  note: string;
  governance: string;
  breaker_recovery: Record<string, string>;
  breaker_response: Record<string, string>;
  timezone_policy: string;
}

export interface ExposureGroup {
  long: string;
  short: string;
  gross: string;
  net: string;
  of_equity: string | null;
  asset_class?: string;
  sector?: string;
  cluster?: string;
}

export interface ExposureAsset {
  asset_id: string;
  symbol: string;
  asset_class: string;
  direction: "LONG" | "SHORT";
  quantity: string;
  exposure: string;
  of_equity: string | null;
  margin_required: string;
  sector: string | null;
  cluster: string | null;
  price: string | null;
  price_status: string;
  price_source: string | null;
  price_observed_at: string | null;
}

export interface Baseline {
  period: string;
  period_start: string;
  equity: string;
  basis: string;
  observed_at: string;
  source: string;
}

export interface Condition {
  breaker: string;
  breached: boolean | null;
  message: string;
  measured: string | null;
  limit: string | null;
  [key: string]: unknown;
}

export interface LiveState {
  as_of: string;
  recorded: false;
  mark_status: "COMPLETE" | "STALE" | "INCOMPLETE";
  mark_issues: { kind: string; symbol?: string; currency?: string; detail?: string | null }[];
  ledger_sequence: number;
  functional_currency: string;
  equity: string;
  contributed_capital: string;
  cash: string;
  available_capital: string;
  long_exposure: string;
  short_exposure: string;
  gross_exposure: string;
  net_exposure: string;
  margin_used: string;
  pending_exposure: string;
  pending_source: string;
  of_equity: Record<string, string | null>;
  open_positions: number;
  daily_loss: string | null;
  daily_baseline: string | null;
  weekly_loss: string | null;
  weekly_baseline: string | null;
  peak_equity: string | null;
  drawdown: string | null;
  available_risk_budget: string | null;
  baselines: Record<string, Baseline | null>;
  reconciliation: { status: string | null; current: boolean; run_id: string | null };
  conditions: Condition[];
  breakdown: {
    assets: ExposureAsset[];
    asset_classes: ExposureGroup[];
    sectors: ExposureGroup[];
    clusters: ExposureGroup[];
  };
}

export interface RiskOverview {
  account: { id: string; name: string; purpose: string; base_currency: string };
  trading_halted: boolean;
  active_breakers: string[];
  kill_switch: BreakerStatus;
  breakers: BreakerView[];
  limits: Limits;
  live: LiveState | null;
  latest_snapshot: Record<string, unknown> | null;
  assessment_counts: Record<AssessmentResult, number>;
  reserved_exposure: { amount: string; note: string };
  disclaimer: string;
}

export interface AssessmentSummary {
  id: string;
  assessed_at: string;
  valid_until: string;
  result: AssessmentResult;
  source: string;
  strategy_proposal_id: string | null;
  ai_decision_id: string | null;
  asset_id: string;
  symbol: string;
  asset_class: string;
  direction: "LONG" | "SHORT";
  entry: string;
  stop: string | null;
  target: string | null;
  requested_quantity: string | null;
  permitted_quantity: string | null;
  functional_currency: string;
  equity: string;
  risk_budget: string | null;
  potential_loss: string | null;
  notional: string | null;
  estimated_costs: string | null;
  binding_constraint: string | null;
  config_version_id: string | null;
  ledger_sequence: number;
  requested_by: string;
  engine_version: string;
  reasons: string[];
}

export interface AssessmentCheck {
  ordinal: number;
  check: string;
  outcome: "PASS" | "FAIL" | "UNKNOWN" | "HALT" | "RESTRICT" | "INFO";
  reason_code: string | null;
  message: string;
  measured: string | null;
  limit: string | null;
  unit: string | null;
  details: Record<string, unknown>;
}

export interface AssessmentDetail extends Omit<AssessmentSummary, "reasons"> {
  checks: AssessmentCheck[];
  calculations: Record<string, unknown>;
  exposure_after: Record<string, unknown>;
  inputs_sha256: string;
  content_sha256: string;
  note: string;
}

export interface KillSwitchResult {
  account_id: string;
  engaged: boolean;
  already_engaged: boolean;
  release: string;
}
