/**
 * Phase 3C news and event API types (backend: app/api/v1/news.py, app/news/queries.py).
 * All text fields arrive as plain, sanitized text; they are always rendered as text.
 */
export type IsoTime = string;

export type ArticleOrigin = "REALTIME_STREAM" | "HISTORICAL_REST";
export type MappingStatus = "MAPPED" | "UNKNOWN_SYMBOL" | "INVALID_SYMBOL" | "NOT_EFFECTIVE" | "AMBIGUOUS";
export type EventScope = "ASSET" | "MARKET" | "UNLINKED";
export type NewsJobStatus = "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
export type NewsJobKind = "HISTORICAL_BACKFILL" | "STREAM_RECOVERY";
export type StreamHealthState =
  | "NOT_RUNNING"
  | "DISABLED"
  | "STOPPED"
  | "FAILED"
  | "CONNECTING"
  | "DEGRADED"
  | "STREAMING";

export const EVENT_CATEGORIES = [
  "EARNINGS",
  "ANALYST_RATINGS",
  "MERGERS_ACQUISITIONS",
  "CORPORATE_ACTIONS",
  "MANAGEMENT_CHANGES",
  "PRODUCT_LAUNCHES",
  "REGULATORY",
  "LITIGATION",
  "CENTRAL_BANKS",
  "INFLATION",
  "EMPLOYMENT",
  "MACROECONOMIC",
  "GEOPOLITICAL",
  "CRYPTOCURRENCY",
  "INDUSTRY",
  "COMPANY_ANNOUNCEMENTS",
  "OTHER",
] as const;
export type EventCategory = (typeof EVENT_CATEGORIES)[number];

export const CATEGORY_LABELS: Record<EventCategory, string> = {
  EARNINGS: "Earnings",
  ANALYST_RATINGS: "Analyst ratings",
  MERGERS_ACQUISITIONS: "M&A",
  CORPORATE_ACTIONS: "Corporate actions",
  MANAGEMENT_CHANGES: "Management",
  PRODUCT_LAUNCHES: "Product launches",
  REGULATORY: "Regulatory",
  LITIGATION: "Litigation",
  CENTRAL_BANKS: "Central banks",
  INFLATION: "Inflation",
  EMPLOYMENT: "Employment",
  MACROECONOMIC: "Macroeconomic",
  GEOPOLITICAL: "Geopolitical",
  CRYPTOCURRENCY: "Crypto",
  INDUSTRY: "Industry",
  COMPANY_ANNOUNCEMENTS: "Company news",
  OTHER: "Unclassified",
};

export interface AssetLink {
  provider_symbol: string;
  status: MappingStatus;
  asset_id: string | null;
  asset_symbol: string | null;
  asset_status: string | null;
  reason: string;
  mapping_version: string;
}

export interface CategoryView {
  category: EventCategory;
  source: "ATLAS" | "PROVIDER";
  method: string;
  confidence: "MEDIUM" | "LOW" | "NONE";
  verification: "UNVERIFIED" | "PROVIDER_ASSERTED";
  is_primary: boolean;
  score: number;
  provider_label: string | null;
  evidence: { matches?: { field: string; rule: string; text: string }[] } & Record<string, unknown>;
}

export interface RelevanceView {
  asset_id: string;
  asset_symbol: string | null;
  version: string;
  static_relevance: string;
  recency_weight: number;
  combined: number;
  factors: Record<string, unknown>;
}

export interface EventView {
  event_id: number;
  primary_category: EventCategory;
  scope: EventScope;
  taxonomy_version: string;
  classifier_version: string;
  rules_sha256: string;
  event_time: IsoTime;
  available_at: IsoTime;
  categories: CategoryView[];
}

export interface ArticleSummary {
  article_id: string;
  provider: string;
  provider_article_id: string;
  revision: number;
  current_revision: number;
  headline: string;
  summary: string | null;
  author: string | null;
  source: string | null;
  url: string | null;
  symbols: string[];
  published_at: IsoTime;
  provider_updated_at: IsoTime | null;
  first_received_at: IsoTime;
  first_available_at: IsoTime;
  first_origin: ArticleOrigin;
  first_live_received_at: IsoTime | null;
  revision_origin: ArticleOrigin;
  revision_received_at: IsoTime;
  revision_available_at: IsoTime;
  original_available_at: IsoTime | null;
  assets: AssetLink[];
  event: EventView | null;
  relevance: RelevanceView[];
}

