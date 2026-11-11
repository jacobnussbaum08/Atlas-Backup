import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { drawdownSeries, pct, stitchedSeries } from "../lib/validation";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type {
  Fold,
  ModelGroup,
  MonitoringView,
  RobustnessRow,
  TradeStatistics,
  ValidationOverview,
  ValidationRun,
  ValidationRunDetail,
  WindowMetrics,
} from "../types/validation";

const charts = vi.hoisted(() => ({ series: [] as { kind: string; data: unknown[] }[] }));
vi.mock("lightweight-charts", () => ({
  LineSeries: "Line",
  AreaSeries: "Area",
  ColorType: { Solid: "solid" },
  createChart: () => ({
    addSeries: (kind: string) => ({ setData: (data: unknown[]) => charts.series.push({ kind, data }) }),
    timeScale: () => ({ fitContent: () => undefined }),
    remove: () => undefined,
  }),
}));

// TEST FIXTURES shaped like /api/v1/validation responses. Invented numbers, not results.
const RUN_ID = "22222222-2222-4222-8222-222222222222";

const STATS: TradeStatistics = {
  trades: 4,
  wins: 1,
  win_rate: 0.25,
  average_win: 50,
  average_loss: -30,
  profit_factor: 0.55,
  expectancy: -10,
  net_pnl: -40,
  gross_pnl: -36,
  costs: 4,
  average_holding_sessions: 3,
  top5_share_of_net_profit: null,
  long_trades: 4,
  short_trades: 0,
  status: "INSUFFICIENT_SAMPLE",
};

const WINDOW: WindowMetrics = {
  first: "2026-08-03",
  last: "2026-08-14",
  sessions: 2,
  start_equity: 100000,
  end_equity: 99800,
  net_return: -0.002,
  gross_return: -0.0015,
  max_drawdown: 0.004,
  sharpe_annualized: -1.2,
  sortino_annualized: null,
  returns_status: "INSUFFICIENT_SAMPLE",
  costs: 4,
  turnover: 0.3,
  benchmark_return: 0.01,
  excess_return: -0.012,
  trade_statistics: STATS,
};

const RUN: ValidationRun = {
  id: RUN_ID,
  experiment_id: "e",
  name: "Fixture walk-forward",
  kind: "WALK_FORWARD",
  strategies: ["momentum"],
  search_method: "RANDOM",
  status: "COMPLETED",
  progress: { stage: "completed" },
  start_date: "2026-06-29",
  end_date: "2026-10-30",
  holdout_start: "2026-10-07",
  holdout_end: "2026-10-30",
  data_known_at: "2026-11-01T00:00:00Z",
  verdict: "INSUFFICIENT_EVIDENCE",
  findings: ["INSUFFICIENT_EVIDENCE", "UNPROFITABLE"],
  trials: 12,
  prior_trials: 8,
  oos_net_return: -0.002,
  oos_sharpe: -1.2,
  oos_max_drawdown: 0.004,
  oos_trades: 4,
  oos_sessions: 2,
  reproduction_of: null,
  reproduced_identical: null,
  result_sha256: "ab".repeat(32),
  error_message: null,
  created_at: "2026-11-11T00:00:00Z",
  finished_at: "2026-11-11T00:05:00Z",
  requested_by: "operator:api",
};

const STITCHED = {
  sessions: 2,
  windows: 1,
  net_return: -0.002,
  gross_return: -0.0015,
  max_drawdown: 0.004,
  sharpe_annualized: -1.2,
  sortino_annualized: null,
  returns_status: "INSUFFICIENT_SAMPLE" as const,
  costs: 4,
  turnover_mean: 0.3,
  benchmark_return: 0.01,
  excess_return: -0.012,
  trade_statistics: STATS,
  fold_net_returns: [-0.002],
};

