import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { formatRatio, ratioToPercentString } from "../lib/backtest";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type {
  BacktestDetail,
  BacktestOptions,
  BacktestRun,
  BenchmarkRow,
  EquityRow,
  MetricRow,
  TradeDetail,
  TradeRow,
} from "../types/backtests";

// lightweight-charts draws on canvas, which jsdom lacks: record what the page asks of it.
const charts = vi.hoisted(() => ({ series: [] as { kind: string; data: unknown[] }[] }));
vi.mock("lightweight-charts", () => ({
  LineSeries: "Line",
  AreaSeries: "Area",
  ColorType: { Solid: "solid" },
  createChart: () => ({
    addSeries: (kind: string) => ({
      setData: (data: unknown[]) => charts.series.push({ kind, data }),
    }),
    timeScale: () => ({ fitContent: () => undefined }),
    remove: () => undefined,
  }),
}));

// TEST FIXTURES shaped like /api/v1/backtests responses. Invented numbers, not results.
const RUN_ID = "11111111-1111-4111-8111-111111111111";

function run(extra: Partial<BacktestRun> = {}): BacktestRun {
  return {
    id: RUN_ID,
    name: "Fixture run",
    status: "COMPLETED",
    simulated: true,
    start_date: "2026-10-01",
    end_date: "2026-10-02",
    initial_capital: "100000",
    currency: "USD",
    strategies: ["momentum"],
    universe_mode: "EXPLICIT",
    symbols: ["FIXA"],
    asset_count: 1,
    benchmark_symbol: "SPY",
    seed: 0,
    created_at: "2026-11-01T00:00:00Z",
    started_at: "2026-11-01T00:00:01Z",
    finished_at: "2026-11-01T00:00:05Z",
    sessions_total: 2,
    sessions_done: 2,
    progress: 1,
    simulated_through: "2026-10-02T21:00:00Z",
    result_sha256: "ab".repeat(32),
    reproduction_of: null,
    reproduction_matches: null,
    error_stage: null,
    error_message: null,
    summary: { final_equity: "99876.50", total_return: "-0.00123500", trades: 1 },
    requested_by: "operator:api",
    ...extra,
  };
}

const DETAIL: BacktestDetail = {
  ...run(),
  request: {},
  config: { policies: { bar_availability: "close + settle" }, model_usage: "none" },
  config_sha256: "c".repeat(64),
  engine_version: "8A.1",
  code_version: "abc123",
  data_provider: "alpaca",
  data_feed: "iex",
  data_known_at: "2026-11-01T00:00:00Z",
  dataset_sha256: "d".repeat(64),
  dataset_bars: 10,
  execution_model_id: 3,
  execution_model_sha256: "e".repeat(64),
  risk_sha256: "f".repeat(64),
  risk_source: "ENGINE_DEFAULTS",
  strategy_versions: [
    {
      strategy_id: "momentum",
      strategy_version_id: "v",
      version: 2,
      logic_version: "momentum-1",
      parameters: {},
      parameters_sha256: "0".repeat(64),
      parameters_source: "STORED_VERSION",
      timeframes: ["1Day"],
    },
  ],
  errors: 0,
};

const metric = (name: string, value: string | null, extra: Partial<MetricRow> = {}): MetricRow => ({
  scope: "PORTFOLIO",
  scope_key: "portfolio",
  metric: name,
  value,
  unit: "ratio",
  status: "OK",
  sample_size: 2,
  detail: "",
  ...extra,
});

const METRICS: MetricRow[] = [
  metric("total_return", "-0.00123500"),
  metric("gross_total_return", "0.00010000"),
  metric("final_equity", "99876.50", { unit: "USD" }),
  metric("sharpe_ratio", null, { status: "INSUFFICIENT_SAMPLE", detail: "2 daily returns; at least 126 are required" }),
  metric("win_rate", "0", { status: "INSUFFICIENT_SAMPLE", detail: "1 closed trades; fewer than 30" }),
  metric("trades_closed", "1", { unit: "count" }),
  metric("total_costs", "2.50", { unit: "USD" }),
  metric("net_pnl_trades", "-123.50", { scope: "STRATEGY", scope_key: "momentum", unit: "USD" }),
];

const EQUITY: EquityRow[] = ["2026-10-01", "2026-10-02"].map((session, i) => ({
  session,
  at: `${session}T21:00:00Z`,
  equity: i ? "99876.50" : "100000.00",
  gross_equity: i ? "99879.00" : "100000.00",
  cash: "100000.00",
  long_exposure: "0",
  short_exposure: "0",
  realized_pnl: "0",
  unrealized_pnl: "0",
  total_costs: i ? "2.50" : "0",
  drawdown: i ? "123.50" : "0",
  drawdown_pct: i ? "0.00123500" : "0",
  open_positions: 0,
  details: {},
}));

