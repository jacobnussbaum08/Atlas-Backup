import type {
  BacktestDetail,
  BacktestOptions,
  BacktestRequestBody,
  BacktestRun,
  BenchmarkRow,
  CompareResult,
  EquityRow,
  EvaluationCount,
  MetricRow,
  ProposalRow,
  RejectionRow,
  RunErrorRow,
  RunEventRow,
  TradeDetail,
  TradeRow,
} from "../types/backtests";
import { buildUrl, getJson, postJson } from "./api";

/**
 * Typed clients for the Phase 8A backtesting endpoints. Two operator-gated writes (create a
 * run, reproduce a completed one); everything else reads stored replay results. A backtest
 * is a simulation over stored historical data: nothing here orders or executes anything.
 */
type Opts = { signal?: AbortSignal };
const run = (id: string) => `/api/v1/backtests/${encodeURIComponent(id)}`;

export const backtests = {
  options: (opts: Opts = {}) => getJson<BacktestOptions>("/api/v1/backtests/options", opts),
  list: (opts: Opts = {}) => getJson<BacktestRun[]>(buildUrl("/api/v1/backtests", { limit: 100 }), opts),
  detail: (id: string, opts: Opts = {}) => getJson<BacktestDetail>(run(id), opts),
  equity: (id: string, opts: Opts = {}) => getJson<EquityRow[]>(`${run(id)}/equity`, opts),
  metrics: (id: string, opts: Opts = {}) => getJson<MetricRow[]>(`${run(id)}/metrics`, opts),
  benchmarks: (id: string, opts: Opts = {}) => getJson<BenchmarkRow[]>(`${run(id)}/benchmarks`, opts),
  trades: (id: string, opts: Opts = {}) => getJson<TradeRow[]>(`${run(id)}/trades`, opts),
  trade: (id: string, seq: number, opts: Opts = {}) =>
    getJson<TradeDetail>(`${run(id)}/trades/${seq}`, opts),
  proposals: (id: string, opts: Opts = {}) => getJson<ProposalRow[]>(`${run(id)}/proposals`, opts),
  rejections: (id: string, opts: Opts = {}) => getJson<RejectionRow[]>(`${run(id)}/rejections`, opts),
  evaluations: (id: string, opts: Opts = {}) =>
    getJson<EvaluationCount[]>(`${run(id)}/evaluations`, opts),
  events: (id: string, opts: Opts = {}) => getJson<RunEventRow[]>(`${run(id)}/events`, opts),
  errors: (id: string, opts: Opts = {}) => getJson<RunErrorRow[]>(`${run(id)}/errors`, opts),
  compare: (ids: string[], opts: Opts = {}) =>
    getJson<CompareResult>(buildUrl("/api/v1/backtests/compare", { run_ids: ids }), opts),
  create: (body: BacktestRequestBody, operatorToken?: string) =>
    postJson<BacktestDetail>("/api/v1/backtests", body, operatorToken ? { operatorToken } : {}),
  reproduce: (id: string, operatorToken?: string) =>
    postJson<BacktestDetail>(`${run(id)}/reproduce`, {}, operatorToken ? { operatorToken } : {}),
};
