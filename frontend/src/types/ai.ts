/** Shapes of the /api/v1/ai responses (Phase 5B: central decision coordinator and LLM). */

export type DecisionOutcome =
  | "PRIORITIZE_FOR_REVIEW"
  | "DEPRIORITIZE"
  | "REJECT"
  | "DEFER"
  | "INSUFFICIENT_DATA"
  | "NO_OPPORTUNITY";

export interface Reason {
  code: string;
  message: string;
  outcome: DecisionOutcome | null;
}

export interface AIOverview {
  runs: Record<string, number>;
  decisions: Record<string, number>;
  llm_analyses: Record<string, number>;
  latest_run: RunSummary | null;
  llm: { provider: string; model: string | null; configured: boolean; priced: boolean };
  current_policy_sha256: string;
  disclaimer: string;
  llm_note: string;
}

export interface RunSummary {
  id: string;
  policy_version_id: string;
  policy_code: string;
  policy_version: number;
  mode: string;
  basis: string;
  trigger: string;
  decision_at: string;
  knowledge_at: string;
  status: string;
  outcome: string | null;
  proposals_considered: number;
  candidates_count: number;
  prioritized_count: number;
  outcome_counts: Record<string, number>;
  notes: string[];
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface DecisionSummary {
  id: string;
  run_id: string;
  outcome: DecisionOutcome;
  rank: number | null;
  asset_id: string | null;
  symbol: string | null;
  direction: string | null;
  timeframes: string[];
  strategies: string[];
  proposal_count: number;
  heuristic_score: number | null;
  ranking_score: number | null;
  component_coverage: number | null;
  calibrated_probability: number | null;
  expected_value_status: string | null;
  reward_risk_net: number | null;
  uncertainty_index: number | null;
  reasons: Reason[];
  conflict_types: string[];
  decision_at: string;
  data_cutoff: string;
  expires_at: string;
}

export interface ConflictView {
  id: number;
  run_id: string;
  type: string;
  asset_id: string | null;
  symbol: string | null;
  decision_ids: string[];
  proposal_ids: string[];
  handling: string;
  description: string;
  data: Record<string, unknown>;
}

export interface AnalysisView {
  id: string;
  task: string;
  subject_kind: string;
  subject_ref: string;
  article_id: string | null;
  headline: string | null;
  run_id: string | null;
  symbols: string[];
  provider: string;
  model: string;
  prompt_version: string;
  schema_version: string;
  analysis: Record<string, unknown>;
  flags: string[];
  usable: boolean;
  subject_time: string | null;
  available_at: string;
}

export interface RunDetail extends RunSummary {
  inputs_sha256: string | null;
  result_sha256: string | null;
  summary: Record<string, unknown>;
  code_version: Record<string, unknown>;
  input_counts: Record<string, number>;
  decisions: DecisionSummary[];
  conflicts: ConflictView[];
  explanations: AnalysisView[];
}

export interface ComponentView {
  name: string;
  kind: string;
  status: "AVAILABLE" | "UNAVAILABLE" | "EXCLUDED";
  score: number | null;
  raw_value: number | null;
  weight: number;
  contribution: number | null;
  reason: string;
  inputs: Record<string, unknown>;
}

export interface EvidenceView {
  id: number;
  kind: string;
  stance: "SUPPORTS" | "OPPOSES" | "NEUTRAL" | "CONTEXT";
  summary: string;
  input_id: number | null;
  data: Record<string, unknown>;
}

export interface ProposalBrief {
  id: string;
  strategy_id: string;
  direction: string;
  timeframe: string;
  signal_at: string;
  data_cutoff: string;
  reference_entry: string;
  invalidation_level: string | null;
  target_reference: string | null;
  signal_strength: string;
  strength_kind: string;
  expires_at: string;
  reasoning: string;
}

export interface DecisionDetail extends DecisionSummary {
  policy_version_id: string;
  proposal_ids: string[];
  strategy_versions: { strategy_id: string; version: number; logic_version: string }[];
  model_versions: {
    model_version_id: string;
    model_name: string;
    version: number;
    status_at_decision: string;
    output_kind: string;
    used: boolean;
  }[];
  regime_snapshot: {
    market?: Record<string, { label: string; confidence: number; as_of: string }>;
    asset?: Record<string, { label: string; confidence: number; as_of: string }>;
  };
  news_refs: { event_count: number; window_start: string; window_end: string }[];
  llm_refs: { analysis_id: string; model: string; prompt_version: string; available_at: string }[];
  feature_timestamps: Record<string, unknown>;
  scores: Record<string, unknown>;
  expected: Record<string, unknown>;
  uncertainty: Record<string, unknown>;
  conflicts: Record<string, unknown>[];
  explanation: string;
  content_sha256: string;
  created_at: string;
  components: ComponentView[];
  evidence: EvidenceView[];
  proposals: ProposalBrief[];
  llm_analyses: AnalysisView[];
}

export interface Rankings {
  run: RunSummary | null;
  decisions: DecisionSummary[];
  disclaimer: string;
}

export interface UsageReport {
  window_days: number;
  configuration: {
    provider: string;
    model: string | null;
    priced: boolean;
    input_usd_per_mtok: string | null;
    output_usd_per_mtok: string | null;
    pricing_source: string | null;
    max_concurrency: number;
    max_retries: number;
    batch_size: number;
    news_analysis_enabled: boolean;
  };
  budget: {
    spent_today_usd: string;
    spent_month_usd: string;
    reserved_usd: string;
    requests_today: number;
    daily_budget_usd: string;
    monthly_budget_usd: string;
    max_requests_per_day: number;
  };
  by_day: { day: string; input_tokens: number; output_tokens: number; responses: number; cost_usd: string }[];
  by_task: { task: string; model: string; input_tokens: number; output_tokens: number; responses: number; cost_usd: string }[];
  request_statuses: Record<string, number>;
  note: string;
}

export interface EvaluationGroup {
  observations: number | null;
  mean: number | null;
  median?: number | null;
  hit_rate?: number | null;
  note?: string;
}

export interface EvaluationView {
  id: string;
  known_at: string;
  decision_start: string | null;
  decision_end: string | null;
  runs_evaluated: number;
  spec: Record<string, unknown>;
  results: Record<string, unknown>;
  verdict: "INSUFFICIENT_EVIDENCE" | "NO_INCREMENTAL_VALUE" | "INCREMENTAL_VALUE_OBSERVED";
  notes: string[];
  created_at: string;
}
