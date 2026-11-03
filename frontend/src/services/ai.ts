import type {
  AIOverview,
  AnalysisView,
  DecisionDetail,
  EvaluationView,
  Rankings,
  RunDetail,
  RunSummary,
  UsageReport,
} from "../types/ai";
import { getJson } from "./api";

/** Typed clients for the decision-engine endpoints (all read-only). */
type Signal = { signal?: AbortSignal };

export const ai = {
  overview: (opts: Signal = {}) => getJson<AIOverview>("/api/v1/ai/overview", opts),
  rankings: (opts: Signal = {}) => getJson<Rankings>("/api/v1/ai/rankings", opts),
  runs: (opts: Signal = {}) => getJson<RunSummary[]>("/api/v1/ai/runs", opts),
  run: (runId: string, opts: Signal = {}) =>
    getJson<RunDetail>(`/api/v1/ai/runs/${encodeURIComponent(runId)}`, opts),
  decision: (decisionId: string, opts: Signal = {}) =>
    getJson<DecisionDetail>(`/api/v1/ai/decisions/${encodeURIComponent(decisionId)}`, opts),
  analyses: (opts: Signal = {}) => getJson<AnalysisView[]>("/api/v1/ai/llm/analyses", opts),
  usage: (opts: Signal = {}) => getJson<UsageReport>("/api/v1/ai/llm/usage", opts),
  evaluations: (opts: Signal = {}) => getJson<EvaluationView[]>("/api/v1/ai/evaluations", opts),
};
