import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type { DatasetVersionSummary, MLOverview, ModelSummary, RunDetail, RunSummary } from "../types/ml";

// TEST FIXTURES: API payloads shaped like /api/v1/ml responses. Not real results.
const OVERVIEW: MLOverview = {
  datasets: 1,
  dataset_versions: { TEST_FIXTURE: 1 },
  training_runs: { SUCCEEDED: 1 },
  models: { CANDIDATE: 1, VALIDATED: 0, SHADOW: 0, APPROVED: 0, REJECTED: 0, RETIRED: 0 },
  predictions: 0,
  segments: ["ALL"],
  splits: ["TEST"],
  notes: ["Models produce analytical estimates only: no model can submit orders."],
};
const EMPTY_OVERVIEW: MLOverview = { ...OVERVIEW, datasets: 0, dataset_versions: {}, training_runs: {}, models: {}, notes: [] };

const RUN: RunSummary = {
  id: "run-1",
  dataset_version_id: "ds-1",
  dataset: "fixture-direction",
  dataset_version: 1,
  origin: "TEST_FIXTURE",
  model_type: "XGBOOST",
  task: "CLASSIFICATION",
  status: "SUCCEEDED",
  validation_status: "FAILED",
  primary_metric: "log_loss",
  test_metric: 0.6931,
  test_baseline_metric: 0.6925,
  test_observations: 400,
  test_mean_net_return: -0.0012,
  test_take_all_net_return: -0.001,
  walk_forward_win_fraction: 0.25,
  test_period_reuse: 1,
  model_version_id: "model-1",
  model_name: "direction.1day.xgb",
  started_at: "2026-11-03T06:00:00Z",
  finished_at: "2026-11-03T06:01:00Z",
  error: null,
};

const RUN_DETAIL: RunDetail = {
  ...RUN,
  spec: {},
  target_definition: { kind: "DIRECTION", horizon_periods: 5, round_trip_cost_bps: 10 },
  code_version: { commit: "abcdef1234567890", dirty: false },
  random_seed: 7,
  periods: {
    train: { start: "2025-06-01T00:00:00Z", end: "2026-01-01T00:00:00Z", observations: 1200 },
    validation: { start: "2026-01-10T00:00:00Z", end: "2026-04-01T00:00:00Z", observations: 400 },
    test: { start: "2026-04-10T00:00:00Z", end: "2026-10-20T00:00:00Z", observations: 400 },
    embargo_periods: 5,
  },
  preprocessing: { numeric: ["a", "b"], dropped: { c: "constant in training" } },
  parameters: { family: "gradient-boosted trees (XGBoost, hist)", hyperparameters: { max_depth: 3 } },
  training_metrics: null,
  validation_metrics: null,
  test_metrics: null,
  baseline_metrics: null,
  walk_forward: null,
  robustness: null,
  selection: { criterion: "lowest mean walk-forward log_loss", candidates: 3 },
  validation_reasons: ["test log loss does not beat the baseline"],
  notes: ["Outputs are uncalibrated scores, not probabilities."],
  evaluations: [
    {
      split: "WALK_FORWARD",
      fold: 1,
      segment_kind: "ALL",
      segment_value: "ALL",
      period_start: "2025-09-01T00:00:00Z",
      period_end: "2025-10-01T00:00:00Z",
      observations: 300,
      metrics: { log_loss: 0.694, financial: { mean_net_return: -0.002 } },
      baseline_metrics: { log_loss: 0.693 },
    },
    {
      split: "TEST",
      fold: null,
      segment_kind: "DIRECTION",
      segment_value: "SHORT",
      period_start: null,
      period_end: null,
      observations: 12,
      metrics: { observations: 12, insufficient: true },
      baseline_metrics: {},
    },
  ],
  calibration: [
    {
      method: "SIGMOID",
      fitted_on: "VALIDATION",
      evaluated_on: "VALIDATION_HOLDOUT",
      observations: 200,
      brier_raw: 0.26,
      brier_calibrated: 0.25,
      brier_base_rate: 0.249,
      ece_raw: 0.08,
      ece_calibrated: 0.03,
      reliability_raw: [],
      reliability_calibrated: [],
      selected: true,
      accepted: false,
      decision: "not accepted as a probability: held-out Brier worse than the base rate's",
    },
  ],
};

