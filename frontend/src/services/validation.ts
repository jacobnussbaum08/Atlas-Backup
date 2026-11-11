import type {
  Approval,
  Candidate,
  ChampionRow,
  ExperimentBody,
  Fold,
  HoldoutEvaluation,
  ModelGroup,
  MonitoringView,
  RobustnessRow,
  SegmentRow,
  Trial,
  ValidationOptions,
  ValidationOverview,
  ValidationRun,
  ValidationRunDetail,
} from "../types/validation";
import { buildUrl, getJson, postJson } from "./api";

/**
 * Typed clients for the Phase 8B validation laboratory. Reads return stored records only.
 * The operator-gated writes create or reproduce a run, read a run's holdout once, and record
 * explicit lifecycle decisions; nothing here promotes, orders or executes anything.
 */
type Opts = { signal?: AbortSignal };
const base = "/api/v1/validation";
const run = (id: string) => `${base}/runs/${encodeURIComponent(id)}`;
const token = (t?: string) => (t ? { operatorToken: t } : {});

export const validation = {
  overview: (o: Opts = {}) => getJson<ValidationOverview>(`${base}/overview`, o),
  options: (o: Opts = {}) => getJson<ValidationOptions>(`${base}/options`, o),
  runs: (o: Opts = {}) => getJson<ValidationRun[]>(buildUrl(`${base}/runs`, { limit: 100 }), o),
  detail: (id: string, o: Opts = {}) => getJson<ValidationRunDetail>(run(id), o),
  folds: (id: string, o: Opts = {}) => getJson<Fold[]>(`${run(id)}/folds`, o),
  trials: (id: string, o: Opts = {}) => getJson<Trial[]>(`${run(id)}/trials`, o),
  robustness: (id: string, o: Opts = {}) => getJson<RobustnessRow[]>(`${run(id)}/robustness`, o),
  segments: (id: string, o: Opts = {}) => getJson<SegmentRow[]>(`${run(id)}/segments`, o),
  models: (o: Opts = {}) => getJson<ModelGroup[]>(`${base}/models/compare`, o),
  champions: (o: Opts = {}) => getJson<ChampionRow[]>(`${base}/champions`, o),
  candidates: (o: Opts = {}) => getJson<Candidate[]>(`${base}/candidates`, o),
  promotions: (o: Opts = {}) => getJson<Approval[]>(`${base}/promotions`, o),
  monitoring: (o: Opts = {}) => getJson<MonitoringView>(`${base}/monitoring`, o),
  create: (body: ExperimentBody, operatorToken?: string) =>
    postJson<ValidationRunDetail>(`${base}/runs`, body, token(operatorToken)),
  reproduce: (id: string, operatorToken?: string) =>
    postJson<ValidationRunDetail>(`${run(id)}/reproduce`, {}, token(operatorToken)),
  holdout: (id: string, operatorToken?: string) =>
    postJson<HoldoutEvaluation>(`${run(id)}/holdout`, {}, token(operatorToken)),
};
