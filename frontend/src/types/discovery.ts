// Shapes of the /api/v1/assets, /api/v1/discovery and /api/v1/market-sessions responses
// (Phase 3A). Decimal values arrive as strings and are never converted for arithmetic.

export type UniverseLayer = "DISCOVERY_POOL" | "QUALIFIED_UNIVERSE" | "ACTIVE_WATCHLIST";
export type AssetStatus = "ACTIVE" | "INACTIVE" | "DELISTED";
export type InstrumentType = "STOCK" | "ETF" | "UNCLASSIFIED";
export type QualificationOutcome = "QUALIFIED" | "DISQUALIFIED" | "INSUFFICIENT_DATA";
export type RuleStatus = "PASS" | "FAIL" | "UNAVAILABLE" | "NOT_APPLICABLE";
export type DiscoveryJobKind =
  | "DIRECTORY_DISCOVERY"
  | "METADATA_REFRESH"
  | "QUALIFICATION"
  | "WATCHLIST_SCORING"
  | "MEMBERSHIP_UPDATE";
export type DiscoveryJobStatus = "PENDING" | "RUNNING" | "COMPLETED" | "PARTIALLY_COMPLETED" | "FAILED";
export type SortKey = "symbol" | "name" | "exchange" | "status" | "first_discovered_at" | "last_verified_at" | "rank" | "score";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  notes: string[];
}

export interface AssetSummary {
  asset_id: string;
  symbol: string;
  name: string | null;
  asset_class: string;
  instrument_type: InstrumentType;
  exchange: string | null;
  provider_exchange: string | null;
  status: AssetStatus;
  provider_asset_id: string | null;
  provider_symbol: string | null;
  tradable: boolean | null;
  shortable: boolean | null;
  easy_to_borrow: boolean | null;
  marginable: boolean | null;
  fractionable: boolean | null;
  attributes: string[];
  first_discovered_at: string | null;
  last_verified_at: string | null;
  in_discovery_pool: boolean;
  qualified: boolean;
  qualified_since: string | null;
  on_watchlist: boolean;
  qualification_outcome: QualificationOutcome | null;
  failed_rules: string[];
  unavailable_rules: string[];
  watchlist_rank: number | null;
  watchlist_score: string | null;
}

export interface ReferenceVersion {
  id: number;
  version: number;
  observed_at: string;
  source: string;
  discovery_job_id: string | null;
  symbol: string;
  provider_symbol: string;
  name: string | null;
  provider_asset_class: string;
  provider_exchange: string | null;
  exchange_mic: string | null;
  status: "ACTIVE" | "INACTIVE";
  tradable: boolean | null;
  marginable: boolean | null;
  shortable: boolean | null;
  easy_to_borrow: boolean | null;
  borrow_status: string | null;
  fractionable: boolean | null;
  margin_requirement_long: string | null;
  margin_requirement_short: string | null;
  min_order_size: string | null;
  min_trade_increment: string | null;
  price_increment: string | null;
  attributes: string[];
  delisting_evidence: string | null;
  content_sha256: string;
}

export interface ProviderIdentity {
  provider: string;
  provider_asset_id: string;
  provider_asset_class: string;
  provider_status: "ACTIVE" | "INACTIVE";
  current_symbol: string;
  current_version: number;
  first_seen_at: string;
  last_seen_at: string;
  missing_since: string | null;
}

export interface SymbolMapping {
  provider: string;
  provider_symbol: string;
  provider_exchange: string | null;
  status: string;
  valid_from: string;
  valid_to: string | null;
  valid_from_basis: "OPERATOR_REFERENCE" | "PROVIDER_HISTORY_ASSUMED" | "OBSERVED";
}

export interface LayerState {
  layer: UniverseLayer;
  member: boolean;
  since: string | null;
  reason_code: string | null;
  reason: string | null;
}

export interface RuleResult {
  rule: string;
  category: string;
  mandatory: boolean;
  status: RuleStatus;
  message: string;
  value: unknown;
  threshold: unknown;
}

export interface Evaluation {
  evaluation_id: string;
  job_id: string;
  as_of: string;
  outcome: QualificationOutcome;
  rule_set_id: string;
  rule_set_code: string | null;
  rule_set_version: number | null;
  failed_rules: string[];
  unavailable_rules: string[];
  rule_results: RuleResult[];
  short_eligibility: Record<string, unknown>;
  reference_version_id: number | null;
}

