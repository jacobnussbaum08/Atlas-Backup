/**
 * Shapes of the Phase 8B /api/v1/validation responses. Statistics are analytical numbers
 * (JSON floats) derived from the Decimal replays; nothing here is a ledger amount.
 */
export type RunStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
export type Verdict = "INSUFFICIENT_EVIDENCE" | "NOT_ROBUST" | "CANDIDATE_FOR_REVIEW";
export type CandidateStatus = "CANDIDATE" | "VALIDATING" | "SHADOW" | "APPROVED" | "REJECTED" | "RETIRED";
export type ResultStatus = "OK" | "INSUFFICIENT_SAMPLE" | "NOT_APPLICABLE" | "NOT_RUN";
export type MeasurementStatus = "OK" | "WARNING" | "BREACH" | "INSUFFICIENT_DATA";
export type Json = Record<string, unknown>;

export interface TradeStatistics {
  trades: number;
  wins: number;
  win_rate: number | null;
  average_win: number | null;
  average_loss: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  net_pnl: number;
  gross_pnl: number;
  costs: number;
  average_holding_sessions: number | null;
  top5_share_of_net_profit: number | null;
  long_trades: number;
  short_trades: number;
  status: ResultStatus;
}

export interface WindowMetrics {
  first: string;
  last: string;
  sessions: number;
  start_equity: number;
  end_equity: number;
  net_return: number;
  gross_return: number;
  max_drawdown: number;
  sharpe_annualized: number | null;
  sortino_annualized: number | null;
  returns_status: ResultStatus;
  costs: number;
  turnover: number | null;
  benchmark_return: number | null;
  excess_return: number | null;
  trade_statistics: TradeStatistics;
  purged_trades?: Record<string, number>;
}

export interface Stitched {
  sessions: number;
  windows: number;
  net_return: number;
  gross_return: number;
  max_drawdown: number;
  sharpe_annualized: number | null;
  sortino_annualized: number | null;
  returns_status: ResultStatus;
  costs: number;
  turnover_mean: number | null;
  benchmark_return: number | null;
  excess_return: number | null;
  trade_statistics: TradeStatistics;
  fold_net_returns: number[];
}

export interface ValidationRun {
  id: string;
  experiment_id: string;
  name: string;
  kind: "WALK_FORWARD" | "EVALUATION";
  strategies: string[];
  search_method: string;
  status: RunStatus;
  progress: { stage?: string; done?: number; total?: number };
  start_date: string;
  end_date: string;
  holdout_start: string | null;
  holdout_end: string | null;
  data_known_at: string;
  verdict: Verdict | null;
  findings: string[];
  trials: number | null;
  prior_trials: number | null;
  oos_net_return: number | null;
  oos_sharpe: number | null;
  oos_max_drawdown: number | null;
  oos_trades: number | null;
  oos_sessions: number | null;
  reproduction_of: string | null;
  reproduced_identical: boolean | null;
  result_sha256: string | null;
  error_message: string | null;
  created_at: string;
  finished_at: string | null;
  requested_by: string;
}

export interface HoldoutEvaluation {
  id: string;
  run_id: string;
  holdout_start: string;
  holdout_end: string;
  access_number: number;
  parameters_sha256: string;
  result: WindowMetrics & { note: string; series: [string, number, number][] };
  created_at: string;
  requested_by: string;
}

export interface ValidationRunDetail extends ValidationRun {
  config: Json;
  config_sha256: string;
  base_config_sha256: string;
  engine_version: string;
  code_version: string;
  seed: number;
  layout: Json | null;
  summary: {
    reasons?: string[];
    out_of_sample?: Stitched;
    champion_out_of_sample?: Stitched;
    final_parameters?: Record<string, Json>;
    stored_parameters?: Record<string, Json>;
    trials_recorded?: number;
    infeasible_points?: number;
    selected_trials?: number[];
    statement?: string;
    holdout?: Json | null;
    thresholds?: Json;
  } | null;
  strategy_versions: { strategy_id: string; version: number; logic_version: string; parameters_sha256: string }[];
  risk_source: string | null;
  execution_model_sha256: string | null;
  assets: string[];
  benchmark: string | null;
  holdout_evaluations: HoldoutEvaluation[];
  holdout_status: "UNTOUCHED" | "EVALUATED_ONCE" | "REUSED";
}

