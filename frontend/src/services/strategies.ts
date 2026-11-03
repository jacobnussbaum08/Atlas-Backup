import type {
  CatalogResponse,
  EvaluationQuery,
  EvaluationsResponse,
  FeatureRegistryResponse,
  ProposalQuery,
  ProposalsResponse,
  StrategyDetail,
} from "../types/strategies";
import { buildUrl, getJson } from "./api";

/** Typed clients for the Phase 4A Strategy Laboratory endpoints (read-only from the dashboard). */
type Signal = { signal?: AbortSignal };

export const strategies = {
  catalog: (opts: Signal = {}) => getJson<CatalogResponse>("/api/v1/strategies", opts),
  detail: (strategyId: string, opts: Signal = {}) =>
    getJson<StrategyDetail>(`/api/v1/strategies/${encodeURIComponent(strategyId)}`, opts),
  evaluations: (query: EvaluationQuery, opts: Signal = {}) =>
    getJson<EvaluationsResponse>(buildUrl("/api/v1/strategies/evaluations", { ...query }), opts),
  proposals: (query: ProposalQuery, opts: Signal = {}) =>
    getJson<ProposalsResponse>(buildUrl("/api/v1/strategies/proposals", { ...query }), opts),
  features: (opts: Signal = {}) => getJson<FeatureRegistryResponse>("/api/v1/strategies/features", opts),
};
