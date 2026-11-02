import type {
  AssetSnapshot,
  AuditEvent,
  CandlesResponse,
  CompletenessResponse,
  DataGap,
  GapStatus,
  HistoricalFetchRequest,
  IngestionJob,
  IngestionJobStatus,
  MarketAsset,
  MarketDataConfiguration,
  MarketStatus,
  PipelineHealth,
  ProviderInfo,
  QualityEvent,
  QuotesResponse,
  RecoveryJob,
  RecoveryJobStatus,
  SnapshotsResponse,
  StreamGap,
  StreamMetrics,
  StreamStatus,
  TradesResponse,
} from "../types/marketData";
import { buildUrl, getJson, postJson } from "./api";

/** Typed clients for the existing /api/v1/market-data endpoints. */
const BASE = "/api/v1/market-data";

type Signal = { signal?: AbortSignal };

const get = <T>(path: string, query: Parameters<typeof buildUrl>[1], opts: Signal = {}) =>
  getJson<T>(buildUrl(`${BASE}${path}`, query), opts);

export const marketData = {
  marketStatus: (opts?: Signal) => get<MarketStatus>("/market-status", {}, opts),
  snapshots: (symbols?: string[], opts?: Signal) =>
    get<SnapshotsResponse>("/snapshots", { symbol: symbols }, opts),
  assets: (opts?: Signal) => get<MarketAsset[]>("/assets", {}, opts),
  providers: (opts?: Signal) => get<ProviderInfo[]>("/providers", {}, opts),
  configuration: (opts?: Signal) => get<MarketDataConfiguration>("/configuration", {}, opts),
  stream: (opts?: Signal) => get<StreamStatus>("/stream", {}, opts),
  streamGaps: (query: { open_only?: boolean; limit?: number; offset?: number }, opts?: Signal) =>
    get<StreamGap[]>("/stream/gaps", query, opts),
  pipelineHealth: (opts?: Signal) => get<PipelineHealth>("/pipeline-health", {}, opts),
  metrics: (opts?: Signal) => get<StreamMetrics>("/metrics", {}, opts),
  candles: (
    query: {
      symbol: string;
      timeframe: string;
      start: string;
      end: string;
      quality?: readonly string[];
      after?: string;
      limit?: number;
    },
    opts?: Signal,
  ) => get<CandlesResponse>("/candles", query, opts),
  trades: (
    query: { symbol: string; start: string; end: string; limit?: number; newest_first?: boolean },
    opts?: Signal,
  ) => get<TradesResponse>("/trades", query, opts),
  quotes: (
    query: { symbol: string; start: string; end: string; limit?: number; newest_first?: boolean },
    opts?: Signal,
  ) => get<QuotesResponse>("/quotes", query, opts),
  completeness: (
    query: { symbol: string; start: string; end: string; data_type?: string; timeframe?: string },
    opts?: Signal,
  ) => get<CompletenessResponse>("/completeness", query, opts),
  gaps: (
    query: {
      status?: readonly GapStatus[];
      symbol?: string;
      data_type?: string;
      limit?: number;
      before_id?: number;
    },
    opts?: Signal,
  ) => get<DataGap[]>("/gaps", query, opts),
  recoveryJobs: (query: { status?: readonly RecoveryJobStatus[]; limit?: number }, opts?: Signal) =>
    get<RecoveryJob[]>("/recovery-jobs", query, opts),
  ingestionJobs: (
    query: {
      status?: readonly IngestionJobStatus[];
      symbol?: string;
      timeframe?: string;
      created_from?: string;
      created_to?: string;
      limit?: number;
      offset?: number;
    },
    opts?: Signal,
  ) => get<IngestionJob[]>("/ingestion-jobs", query, opts),
  ingestionJob: (jobId: string, opts?: Signal) =>
    get<IngestionJob>(`/ingestion-jobs/${encodeURIComponent(jobId)}`, {}, opts),
  qualityEvents: (
    query: { asset_id?: string; event_type?: readonly string[]; limit?: number; offset?: number },
    opts?: Signal,
  ) => get<QualityEvent[]>("/quality-events", query, opts),
  auditEvents: (
    query: { priority?: "NORMAL" | "HIGH"; limit?: number; before_id?: number },
    opts?: Signal,
  ) => get<AuditEvent[]>("/audit-events", query, opts),
  submitHistoricalFetch: (body: HistoricalFetchRequest, operatorToken?: string) =>
    postJson<IngestionJob>(
      `${BASE}/historical-fetch`,
      body,
      operatorToken ? { operatorToken } : {},
    ),
};

export type { AssetSnapshot };
