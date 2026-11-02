import type {
  AssetDetail,
  AssetHistory,
  AssetStatus,
  AssetSummary,
  DiscoveryConfig,
  DiscoveryJob,
  DiscoveryJobKind,
  DiscoverySummary,
  JobRequestResponse,
  Page,
  QualificationHistory,
  QualificationOutcome,
  SortKey,
  UniverseLayer,
} from "../types/discovery";
import { buildUrl, getJson, postJson } from "./api";

/** Typed clients for the Phase 3A asset discovery endpoints (reads; operator-gated runs). */
type Signal = { signal?: AbortSignal };

export interface AssetQuery {
  search?: string;
  status?: AssetStatus[];
  exchange?: string[];
  outcome?: QualificationOutcome | "";
  sort?: SortKey;
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

/** Endpoint per layer; ``null`` = every catalogued instrument. */
export const LAYER_PATHS: Record<UniverseLayer | "ALL", string> = {
  DISCOVERY_POOL: "/api/v1/assets/discovery-pool",
  QUALIFIED_UNIVERSE: "/api/v1/assets/qualified",
  ACTIVE_WATCHLIST: "/api/v1/assets/watchlist",
  ALL: "/api/v1/assets",
};

export const discovery = {
  summary: (opts: Signal = {}) => getJson<DiscoverySummary>("/api/v1/discovery/summary", opts),
  assets: (layer: UniverseLayer | "ALL", query: AssetQuery, opts: Signal = {}) =>
    getJson<Page<AssetSummary>>(buildUrl(LAYER_PATHS[layer], { ...query }), opts),
  asset: (assetId: string, opts: Signal = {}) => getJson<AssetDetail>(`/api/v1/assets/${assetId}`, opts),
  qualification: (assetId: string, opts: Signal = {}) =>
    getJson<QualificationHistory>(`/api/v1/assets/${assetId}/qualification`, opts),
  history: (assetId: string, opts: Signal = {}) => getJson<AssetHistory>(`/api/v1/assets/${assetId}/history`, opts),
  jobs: (query: { kind?: DiscoveryJobKind[]; limit?: number }, opts: Signal = {}) =>
    getJson<Page<DiscoveryJob>>(buildUrl("/api/v1/discovery/jobs", { ...query }), opts),
  config: (opts: Signal = {}) => getJson<DiscoveryConfig>("/api/v1/discovery/config", opts),
  run: (body: { mode: "FULL" | "INCREMENTAL"; pipeline: boolean }, operatorToken?: string) =>
    postJson<JobRequestResponse>("/api/v1/discovery/run", body, operatorToken ? { operatorToken } : {}),
};
