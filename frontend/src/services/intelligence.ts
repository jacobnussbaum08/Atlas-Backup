import type {
  AssetList,
  AssetSort,
  Correlations,
  IntelligenceRun,
  MarketConditions,
  RegimeHistory,
  RunKind,
  Timeframe,
} from "../types/intelligence";
import { buildUrl, getJson, postJson } from "./api";

/** Typed clients for the Phase 3B market intelligence endpoints (reads; operator-gated runs). */
type Signal = { signal?: AbortSignal };

export const intelligence = {
  market: (opts: Signal = {}) => getJson<MarketConditions>("/api/v1/intelligence/market", opts),
  history: (timeframe: Timeframe, opts: Signal = {}) =>
    getJson<RegimeHistory>(buildUrl("/api/v1/intelligence/regimes/history", { timeframe }), opts),
  assets: (query: { timeframe: Timeframe; sort: AssetSort; limit?: number }, opts: Signal = {}) =>
    getJson<AssetList>(buildUrl("/api/v1/intelligence/assets", { ...query }), opts),
  correlations: (opts: Signal = {}) => getJson<Correlations>("/api/v1/intelligence/correlations", opts),
  runs: (opts: Signal = {}) =>
    getJson<IntelligenceRun[]>(buildUrl("/api/v1/intelligence/runs", { limit: 10 }), opts),
  start: (body: { kind: RunKind; sessions?: number }, operatorToken?: string) =>
    postJson<IntelligenceRun>("/api/v1/intelligence/runs", body, operatorToken ? { operatorToken } : {}),
};
