// Mirrors of the Phase 8A backtesting API (backend/app/backtesting/queries.py). Decimals
// stay strings. Every number shown comes from a stored replay of historical data.

export type RunStatus = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
export type MetricStatus = "OK" | "INSUFFICIENT_SAMPLE" | "NOT_APPLICABLE";

export interface BacktestRun {
  id: string;
  name: string;
  status: RunStatus;
  simulated: boolean;
  start_date: string;
  end_date: string;
  initial_capital: string;
  currency: string;
  strategies: string[];
  universe_mode: string;
  symbols: string[];
  asset_count: number;
  benchmark_symbol: string | null;
  seed: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  sessions_total: number;
  sessions_done: number;
  progress: number | null;
  simulated_through: string | null;
  result_sha256: string | null;
  reproduction_of: string | null;
  reproduction_matches: boolean | null;
  error_stage: string | null;
  error_message: string | null;
  summary: Record<string, unknown> | null;
  requested_by: string;
}

export interface StrategyVersionRow {
  strategy_id: string;
  strategy_version_id: string;
  version: number;
  logic_version: string;
  parameters: Record<string, unknown>;
  parameters_sha256: string;
  parameters_source: string;
  timeframes: string[];
}

export interface BacktestDetail extends BacktestRun {
  request: Record<string, unknown>;
  config: Record<string, unknown> & {
    policies?: Record<string, string>;
    model_usage?: string;
    risk?: { hard_limits?: Record<string, unknown>; preferences?: Record<string, unknown> };
    execution?: Record<string, unknown>;
    data?: Record<string, unknown>;
  };
  config_sha256: string;
  engine_version: string;
  code_version: string;
  data_provider: string;
  data_feed: string;
  data_known_at: string;
  dataset_sha256: string | null;
  dataset_bars: number | null;
  execution_model_id: number;
  execution_model_sha256: string;
  risk_sha256: string;
  risk_source: string;
  strategy_versions: StrategyVersionRow[];
  errors: number;
}

export interface EquityRow {
  session: string;
  at: string;
  equity: string;
  gross_equity: string;
  cash: string;
  long_exposure: string;
  short_exposure: string;
  realized_pnl: string;
  unrealized_pnl: string;
  total_costs: string;
  drawdown: string;
  drawdown_pct: string;
  open_positions: number;
  details: Record<string, unknown>;
}

export interface MetricRow {
  scope: "PORTFOLIO" | "DIRECTION" | "STRATEGY" | "ASSET";
  scope_key: string;
  metric: string;
  value: string | null;
  unit: string;
  status: MetricStatus;
  sample_size: number;
  detail: string;
}

export interface BenchmarkRow {
  kind: "BENCHMARK" | "BUY_AND_HOLD";
  label: string;
  available: boolean;
  total_return: string | null;
  annualized_return: string | null;
  max_drawdown_pct: string | null;
  excess_return: string | null;
  details: { series?: [string, string][]; notes?: string[]; relation?: Record<string, unknown> };
}

export interface TradeRow {
  seq: number;
  proposal_seq: number;
  strategy_id: string;
  timeframe: string;
  asset_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  status: "CLOSED" | "OPEN_AT_END";
  opened_at: string;
  closed_at: string | null;
  entry_quantity: string;
  entry_price: string | null;
  exit_quantity: string;
  exit_price: string | null;
  exit_reason: string | null;
  realized_gross: string;
  unrealized: string;
  costs: string;
  net_pnl: string;
  holding_sessions: number | null;
  details: Record<string, unknown>;
}

export interface OrderRow {
  seq: number;
  role: string;
  side: string;
  order_type: string;
  quantity: string;
  filled_quantity: string;
  average_price: string | null;
  status: string;
  arrival_at: string;
  expires_at: string | null;
  commission: string;
  fees: string;
  details: { events?: { event: string; at: string; status: string; reason: string }[] } & Record<
    string,
    unknown
  >;
}

export interface FillRow {
  seq: number;
  order_seq: number;
  side: string;
  role: string;
  session: string;
  quantity: string;
  price: string;
  notional: string;
  commission: string;
  fees: string;
  basis: string;
  details: { gap?: boolean; assumptions?: string[]; slippage_bps?: string | null } & Record<
    string,
    unknown
  >;
}

export interface ProposalRow {
  seq: number;
  session: string;
  strategy_id: string;
  timeframe: string;
  symbol: string;
  direction: string;
  reference_entry: string;
  invalidation_level: string | null;
  target_reference: string | null;
  signal_strength: string;
  risk_result: string | null;
  permitted_quantity: string | null;
  binding_constraint: string | null;
  outcome: string | null;
  details: Record<string, unknown>;
}

export interface TradeDetail {
  trade: TradeRow;
  proposal: ProposalRow | null;
  orders: OrderRow[];
  fills: FillRow[];
}

export interface RejectionRow {
  seq: number;
  at: string;
  stage: string;
  strategy_id: string;
  symbol: string;
  direction: string | null;
  code: string;
  message: string;
}

export interface EvaluationCount {
  strategy_id: string;
  timeframe: string;
  state: string;
  count: number;
}

export interface RunEventRow {
  seq: number;
  at: string;
  kind: string;
  symbol: string | null;
  message: string;
}

export interface RunErrorRow {
  seq: number;
  at: string | null;
  stage: string;
  fatal: boolean;
  message: string;
}

export interface StrategyOption {
  strategy_id: string;
  family: string;
  name: string;
  implemented: boolean;
  supported_timeframes: string[];
  latest_version: number | null;
  logic_version: string;
  replayable: boolean;
}

export interface AssetOption {
  asset_id: string;
  symbol: string;
  asset_class: string;
  raw: { first: string; last: string; bars: number } | null;
  split: { first: string; last: string; bars: number } | null;
}

export interface BacktestOptions {
  strategies: StrategyOption[];
  timeframes: string[];
  universe_modes: string[];
  layers: string[];
  assets: AssetOption[];
  data: { provider: string; feed: string };
}

export interface CompareResult {
  runs: {
    run: BacktestRun;
    portfolio: Record<string, { value: string | null; status: MetricStatus; sample_size: number }>;
    strategies: Record<string, Record<string, { value: string | null; status: MetricStatus }>>;
    benchmarks: { kind: string; label: string; total_return: string | null; excess_return: string | null }[];
  }[];
  metrics: string[];
}

export interface BacktestRequestBody {
  name: string;
  start_date: string;
  end_date: string;
  initial_capital: string;
  universe: { mode: "EXPLICIT" | "POINT_IN_TIME"; symbols?: string[]; layer?: string };
  strategies: { strategy_id: string; version?: number }[];
  timeframes?: string[] | null;
  directions?: string[] | null;
  benchmark_symbol: string | null;
  execution?: Record<string, unknown>;
  seed: number;
}