const DETAIL: ValidationRunDetail = {
  ...RUN,
  config: {},
  config_sha256: "c".repeat(64),
  base_config_sha256: "d".repeat(64),
  engine_version: "8B.1",
  code_version: "abc123def456",
  seed: 0,
  layout: {},
  summary: {
    reasons: ["out of sample: 4 closed trades (need 30) over 2 sessions (need 60)"],
    out_of_sample: STITCHED,
    champion_out_of_sample: { ...STITCHED, net_return: -0.004 },
    selected_trials: [3],
    statement: "Historical simulation. Out-of-sample here means data not used to choose these parameters.",
  },
  strategy_versions: [{ strategy_id: "momentum", version: 1, logic_version: "momentum-1", parameters_sha256: "e".repeat(64) }],
  risk_source: "ENGINE_DEFAULTS",
  execution_model_sha256: "f".repeat(64),
  assets: ["FIXA"],
  benchmark: "SPY",
  holdout_evaluations: [],
  holdout_status: "UNTOUCHED",
};

const FOLDS: Fold[] = [
  {
    fold_index: 0,
    refit: true,
    train: ["2026-06-29", "2026-07-31"],
    validation: null,
    test: ["2026-08-03", "2026-08-14"],
    embargo_sessions: 11,
    selected_trial: 3,
    selection: { note: "" },
    in_sample: { ...WINDOW, net_return: 0.03, sharpe_annualized: 2.5 },
    validation_metrics: null,
    out_of_sample: WINDOW,
    champion_out_of_sample: { ...WINDOW, net_return: -0.004 },
    oos_series: [
      ["2026-08-03", -0.001, 99900],
      ["2026-08-04", -0.001, 99800],
    ],
    champion_series: [
      ["2026-08-03", -0.002, 99800],
      ["2026-08-04", -0.002, 99600],
    ],
  },
];

const ROBUSTNESS: RobustnessRow[] = [
  {
    method: "DEFLATED_SHARPE",
    scope: "stitched out-of-sample returns",
    status: "INSUFFICIENT_SAMPLE",
    result: { deflated_sharpe: 0.12, probabilistic_sharpe_vs_zero: 0.2, effective_trials: 9.5, prior_trials_on_overlapping_data: 8 },
    assumptions: ["the null: every trial has zero true Sharpe"],
    limitations: ["optimistic with overlapping positions"],
  },
  {
    method: "COST_STRESS",
    scope: "final parameters",
    status: "OK",
    result: { levels: { "0": { net_return: 0.001 }, "1": { net_return: -0.002 }, "2": { net_return: -0.005 } } },
    assumptions: ["scaled costs"],
    limitations: ["fills unchanged"],
  },
];

const OVERVIEW: ValidationOverview = {
  notices: ["Historical simulation only.", "Nothing here is promoted automatically."],
  runs_by_status: { COMPLETED: 1 },
  runs_by_verdict: { INSUFFICIENT_EVIDENCE: 1 },
  trials_recorded: 12,
  holdout_evaluations: 0,
  candidates_by_status: {},
  alerts_last_7_days: 0,
};

const MODELS: ModelGroup[] = [
  {
    dataset_version_id: "33333333-3333-4333-8333-333333333333",
    observations: 1000,
    mean_label_concurrency: 8.9,
    runs: 1,
    hyperparameter_candidates_tried: 3,
    any_incremental_value: false,
    statement: "test-period figures of 5A runs",
    models: [
      {
        training_run_id: "r1",
        model_type: "XGBOOST",
        task: "CLASSIFICATION",
        validation_status_5a: "FAILED",
        test_period_reuse: 2,
        approved_champion: false,
        predictive: { log_loss: 0.6927, baseline_log_loss: 0.6926 },
        walk_forward: { folds: 4, beating_baseline: 1, metric: "log_loss" },
        calibration: { test_ece: 0.005 },
        financial: { mean_net_return: -0.0025, take_all_mean_net_return: -0.001, effective_observations: 204 },
        incremental_value: false,
        reasons: ["the model's policy loses money after costs (worse than taking no trade)"],
      },
    ],
  },
];

const MONITORING: MonitoringView = {
  latest_monitor_run_id: "m1",
  measurements: [
    {
      id: 1,
      subject_kind: "STRATEGY",
      subject_key: "momentum",
      metric: "EXPECTANCY_SHIFT",
      feature: null,
      measured_at: "2026-11-11T00:00:00Z",
      value: null,
      p_value: null,
      status: "INSUFFICIENT_DATA",
      n_reference: 4,
      n_current: 0,
    },
  ],
  rules: [],
  alerts: [],
};

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

