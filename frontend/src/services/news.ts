import type {
  ArticleDetail,
  CalendarStatus,
  FeedQuery,
  FeedResponse,
  NewsJob,
  NewsProviderStatus,
  NewsSource,
  NewsStreamHealth,
  NewsSummary,
} from "../types/news";
import { buildUrl, getJson, postJson } from "./api";

/** Typed clients for the Phase 3C news endpoints (reads; operator-gated historical retrieval). */
type Signal = { signal?: AbortSignal };

export const news = {
  feed: (query: FeedQuery, opts: Signal = {}) =>
    getJson<FeedResponse>(buildUrl("/api/v1/news/articles", { ...query }), opts),
  article: (articleId: string, opts: Signal = {}) =>
    getJson<ArticleDetail>(`/api/v1/news/articles/${encodeURIComponent(articleId)}`, opts),
  summary: (start: string | undefined, opts: Signal = {}) =>
    getJson<NewsSummary>(buildUrl("/api/v1/news/summary", { start }), opts),
  sources: (opts: Signal = {}) => getJson<NewsSource[]>("/api/v1/news/sources", opts),
  providers: (opts: Signal = {}) => getJson<NewsProviderStatus[]>("/api/v1/news/providers", opts),
  stream: (opts: Signal = {}) => getJson<NewsStreamHealth>("/api/v1/news/stream", opts),
  calendar: (opts: Signal = {}) => getJson<CalendarStatus>("/api/v1/news/economic-calendar", opts),
  jobs: (opts: Signal = {}) => getJson<NewsJob[]>(buildUrl("/api/v1/news/ingestion-jobs", { limit: 8 }), opts),
  startBackfill: (body: { symbols: string[]; start: string; end: string }, operatorToken?: string) =>
    postJson<NewsJob>("/api/v1/news/ingestion-jobs", body, operatorToken ? { operatorToken } : {}),
};

/** An external link target, only for absolute http(s) URLs (the API already enforces this). */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

export function hostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
