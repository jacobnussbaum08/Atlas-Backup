// Shapes of the Strategy Laboratory API (/api/v1/strategies; Phase 4A, signal states,
// checks, levels and conflicts Phase 4B).

export type StrategyFamily =
  | "MOMENTUM"
  | "TREND_FOLLOWING"
  | "MEAN_REVERSION"
  | "BREAKOUT"
  | "VOLATILITY"
  | "EVENT_DRIVEN";
export type Direction = "LONG" | "SHORT";
export type EvaluationStatus = "RUNNING" | "PROPOSED" | "NO_PROPOSAL" | "SKIPPED" | "FAILED";
export type ProposalStatus = "ACTIVE" | "EXPIRED" | "SUPERSEDED";
export type EvaluationMode = "LIVE" | "HISTORICAL";
export type KnowledgeBasis = "RECORDED" | "RECONSTRUCTED";
export type TriggerKind = "BAR_CLOSE" | "NEWS_EVENT" | "MANUAL";
export type SignalState =
  | "SIGNAL"
  | "NO_SIGNAL"
  | "INSUFFICIENT_DATA"
  | "MISSING_DATA"
  | "UNAVAILABLE_DATA"
  | "INVALID_DATA"
  | "INELIGIBLE_ASSET"
  | "DUPLICATE"
  | "FAILED";

export interface FeatureKey {
  key: string;
  optional: boolean;
}

export interface Schedule {
  triggers: TriggerKind[];
  description: string;
}

export interface CurrentVersion {
  id: string;
  version: number;
  logic_version: string;
  configuration_id: string;
  reason: string;
  created_at: string;
}

export interface StrategySummary {
  strategy_id: string;
  family: StrategyFamily;
  name: string;
  description: string;
  implemented: boolean;
  implementation_note: string;
  enabled: boolean;
  status_changed_at: string | null;
  current_version: CurrentVersion | null;
  version_count: number;
  supported_asset_classes: string[];
  supported_timeframes: string[];
  configured_timeframes: string[];
  schedule: Schedule;
  required_features: Record<string, FeatureKey[]>;
  evaluations: number;
  last_evaluation_at: string | null;
  proposals: number;
  active_proposals: number;
  parameters: Record<string, ParameterValue> | null;
  signal_states: Partial<Record<SignalState, number>>;
}

export interface CatalogResponse {
  strategies: StrategySummary[];
  feature_registry_version: string;
  sync_problems: string[];
  as_of: string;
  notes: string[];
}

export type ParameterValue = string | number | boolean | null | ParameterValue[];

export interface VersionView {
  id: string;
  strategy_id: string;
  version: number;
  logic_version: string;
  configuration_id: string;
  parameters: Record<string, ParameterValue>;
  parameters_sha256: string;
  name: string;
  description: string;
  implemented: boolean;
  supported_asset_classes: string[];
  supported_timeframes: string[];
  schedule: Schedule;
  required_features: Record<string, FeatureKey[]>;
  feature_registry_version: string;
  reason: string;
  created_by: string;
  created_at: string;
}

export interface StatusEvent {
  enabled: boolean;
  reason: string;
  actor: string;
  created_at: string;
}

export interface ParameterSchema {
  properties: Record<string, { description?: string; minimum?: number; maximum?: number; title?: string }>;
}

export interface StrategyDetail {
  strategy: StrategySummary;
  parameters: Record<string, ParameterValue> | null;
  parameters_schema: ParameterSchema;
  versions: VersionView[];
  status_history: StatusEvent[];
}

export interface SkipReason {
  feature: string;
  status: string;
  reason: string | null;
}

/** One condition a strategy evaluated; passed null = informational / not applicable. */
export interface CheckItem {
  name: string;
  passed: boolean | null;
  detail: string;
  direction: Direction | null;
  features: string[];
}

export interface Eligibility {
  known: boolean;
  provider: string;
  provider_status: string | null;
  tradable: boolean | null;
  shortable: boolean | null;
  easy_to_borrow: boolean | null;
  reference_version: number | null;
  observed_at: string | null;
  long_eligible: boolean;
  short_eligible: boolean;
}

export interface EvaluationView {
  id: string;
  strategy_id: string;
  strategy_version_id: string;
  version: number;
  logic_version: string;
  configuration_id: string;
  asset_id: string;
  symbol: string;
  timeframe: string;
  mode: EvaluationMode;
  basis: KnowledgeBasis;
  trigger: TriggerKind;
  trigger_ref: string;
  attempt: number;
  status: EvaluationStatus;
  data_cutoff: string;
  known_at: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  proposals_count: number;
  skip_reasons: SkipReason[];
  notes: string[];
  worker_id: string;
  signal_state: SignalState | null;
  signal_reason: string | null;
  checks: CheckItem[];
  eligibility: Eligibility | null;
}

