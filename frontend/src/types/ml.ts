/** Machine-learning engine API types (Phase 5A, /api/v1/ml). */

export type LifecycleStatus = "CANDIDATE" | "VALIDATED" | "SHADOW" | "APPROVED" | "REJECTED" | "RETIRED";
export type ValidationStatus = "PASSED" | "FAILED" | "INSUFFICIENT_EVIDENCE";
export type OutputKind = "CALIBRATED_PROBABILITY" | "UNCALIBRATED_SCORE" | "REGRESSION_ESTIMATE";
export type DataOrigin = "REAL_HISTORICAL" | "TEST_FIXTURE";
export type RunStatus = "RUNNING" | "SUCCEEDED" | "FAILED";

export type Metrics = Record<string, unknown>;

export interface MLOverview {
  datasets: number;
  dataset_versions: Record<string, number>;
  training_runs: Record<string, number>;
  models: Record<string, number>;
  predictions: number;
  segments: string[];
  splits: string[];
  notes: string[];
}

export interface DatasetVersionSummary {
  id: string;
  dataset: string;
  version: number;
  source: string;
  origin: DataOrigin;
  target_kind: string;
  timeframes: string[];
  asset_count: number;
  observation_count: number;
  matured_count: number;
  decision_start: string | null;
  decision_end: string | null;
  data_known_at: string;
  warnings: string[];
  content_sha256: string;
  created_at: string;
}

export interface RunSummary {
  id: string;
  dataset_version_id: string;
  dataset: string;
  dataset_version: number;
  origin: DataOrigin;
  model_type: string;
  task: "CLASSIFICATION" | "REGRESSION";
  status: RunStatus;
  validation_status: ValidationStatus | null;
  primary_metric: string;
  test_metric: number | null;
  test_baseline_metric: number | null;
  test_observations: number | null;
  test_mean_net_return: number | null;
  test_take_all_net_return: number | null;
  walk_forward_win_fraction: number | null;
  test_period_reuse: number;
  model_version_id: string | null;
  model_name: string | null;
  started_at: string;
  finished_at: string | null;
  error: string | null;
}

export interface Evaluation {
  split: string;
  fold: number | null;
  segment_kind: string;
  segment_value: string;
  period_start: string | null;
  period_end: string | null;
  observations: number;
  metrics: Metrics;
  baseline_metrics: Metrics;
}

export interface ReliabilityBin {
  lower: number;
  upper: number;
  count: number;
  mean_score: number | null;
  observed_rate: number | null;
}

export interface CalibrationResult {
  method: string;
  fitted_on: string;
  evaluated_on: string;
  observations: number;
  brier_raw: number | null;
  brier_calibrated: number | null;
  brier_base_rate: number | null;
  ece_raw: number | null;
  ece_calibrated: number | null;
  reliability_raw: ReliabilityBin[];
  reliability_calibrated: ReliabilityBin[];
  selected: boolean;
  accepted: boolean;
  decision: string;
}

export interface RunDetail extends RunSummary {
  spec: Metrics;
  target_definition: Metrics;
  code_version: Metrics;
  random_seed: number;
  periods: Metrics | null;
  preprocessing: Metrics | null;
  parameters: Metrics | null;
  training_metrics: Metrics | null;
  validation_metrics: Metrics | null;
  test_metrics: Metrics | null;
  baseline_metrics: Metrics | null;
  walk_forward: Metrics | null;
  robustness: Metrics | null;
  selection: Metrics | null;
  validation_reasons: string[];
  notes: string[];
  evaluations: Evaluation[];
  calibration: CalibrationResult[];
}

export interface ModelSummary {
  id: string;
  model_name: string;
  version: number;
  model_type: string;
  task: string;
  status: LifecycleStatus;
  status_changed_at: string | null;
  output_kind: OutputKind;
  calibration_method: string;
  validation_status: ValidationStatus;
  origin: DataOrigin;
  training_run_id: string;
  dataset_version_id: string;
  evaluation_summary: Metrics;
  target_kind: string;
  created_at: string;
}

export interface PredictionView {
  id: string;
  model_version_id: string;
  model_name: string;
  model_version: number;
  model_status: LifecycleStatus;
  mode: string;
  symbol: string | null;
  timeframe: string;
  direction: string;
  strategy_id: string | null;
  decision_at: string;
  predicted_at: string;
  validity: string;
  problems: string[];
  output_kind: OutputKind;
  output: Metrics | null;
  requested_by: string;
}