export interface Metric {
  metric: string;
  window: string;
  timeframe: string;
  provider: string;
  feed: string;
  feature_version: string;
  as_of: string;
  source_start: string | null;
  source_end: string | null;
  observations: number;
  expected_observations: number;
  value: string | null;
  status: "OK" | "INSUFFICIENT_DATA" | "NO_DATA";
  details: Record<string, unknown>;
}

export interface FactorScore {
  raw: number | null;
  score: number | null;
  weight: string;
  available: boolean;
  explanation: string;
}

export interface WatchlistScore {
  job_id: string;
  asset_id: string;
  symbol: string | null;
  as_of: string;
  rank: number | null;
  overall_score: string | null;
  weight_coverage: string;
  selected: boolean;
  factor_scores: Record<string, FactorScore>;
  explanation: string;
  scoring_version_id: string;
}

export interface AssetDetail {
  asset: AssetSummary;
  classification_source: string;
  country: string | null;
  quote_currency: string;
  metadata_source: string | null;
  providers: ProviderIdentity[];
  reference: ReferenceVersion | null;
  reference_versions: number;
  mappings: SymbolMapping[];
  layers: LayerState[];
  latest_evaluation: Evaluation | null;
  latest_score: WatchlistScore | null;
  notes: string[];
}

export interface QualificationHistory {
  asset_id: string;
  evaluations: Evaluation[];
  metrics: Metric[];
  notes: string[];
}

export interface LifecycleEvent {
  id: number;
  event_type: string;
  effective_at: string;
  effective_at_basis: string;
  observed_at: string;
  provider: string | null;
  previous_state: Record<string, unknown> | null;
  new_state: Record<string, unknown> | null;
  reason: string;
  discovery_job_id: string | null;
  reference_version_id: number | null;
}

export interface MembershipEvent {
  id: number;
  layer: UniverseLayer;
  action: "ADDED" | "REMOVED";
  sequence: number;
  effective_at: string;
  recorded_at: string;
  reason_code: string;
  reason: string;
  job_id: string | null;
  evaluation_id: string | null;
  score_id: number | null;
}

export interface AssetHistory {
  asset_id: string;
  lifecycle: LifecycleEvent[];
  membership: MembershipEvent[];
  reference_versions: ReferenceVersion[];
  mappings: SymbolMapping[];
  notes: string[];
}

export interface DiscoveryJob {
  id: string;
  kind: DiscoveryJobKind;
  provider: string;
  status: DiscoveryJobStatus;
  trigger: string;
  requested_by: string;
  params: Record<string, unknown>;
  parent_job_id: string | null;
  attempt: number;
  max_attempts: number;
  next_attempt_at: string;
  created_at: string;
  started_at: string | null;
  heartbeat_at: string | null;
  completed_at: string | null;
  counters: Record<string, unknown>;
  summary: Record<string, unknown>;
  error: string | null;
  recent_errors: { at: string; attempt: number; code: string; message: string }[];
}

export interface JobBrief {
  id: string;
  status: DiscoveryJobStatus;
  created_at: string;
  completed_at: string | null;
  error: string | null;
  counters: Record<string, unknown>;
}

export interface DiscoverySummary {
  provider: string;
  catalogued: number;
  by_status: Partial<Record<AssetStatus, number>>;
  discovery_pool: number;
  qualified: number;
  watchlist: number;
  recent_days: number;
  new_listings: number;
  recently_disqualified: number;
  recently_deactivated: number;
  last_successful_discovery: JobBrief | null;
  last_jobs: Partial<Record<DiscoveryJobKind, JobBrief | null>>;
  reference_capabilities: {
    provider: string;
    exchanges: string[];
    not_supplied: string[];
    notes: string[];
    verified_on: string;
  } | null;
  notes: string[];
}

export interface ConfigVersion {
  id: string;
  code: string;
  version: number;
  description: string;
  config: Record<string, unknown>;
  content_sha256: string;
  created_by: string;
  created_at: string;
}

export interface DiscoveryConfig {
  rule_set: ConfigVersion;
  rule_set_versions: ConfigVersion[];
  scoring: ConfigVersion;
  scoring_versions: ConfigVersion[];
  settings: Record<string, unknown>;
  notes: string[];
}

export interface JobRequestResponse {
  job: DiscoveryJob;
  created: boolean;
}