export interface EvaluationsResponse {
  evaluations: EvaluationView[];
  as_of: string;
}

export interface EvidenceItem {
  feature_key: string;
  role: string;
  note: string;
  value: string | null;
  available_at: string | null;
}

export interface ProposalView {
  id: string;
  evaluation_id: string;
  strategy_id: string;
  strategy_version_id: string;
  version: number;
  asset_id: string;
  symbol: string;
  market: string;
  direction: Direction;
  timeframe: string;
  mode: EvaluationMode;
  basis: KnowledgeBasis;
  signal_at: string;
  data_cutoff: string;
  feature_snapshot_ids: string[];
  reference_entry: string;
  invalidation_level: string | null;
  target_reference: string | null;
  holding_horizon_periods: number;
  signal_strength: string;
  strength_kind: "UNCALIBRATED_SCORE";
  evidence: EvidenceItem[];
  reasoning: string;
  expires_at: string;
  created_at: string;
  status: ProposalStatus;
  evaluation_event: string;
  levels: ProposalLevels;
  conflict_count: number;
}

/** How a strategy derived its levels (empty for proposals recorded before Phase 4B). */
export interface ProposalLevels {
  entry_method?: string;
  invalidation_method?: string;
  target_method?: string | null;
  atr?: string;
  risk_per_unit?: string;
  reward_per_unit?: string | null;
  reward_risk?: string | null;
  holding_horizon_periods?: number;
  setup?: string | null;
}

export interface ProposalBrief {
  id: string;
  strategy_id: string;
  direction: Direction;
  timeframe: string;
  signal_at: string;
  expires_at: string;
  reference_entry: string;
  signal_strength: string;
  status: ProposalStatus;
}

export interface ConflictView {
  id: number;
  kind: "OPPOSING_DIRECTION";
  asset_id: string;
  symbol: string;
  mode: EvaluationMode;
  basis: KnowledgeBasis;
  overlap_start: string;
  overlap_end: string;
  detected_by: string;
  detected_at: string;
  same_strategy: boolean;
  proposals: ProposalBrief[];
}

export interface ConflictsResponse {
  conflicts: ConflictView[];
  as_of: string;
  notes: string[];
}

export interface ProposalsResponse {
  proposals: ProposalView[];
  as_of: string;
  notes: string[];
}

export interface FeatureDefinition {
  name: string;
  version: string;
  source: "BARS" | "BENCHMARK" | "MARKET_INTELLIGENCE" | "NEWS";
  description: string;
  method: string;
  parameters: { name: string; type: string; default: ParameterValue; description: string }[];
  timeframes: string[];
  required_data: string[];
  availability: string;
  value_kind: "NUMBER" | "LABEL";
  status: string;
  window_rule: string | null;
  definition_sha256: string;
  registered_at: string | null;
}

export interface FeatureRegistryResponse {
  registry_version: string;
  definitions: FeatureDefinition[];
  notes: string[];
}

export interface EvaluationQuery {
  strategy_id?: string | undefined;
  symbol?: string | undefined;
  status?: EvaluationStatus | undefined;
  signal_state?: SignalState | undefined;
  limit?: number | undefined;
}

export interface ProposalQuery {
  strategy_id?: string | undefined;
  symbol?: string | undefined;
  status?: ProposalStatus | undefined;
  direction?: Direction | undefined;
  limit?: number | undefined;
}

export interface ConflictQuery {
  strategy_id?: string | undefined;
  symbol?: string | undefined;
  limit?: number | undefined;
}

export const FAMILY_LABELS: Record<StrategyFamily, string> = {
  MOMENTUM: "Momentum",
  TREND_FOLLOWING: "Trend following",
  MEAN_REVERSION: "Mean reversion",
  BREAKOUT: "Breakout",
  VOLATILITY: "Volatility",
  EVENT_DRIVEN: "Event driven",
};

export const SIGNAL_STATE_LABELS: Record<SignalState, string> = {
  SIGNAL: "Signal",
  NO_SIGNAL: "No signal",
  INSUFFICIENT_DATA: "Insufficient data",
  MISSING_DATA: "Missing data",
  UNAVAILABLE_DATA: "Unavailable data",
  INVALID_DATA: "Invalid data",
  INELIGIBLE_ASSET: "Ineligible asset",
  DUPLICATE: "Duplicate",
  FAILED: "Failed",
};