describe("Strategy Research & Validation", () => {
  beforeEach(() => {
    charts.series = [];
    installFakeLive();
  });

  it("lists runs with their verdicts and keeps backtests apart from live paper trading", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/validation/overview": { body: OVERVIEW },
      "/api/v1/validation/runs": { body: [RUN] },
      "/api/v1/validation/options": {
        body: { strategies: [{ strategy_id: "momentum", family: "MOMENTUM", name: "Momentum", default_space: null }], kinds: [], search_methods: ["NONE", "GRID", "RANDOM"], objectives: [], windows: {} },
      },
    });
    renderAt("/research");
    expect(await screen.findByRole("link", { name: "Fixture walk-forward" })).toBeInTheDocument();
    expect(screen.getByText("insufficient evidence")).toBeInTheDocument();
    expect(screen.getByText(/Historical simulation, not live paper trading/)).toBeInTheDocument();
    expect(screen.getByText("(+8 earlier)")).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Validation configuration" })).toBeInTheDocument();
  });

  it("shows folds, in-sample versus out-of-sample, robustness and an untouched holdout", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      [`/api/v1/validation/runs/${RUN_ID}`]: { body: DETAIL },
      [`/api/v1/validation/runs/${RUN_ID}/folds`]: { body: FOLDS },
      [`/api/v1/validation/runs/${RUN_ID}/trials`]: { body: [] },
      [`/api/v1/validation/runs/${RUN_ID}/robustness`]: { body: ROBUSTNESS },
      [`/api/v1/validation/runs/${RUN_ID}/segments`]: { body: [] },
    });
    renderAt(`/research/runs/${RUN_ID}`);
    const folds = await screen.findByRole("table", { name: "Walk-forward folds" });
    const row = within(folds).getAllByRole("row")[1];
    expect(row).toHaveTextContent("+3.00%"); // in-sample
    expect(row).toHaveTextContent("-0.20%"); // out of sample
    expect(await screen.findByText(/DSR 0.120/)).toBeInTheDocument();
    expect(screen.getByText(/0× \+0.10% · 1× -0.20% · 2× -0.50%/)).toBeInTheDocument();
    expect(screen.getByText("UNTOUCHED")).toBeInTheDocument();
    expect(screen.getByText(/4 closed trades \(need 30\)/)).toBeInTheDocument();
    await waitFor(() => expect(charts.series.length).toBeGreaterThanOrEqual(3)); // OOS, champion, drawdown
    fireEvent.click(screen.getByRole("button", { name: "Deflated Sharpe ratio" }));
    expect(screen.getByText(/the null: every trial has zero true Sharpe/)).toBeInTheDocument();
  });

  it("separates predictive and financial evidence for models, and labels monitoring as live", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/validation/overview": { body: OVERVIEW },
      "/api/v1/validation/runs": { body: [] },
      "/api/v1/validation/options": { body: { strategies: [], kinds: [], search_methods: [], objectives: [], windows: {} } },
      "/api/v1/validation/models/compare": { body: MODELS },
      "/api/v1/validation/monitoring": { body: MONITORING },
    });
    renderAt("/research");
    fireEvent.click(await screen.findByRole("radio", { name: "Model comparison" }));
    const table = await screen.findByRole("table", { name: /Models on dataset/ });
    expect(within(table).getByText("not shown")).toBeInTheDocument();
    expect(within(table).getByText(/loses money after costs/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Degradation monitoring" }));
    expect(await screen.findByText(/live paper-trading results/)).toBeInTheDocument();
    expect(screen.getByText("INSUFFICIENT_DATA")).toBeInTheDocument();
  });
});

describe("validation display helpers", () => {
  it("formats ratios and compounds stored returns for drawing", () => {
    expect(pct(0.0123)).toBe("+1.23%");
    expect(pct(-0.5, { signed: false })).toBe("-50.00%");
    expect(pct(null)).toBe("—");
    const equity = stitchedSeries([[["d1", 0.1, 0]], [["d2", -0.5, 0]]], 100);
    expect(equity[0]?.[1]).toBeCloseTo(110);
    expect(equity[1]?.[1]).toBeCloseTo(55);
    expect(drawdownSeries(equity, 100)[1]?.[1]).toBeCloseTo(-50);
  });
});