const BENCH: BenchmarkRow[] = [
  {
    kind: "BENCHMARK",
    label: "SPY",
    available: true,
    total_return: "0.01000000",
    annualized_return: null,
    max_drawdown_pct: "0",
    excess_return: "-0.01123500",
    details: { series: [["2026-10-01", "1.005"], ["2026-10-02", "1.01"]], notes: ["price return"] },
  },
];

const TRADE: TradeRow = {
  seq: 1,
  proposal_seq: 1,
  strategy_id: "momentum",
  timeframe: "1Day",
  asset_id: "a",
  symbol: "FIXA",
  direction: "LONG",
  status: "CLOSED",
  opened_at: "2026-10-01T21:00:00Z",
  closed_at: "2026-10-02T21:00:00Z",
  entry_quantity: "10.000000000000000000",
  entry_price: "100.0000000000",
  exit_quantity: "10",
  exit_price: "87.9000000000",
  exit_reason: "STOP_LOSS",
  realized_gross: "-121.00",
  unrealized: "0",
  costs: "2.50",
  net_pnl: "-123.50",
  holding_sessions: 1,
  details: { first_session: "2026-10-01", stop_level: "95", target_level: "110", notes: [] },
};

const TRADE_DETAIL: TradeDetail = {
  trade: TRADE,
  proposal: {
    seq: 1,
    session: "2026-09-30",
    strategy_id: "momentum",
    timeframe: "1Day",
    symbol: "FIXA",
    direction: "LONG",
    reference_entry: "100",
    invalidation_level: "95",
    target_reference: "110",
    signal_strength: "0.5",
    risk_result: "PASS_FOR_RESERVATION",
    permitted_quantity: "10",
    binding_constraint: "risk_budget",
    outcome: "FILLED",
    details: { reasoning: "fixture reasoning" },
  },
  orders: [
    {
      seq: 2,
      role: "STOP",
      side: "SELL",
      order_type: "STOP",
      quantity: "10",
      filled_quantity: "10",
      average_price: "87.9",
      status: "FILLED",
      arrival_at: "2026-09-30T21:00:01Z",
      expires_at: null,
      commission: "1.00",
      fees: "0.05",
      details: {},
    },
  ],
  fills: [
    {
      seq: 2,
      order_seq: 2,
      side: "SELL",
      role: "STOP",
      session: "2026-10-02",
      quantity: "10",
      price: "87.90",
      notional: "879.00",
      commission: "1.00",
      fees: "0.05",
      basis: "BAR_FALLBACK",
      details: { gap: true, assumptions: ["gap: the bar opened beyond the stop"] },
    },
  ],
};

const OPTIONS: BacktestOptions = {
  strategies: [
    {
      strategy_id: "momentum",
      family: "MOMENTUM",
      name: "Momentum",
      implemented: true,
      supported_timeframes: ["1Day", "1Week"],
      latest_version: 2,
      logic_version: "momentum-1",
      replayable: true,
    },
  ],
  timeframes: ["1Day", "1Week"],
  universe_modes: ["EXPLICIT", "POINT_IN_TIME"],
  layers: ["QUALIFIED_UNIVERSE"],
  assets: [
    {
      asset_id: "a",
      symbol: "FIXA",
      asset_class: "EQUITY",
      raw: { first: "2026-06-25", last: "2026-10-30", bars: 90 },
      split: null,
    },
  ],
  data: { provider: "alpaca", feed: "iex" },
};

const BASE = `/api/v1/backtests/${RUN_ID}`;
const ROUTES = {
  ...LAYOUT_ROUTES,
  "/api/v1/backtests": { body: [run()] },
  "/api/v1/backtests/options": { body: OPTIONS },
  [BASE]: { body: DETAIL },
  [`${BASE}/equity`]: { body: EQUITY },
  [`${BASE}/metrics`]: { body: METRICS },
  [`${BASE}/benchmarks`]: { body: BENCH },
  [`${BASE}/trades`]: { body: [TRADE] },
  [`${BASE}/trades/1`]: { body: TRADE_DETAIL },
  [`${BASE}/evaluations`]: { body: [{ strategy_id: "momentum", timeframe: "1Day", state: "NO_SIGNAL", count: 3 }] },
  [`${BASE}/rejections`]: { body: [] },
  [`${BASE}/events`]: { body: [] },
  [`${BASE}/errors`]: { body: [] },
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
  charts.series = [];
});