const MODEL: ModelSummary = {
  id: "model-1",
  model_name: "direction.1day.xgb",
  version: 1,
  model_type: "XGBOOST",
  task: "CLASSIFICATION",
  status: "CANDIDATE",
  status_changed_at: "2026-11-03T06:01:00Z",
  output_kind: "UNCALIBRATED_SCORE",
  calibration_method: "NONE",
  validation_status: "FAILED",
  origin: "TEST_FIXTURE",
  training_run_id: "run-1",
  dataset_version_id: "ds-1",
  evaluation_summary: { test: { log_loss: 0.6931 }, test_baseline: { log_loss: 0.6925 } },
  target_kind: "DIRECTION",
  created_at: "2026-11-03T06:01:00Z",
};

const DATASET: DatasetVersionSummary = {
  id: "ds-1",
  dataset: "fixture-direction",
  version: 1,
  source: "PERIOD_CLOSE",
  origin: "TEST_FIXTURE",
  target_kind: "DIRECTION",
  timeframes: ["1Day"],
  asset_count: 3,
  observation_count: 2400,
  matured_count: 2300,
  decision_start: "2025-06-02T21:00:00Z",
  decision_end: "2026-11-03T22:00:00Z",
  data_known_at: "2026-11-03T22:30:00Z",
  warnings: ["TEST FIXTURE: the bars are synthetic test data, not market data."],
  content_sha256: "0".repeat(64),
  created_at: "2026-11-03T06:00:00Z",
};

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider initial="UTC">
      <MemoryRouter initialEntries={[path]}>
        <App pollIntervalMs={0} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

beforeEach(() => {
  installFakeLive();
});

describe("Machine Learning page", () => {
  it("says so when nothing has been trained, without inventing numbers", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/ml/overview": { body: EMPTY_OVERVIEW },
      "/api/v1/ml/models": { body: [] },
      "/api/v1/ml/runs": { body: [] },
      "/api/v1/ml/datasets": { body: [] },
      "/api/v1/ml/predictions": { body: [] },
    });
    renderAt("/ml");
    expect(await screen.findByText("No model has been trained yet.")).toBeInTheDocument();
    expect(screen.getByText("No training run yet.")).toBeInTheDocument();
    expect(screen.getByText("No dataset has been built yet.")).toBeInTheDocument();
    expect(screen.getByText("No prediction has been recorded.")).toBeInTheDocument();
  });

  it("shows the registry, runs with their evidence, and labels fixture data", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/ml/overview": { body: OVERVIEW },
      "/api/v1/ml/models": { body: [MODEL] },
      "/api/v1/ml/runs": { body: [RUN] },
      "/api/v1/ml/runs/run-1": { body: RUN_DETAIL },
      "/api/v1/ml/datasets": { body: [DATASET] },
      "/api/v1/ml/predictions": { body: [] },
    });
    renderAt("/ml");
    const registry = await screen.findByRole("table", { name: "Model registry" });
    expect(within(registry).getByText("direction.1day.xgb")).toBeInTheDocument();
    expect(within(registry).getByText("Candidate")).toBeInTheDocument();
    expect(within(registry).getByText("Uncalibrated score")).toBeInTheDocument();
    expect(within(registry).getByText("Test fixture")).toBeInTheDocument();
    expect(screen.getByText(/no model can submit orders/)).toBeInTheDocument();

    const runs = screen.getByRole("table", { name: "Training runs" });
    expect(within(runs).getByText("×2")).toBeInTheDocument(); // test period evaluated twice
    fireEvent.click(within(runs).getByRole("button", { name: "Details of run run-1" }));
    expect(await screen.findByText("test log loss does not beat the baseline")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Walk-forward folds" })).toBeInTheDocument();
    expect(screen.getByText(/not accepted as a probability/)).toBeInTheDocument();
    fireEvent.click(screen.getByText(/Test period by segment/));
    expect(screen.getByText("too few observations to evaluate")).toBeInTheDocument();

    const datasets = screen.getByRole("table", { name: "Dataset versions" });
    expect(within(datasets).getByText("2,400 (2,300)")).toBeInTheDocument();
  });

  it("is in the navigation", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/ml/overview": { body: EMPTY_OVERVIEW },
      "/api/v1/ml/models": { body: [] },
      "/api/v1/ml/runs": { body: [] },
      "/api/v1/ml/datasets": { body: [] },
      "/api/v1/ml/predictions": { body: [] },
    });
    renderAt("/ml");
    const link = await screen.findByRole("link", { name: /Machine Learning/ });
    expect(link).toHaveAttribute("href", "/ml");
  });
});
