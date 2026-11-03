// Shapes of the Phase 4A Strategy Laboratory API (/api/v1/strategies).

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

export interface EvaluationView {
  id: string;
  strategy_id: string;
  strategy_version_id: string;
  version: number;
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
}

export interface ProposalsResponse {
  proposals: ProposalView[];
  as_of: string;
  notes: string[];
}

export interface FeatureDefinition {
  name: string;
  version: string;
  source: "BARS" | "MARKET_INTELLIGENCE" | "NEWS";
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
  limit?: number | undefined;
}

export interface ProposalQuery {
  strategy_id?: string | undefined;
  symbol?: string | undefined;
  status?: ProposalStatus | undefined;
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