describe("backtest formatting", () => {
  it("turns ratio strings into exact percentages without floating point", () => {
    expect(ratioToPercentString("0.00123500")).toBe("0.123500");
    expect(ratioToPercentString("-1.5")).toBe("-150");
    expect(formatRatio("-0.00123500")).toBe("−0.12%");
    expect(formatRatio(null)).toBe("—");
  });
});

describe("backtesting page", () => {
  it("lists runs with their stored results and submits a validated configuration", async () => {
    const stub = stubApi({
      ...ROUTES,
      "/api/v1/backtests": (_url, init) =>
        init?.method === "POST" ? { status: 202, body: DETAIL } : { body: [run()] },
    });
    renderAt("/backtesting");
    const table = await screen.findByRole("table", { name: "Backtest runs" });
    expect(within(table).getByText("Fixture run")).toBeInTheDocument();
    expect(within(table).getByText("−0.12%")).toBeInTheDocument();
    expect(within(table).getByText("99,876.50")).toBeInTheDocument();

    const form = await screen.findByRole("form", { name: "Backtest configuration" });
    const submit = within(form).getByRole("button", { name: /run backtest/i });
    expect(submit).toBeDisabled(); // no symbol yet
    fireEvent.change(within(form).getByLabelText("Symbols"), { target: { value: "fixa, FIXA spy" } });
    fireEvent.change(within(form).getByLabelText("Operator token"), { target: { value: "op-secret" } });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(stub.calls.some((c) => c.init?.method === "POST")).toBe(true));
    const post = stub.calls.find((c) => c.init?.method === "POST")!;
    expect(new Headers(post.init?.headers).get("X-Atlas-Operator-Token")).toBe("op-secret");
    const body = JSON.parse(String(post.init?.body));
    expect(body.universe).toEqual({ mode: "EXPLICIT", symbols: ["FIXA", "SPY"] });
    expect(body.strategies).toEqual([{ strategy_id: "momentum" }]);
    expect(body.start_date).toBe("2026-06-25");
    expect(body.end_date).toBe("2026-10-30");
    expect(await screen.findByRole("heading", { name: "Fixture run" })).toBeInTheDocument();
  });
});

describe("backtest detail page", () => {
  it("shows the stored metrics honestly, the charts, the benchmark and trade details", async () => {
    stubApi(ROUTES);
    renderAt(`/backtesting/${RUN_ID}`);
    expect(await screen.findByRole("heading", { name: "Fixture run" })).toBeInTheDocument();
    // A risk-adjusted ratio on two sessions is withheld, not invented.
    const sharpe = (await screen.findByText("Sharpe")).closest("div")!;
    expect(within(sharpe).getByText("—")).toBeInTheDocument();
    expect(within(sharpe).getByText(/at least 126/)).toBeInTheDocument();
    await waitFor(() => expect(charts.series.length).toBeGreaterThanOrEqual(4));
    const equityLine = charts.series.find((s) => s.kind === "Line")!;
    expect(equityLine.data).toEqual([
      { time: "2026-10-01", value: 100000 },
      { time: "2026-10-02", value: 99876.5 },
    ]);
    expect(charts.series.some((s) => s.kind === "Area")).toBe(true);
    const bench = await screen.findByRole("table", { name: "Benchmarks" });
    expect(within(bench).getByText("SPY")).toBeInTheDocument();
    expect(within(bench).getByText("+1.00%")).toBeInTheDocument();
    const trades = await screen.findByRole("table", { name: "Trades" });
    fireEvent.click(within(trades).getByText("FIXA"));
    const fills = await screen.findByRole("table", { name: "Orders and fills" });
    expect(within(fills).getByText("87.9000")).toBeInTheDocument();
    expect(within(fills).getByText("gap")).toBeInTheDocument();
    expect(screen.getByText("fixture reasoning")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Strategy versions" })).toHaveTextContent("momentum-1");
  });

  it("monitors a running replay", async () => {
    stubApi({ ...ROUTES, [BASE]: { body: { ...DETAIL, status: "RUNNING", sessions_done: 1, progress: 0.5 } } });
    renderAt(`/backtesting/${RUN_ID}`);
    const bar = await screen.findByRole("progressbar", { name: "Replay progress" });
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    expect(screen.queryByRole("table", { name: "Trades" })).toBeNull();
  });
});