export interface FeedResponse {
  articles: ArticleSummary[];
  next_cursor: string | null;
  known_at: IsoTime | null;
  as_of: IsoTime;
  notes: string[];
}

export interface RevisionView {
  revision: number;
  reason: string;
  origin: ArticleOrigin;
  headline: string;
  summary: string | null;
  author: string | null;
  source: string | null;
  url: string | null;
  symbols: string[];
  published_at: IsoTime;
  provider_updated_at: IsoTime | null;
  received_at: IsoTime;
  processed_at: IsoTime;
  available_at: IsoTime;
  original_available_at: IsoTime | null;
  content_sha256: string;
  has_body: boolean;
  body_sha256: string | null;
  content: string | null;
  images: { size: string; url: string }[];
  provider_categories: string[];
  ingestion_job_id: string | null;
  stream_session_id: string | null;
  assets: AssetLink[];
  events: EventView[];
}

export interface ArticleDetail {
  article: ArticleSummary;
  observation_count: number;
  last_seen_at: IsoTime;
  revisions: RevisionView[];
  provider_payload: Record<string, unknown>;
}

export interface NewsJob {
  id: string;
  provider: string;
  kind: NewsJobKind;
  status: NewsJobStatus;
  symbols: string[];
  range_start: IsoTime;
  range_end: IsoTime;
  include_content: boolean;
  requested_by: string;
  stream_session_id: string | null;
  params: Record<string, unknown>;
  pages_retrieved: number;
  articles_received: number;
  articles_new: number;
  revisions_new: number;
  duplicates: number;
  malformed: number;
  error: string | null;
  created_at: IsoTime;
  started_at: IsoTime | null;
  heartbeat_at: IsoTime | null;
  completed_at: IsoTime | null;
}

export interface NewsSource {
  provider: string;
  code: string;
  display_name: string;
  reliability: string;
  first_seen_at: IsoTime;
  last_seen_at: IsoTime;
  articles: number;
}

export interface NewsStreamHealth {
  provider: string;
  health: StreamHealthState;
  reasons: string[];
  enabled: boolean | null;
  connection_state: string | null;
  auth_state: string | null;
  subscription_state: string | null;
  requested_symbols: string[];
  confirmed_symbols: string[];
  worker_id: string | null;
  worker_started_at: IsoTime | null;
  heartbeat_at: IsoTime | null;
  heartbeat_age_seconds: number | null;
  last_subscribed_at: IsoTime | null;
  last_message_at: IsoTime | null;
  last_article_at: IsoTime | null;
  last_article_age_seconds: number | null;
  last_persisted_at: IsoTime | null;
  last_error: string | null;
  last_error_at: IsoTime | null;
  recent_errors: { at: string; kind: string; code: string | null; message: string }[];
  reconnect_count: number;
  counters: Record<string, number>;
  recovery_jobs: NewsJob[];
  notes: string[];
}

export interface NewsProviderStatus {
  provider: string;
  display_name: string;
  registered: boolean;
  credentials: string;
  authentication: Record<string, unknown> | null;
  capabilities: Record<string, unknown>;
  content_policy: string;
  store_content: boolean;
  articles_stored: number;
  newest_article_published_at: IsoTime | null;
  newest_article_received_at: IsoTime | null;
  last_job: NewsJob | null;
  last_failed_job: NewsJob | null;
  stream_health: StreamHealthState;
  notes: string[];
}

export interface CalendarStatus {
  availability: "NOT_CONFIGURED" | "AVAILABLE";
  provider: string | null;
  reason: string;
  notes: string[];
  events_stored: number;
}

export interface NewsSummary {
  start: IsoTime;
  end: IsoTime;
  articles: number;
  by_primary_category: Partial<Record<EventCategory, number>>;
  by_first_origin: Partial<Record<ArticleOrigin, number>>;
  notes: string[];
}

export interface FeedQuery {
  symbol?: string | undefined;
  source?: string | undefined;
  category?: EventCategory | undefined;
  origin?: ArticleOrigin | undefined;
  start?: string | undefined;
  q?: string | undefined;
  limit?: number;
  cursor?: string | undefined;
}