export interface Fold {
  fold_index: number;
  refit: boolean;
  train: [string, string];
  validation: [string, string] | null;
  test: [string, string];
  embargo_sessions: number;
  selected_trial: number;
  selection: { note?: string; objective?: string; ranking?: { trial: number; train_score: number | null; plateau_score: number | null; validation_score: number | null }[] };
  in_sample: WindowMetrics | null;
  validation_metrics: WindowMetrics | null;
  out_of_sample: WindowMetrics;
  champion_out_of_sample: WindowMetrics | null;
  oos_series: [string, number, number][];
  champion_series: [string, number, number][] | null;
}

export interface Trial {
  trial_number: number;
  strategy_id: string;
  source: string;
  status: string;
  selectable: boolean;
  parameters: Json;
  development: WindowMetrics | null;
  error: string | null;
}

export interface RobustnessRow {
  method: string;
  scope: string;
  status: ResultStatus;
  result: Json;
  assumptions: string[];
  limitations: string[];
}

export interface SegmentRow {
  sample: string;
  segment_kind: string;
  segment_value: string;
  metrics: TradeStatistics;
  status: ResultStatus;
}

export interface ValidationOverview {
  notices: string[];
  runs_by_status: Record<string, number>;
  runs_by_verdict: Record<string, number>;
  trials_recorded: number;
  holdout_evaluations: number;
  candidates_by_status: Record<string, number>;
  alerts_last_7_days: number;
}

export interface ValidationOptions {
  strategies: { strategy_id: string; family: string; name: string; default_space: Json | null }[];
  kinds: string[];
  search_methods: string[];
  objectives: string[];
  windows: Json;
}

export interface Candidate {
  id: string;
  subject_kind: "STRATEGY" | "MODEL";
  subject_key: string;
  status: CandidateStatus;
  allowed_transitions: string[];
  parameters: Json | null;
  parameters_sha256: string | null;
  model_version_id: string | null;
  source_run_id: string | null;
  notes: string;
  created_by: string;
  created_at: string;
}

export interface ChampionRow {
  subject_kind: "STRATEGY" | "MODEL";
  subject_key: string;
  governed: boolean;
  label: string;
  candidate_id: string | null;
  strategy_version: number | null;
  model_version_id: string | null;
  registry_statuses?: Record<string, number>;
  challengers: Candidate[];
}

export interface Approval {
  id: string;
  candidate_id: string;
  subject_kind: string | null;
  subject_key: string | null;
  decision: "APPROVE" | "REJECT" | "RETIRE" | "ROLLBACK";
  approver: string;
  reason: string;
  replaced: Json | null;
  strategy_version_id: string | null;
  model_version_id: string | null;
  created_at: string;
}

export interface ModelRow {
  training_run_id: string;
  model_type: string;
  task: string;
  validation_status_5a: string | null;
  test_period_reuse: number;
  approved_champion: boolean;
  predictive: Json;
  walk_forward: { folds: number; beating_baseline: number; metric: string | null };
  calibration: Json;
  financial: Json;
  incremental_value: boolean;
  reasons: string[];
}

export interface ModelGroup {
  dataset_version_id: string;
  observations: number | null;
  mean_label_concurrency: number;
  runs: number;
  hyperparameter_candidates_tried: number;
  models: ModelRow[];
  any_incremental_value: boolean;
  statement: string;
}

export interface Measurement {
  id: number;
  subject_kind: string;
  subject_key: string;
  metric: string;
  feature: string | null;
  measured_at: string;
  value: string | null;
  p_value: string | null;
  status: MeasurementStatus;
  n_reference: number;
  n_current: number;
}

export interface AlertRow {
  id: string;
  subject_kind: string;
  subject_key: string;
  severity: "WARNING" | "CRITICAL";
  metric: string;
  message: string;
  action: "NONE" | "WARNED" | "SUSPENDED";
  created_at: string;
}

export interface MonitoringView {
  latest_monitor_run_id: string | null;
  measurements: Measurement[];
  rules: { id: string; subject_kind: string; subject_key: string; version: number; action: string; active: boolean; approved_by: string; reason: string }[];
  alerts: AlertRow[];
}

export interface ExperimentBody {
  name: string;
  kind: "WALK_FORWARD" | "EVALUATION";
  strategies: { strategy_id: string }[];
  start_date: string;
  end_date: string;
  universe: { mode: "EXPLICIT"; symbols: string[] };
  timeframes: string[];
  directions?: string[];
  benchmark_symbol: string | null;
  windows: { mode: string; train_sessions: number; test_sessions: number; validation_sessions: number };
  search: string;
  max_trials: number;
  seed: number;
}
