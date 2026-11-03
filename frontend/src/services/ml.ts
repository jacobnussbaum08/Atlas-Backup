import type {
  DatasetVersionSummary,
  MLOverview,
  ModelSummary,
  PredictionView,
  RunDetail,
  RunSummary,
} from "../types/ml";
import { getJson } from "./api";

/** Typed clients for the ML engine endpoints (read-only from the dashboard). */
type Signal = { signal?: AbortSignal };

export const ml = {
  overview: (opts: Signal = {}) => getJson<MLOverview>("/api/v1/ml/overview", opts),
  datasets: (opts: Signal = {}) => getJson<DatasetVersionSummary[]>("/api/v1/ml/datasets", opts),
  runs: (opts: Signal = {}) => getJson<RunSummary[]>("/api/v1/ml/runs", opts),
  run: (runId: string, opts: Signal = {}) =>
    getJson<RunDetail>(`/api/v1/ml/runs/${encodeURIComponent(runId)}`, opts),
  models: (opts: Signal = {}) => getJson<ModelSummary[]>("/api/v1/ml/models", opts),
  predictions: (opts: Signal = {}) => getJson<PredictionView[]>("/api/v1/ml/predictions", opts),
};
