import { Download, Search } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { Link } from "react-router";

import {
  AssetChips,
  CategoryBadge,
  formatSeconds,
  NewsJobStatusBadge,
  OriginBadge,
  SourceLink,
  StreamHealthBadge,
} from "../components/news";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  KeyValues,
  Loading,
  Notice,
  PageHeader,
  Panel,
  RefreshButton,
  Segmented,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { formatCount, humanize } from "../lib/format";
import { ApiError } from "../services/api";
import { news } from "../services/news";
import {
  type ArticleOrigin,
  type ArticleSummary,
  CATEGORY_LABELS,
  EVENT_CATEGORIES,
  type EventCategory,
  type FeedQuery,
} from "../types/news";

type Range = "24h" | "7d" | "30d" | "all";
const RANGES: readonly { value: Range; label: string }[] = [
  { value: "24h", label: "24 h" },
  { value: "7d", label: "7 d" },
  { value: "30d", label: "30 d" },
  { value: "all", label: "All" },
];
const RANGE_MS: Record<Exclude<Range, "all">, number> = {
  "24h": 86_400_000,
  "7d": 7 * 86_400_000,
  "30d": 30 * 86_400_000,
};
type OriginFilter = "ALL" | ArticleOrigin;
const ORIGINS: readonly { value: OriginFilter; label: string }[] = [
  { value: "ALL", label: "All" },
  { value: "REALTIME_STREAM", label: "Real-time" },
  { value: "HISTORICAL_REST", label: "Retrieved later" },
];
const PAGE_SIZE = 40;

interface Filters {
  symbol: string;
  source: string;
  category: "" | EventCategory;
  origin: OriginFilter;
  range: Range;
  q: string;
}

const INITIAL: Filters = { symbol: "", source: "", category: "", origin: "ALL", range: "7d", q: "" };

function rangeStart(range: Range): string | undefined {
  return range === "all" ? undefined : new Date(Date.now() - RANGE_MS[range]).toISOString();
}

function feedQuery(filters: Filters, cursor?: string): FeedQuery {
  return {
    symbol: filters.symbol || undefined,
    source: filters.source || undefined,
    category: filters.category || undefined,
    origin: filters.origin === "ALL" ? undefined : filters.origin,
    start: rangeStart(filters.range),
    q: filters.q || undefined,
    limit: PAGE_SIZE,
    cursor,
  };
}

export function NewsPage() {
  const [filters, setFilters] = useState<Filters>(INITIAL);
  const [refresh, setRefresh] = useState(0);
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="News & Events"
        description="Financial news as Atlas received it, mapped to assets and classified by versioned keyword rules. Supporting information only: categories are unverified estimates, nothing here measures sentiment or impact, and nothing is a trading signal."
        actions={<RefreshButton onClick={() => setRefresh((n) => n + 1)} />}
      />
      <StatusStrip refreshKey={refresh} />
      <div className="mt-3 grid gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0">
          <FilterBar filters={filters} onChange={setFilters} />
          <Feed filters={filters} refreshKey={refresh} />
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <CategoryPanel range={filters.range} refreshKey={refresh} />
          <JobsPanel refreshKey={refresh} onStarted={() => setRefresh((n) => n + 1)} />
        </div>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ status
function StatusStrip({ refreshKey }: { refreshKey: number }) {
  const providers = useApiResource((signal) => news.providers({ signal }), [refreshKey], { refreshMs: 30_000 });
  const stream = useApiResource((signal) => news.stream({ signal }), [refreshKey], { refreshMs: 15_000 });
  const calendar = useApiResource((signal) => news.calendar({ signal }), [refreshKey]);
  const provider = providers.data?.[0];
  return (
    <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3" aria-label="News provider status">
      <Panel title="Provider" subtitle={provider?.display_name}>
        {providers.status === "error" && <ErrorState message={providers.error ?? ""} onRetry={providers.reload} compact />}
        {providers.status === "loading" && <Loading />}
        {provider && (
          <KeyValues
            items={[
              {
                label: "Credentials",
                value: (
                  <Badge tone={provider.credentials === "CONFIGURED" ? "positive" : "caution"}>
                    {humanize(provider.credentials)}
                  </Badge>
                ),
              },
              { label: "Articles stored", value: <span className="num">{formatCount(provider.articles_stored)}</span> },
              { label: "Newest published", value: <Timestamp iso={provider.newest_article_published_at} /> },
              {
                label: "Last retrieval",
                value: provider.last_job ? (
                  <span className="inline-flex items-center gap-1.5">
                    <NewsJobStatusBadge status={provider.last_job.status} />
                    <Timestamp iso={provider.last_job.created_at} />
                  </span>
                ) : (
                  "none yet"
                ),
              },
              {
                label: "Article bodies",
                value: provider.store_content ? "stored (operator-enabled)" : "not stored",
                title: provider.content_policy,
              },
            ]}
          />
        )}
      </Panel>
      <Panel
        title="Real-time stream"
        actions={stream.data ? <StreamHealthBadge health={stream.data.health} /> : undefined}
      >
        {stream.status === "error" && <ErrorState message={stream.error ?? ""} onRetry={stream.reload} compact />}
        {stream.status === "loading" && <Loading />}
        {stream.data && (
          <>
            <KeyValues
              items={[
                { label: "State", value: stream.data.reasons.join("; ") || "—" },
                {
                  label: "Subscription",
                  value: stream.data.confirmed_symbols.length
                    ? stream.data.confirmed_symbols.join(", ").replace("*", "all news (*)")
                    : "—",
                },
                { label: "Last article", value: <>{formatSeconds(stream.data.last_article_age_seconds)} ago</> },
                { label: "Reconnects", value: <span className="num">{stream.data.reconnect_count}</span> },
                {
                  label: "Recovered",
                  value: (
                    <span className="num">
                      {formatCount(stream.data.counters["recovery_articles_new"] ?? 0)} new articles from{" "}
                      {stream.data.recovery_jobs.length === 1
                        ? "1 recovery job"
                        : `${formatCount(stream.data.recovery_jobs.length)} recovery jobs`}
                    </span>
                  ),
                },
              ]}
            />
            <p className="text-ink-faint mt-2 text-2xs">{stream.data.notes[0]}</p>
          </>
        )}
      </Panel>
      <Panel title="Economic calendar">
        {calendar.status === "error" && <ErrorState message={calendar.error ?? ""} onRetry={calendar.reload} compact />}
        {calendar.data && (
          <div className="flex flex-col gap-2 text-xs">
            <div>
              <Badge tone={calendar.data.availability === "AVAILABLE" ? "positive" : "caution"} dot>
                {humanize(calendar.data.availability)}
              </Badge>
            </div>
            <p>{calendar.data.reason}</p>
            {calendar.data.notes.slice(0, 1).map((note) => (
              <p key={note} className="text-ink-muted">
                {note}
              </p>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}

// ------------------------------------------------------------------ filters and feed
function FilterBar({ filters, onChange }: { filters: Filters; onChange: (f: Filters) => void }) {
  const sources = useApiResource((signal) => news.sources({ signal }), []);
  const [symbol, setSymbol] = useState(filters.symbol);
  const [q, setQ] = useState(filters.q);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onChange({ ...filters, symbol: symbol.trim().toUpperCase(), q: q.trim() });
  };
  return (
    <form
      onSubmit={submit}
      aria-label="News filters"
      className="border-border bg-panel mb-3 flex flex-wrap items-end gap-2 rounded-sm border px-3 py-2 text-xs"
    >
      <label className="flex flex-col gap-0.5">
        <span className="text-ink-muted text-2xs uppercase">Asset</span>
        <input
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          placeholder="Symbol, e.g. AAPL"
          aria-label="Asset symbol"
          maxLength={32}
          className="border-border-strong bg-surface focus:border-accent outline-none h-6 w-28 rounded-sm border px-1.5"
        />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-ink-muted text-2xs uppercase">Source</span>
        <select
          aria-label="Source"
          value={filters.source}
          onChange={(e) => onChange({ ...filters, source: e.target.value })}
          className="border-border-strong bg-surface focus:border-accent outline-none h-6 rounded-sm border px-1"
        >
          <option value="">All sources</option>
          {(sources.data ?? []).map((s) => (
            <option key={s.code} value={s.code}>
              {s.display_name} ({formatCount(s.articles)})
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-ink-muted text-2xs uppercase">Category</span>
        <select
          aria-label="Event category"
          value={filters.category}
          onChange={(e) => onChange({ ...filters, category: e.target.value as Filters["category"] })}
          className="border-border-strong bg-surface focus:border-accent outline-none h-6 rounded-sm border px-1"
        >
          <option value="">All categories</option>
          {EVENT_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {CATEGORY_LABELS[c]}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-ink-muted text-2xs uppercase">Headline</span>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="contains…"
          aria-label="Headline contains"
          maxLength={200}
          className="border-border-strong bg-surface focus:border-accent outline-none h-6 w-36 rounded-sm border px-1.5"
        />
      </label>
      <Button type="submit" aria-label="Apply filters">
        <Search aria-hidden className="size-3.5" />
        Apply
      </Button>
      <div className="flex flex-col gap-0.5">
        <span className="text-ink-muted text-2xs uppercase">Published</span>
        <Segmented label="Time range" options={RANGES} value={filters.range} onChange={(range) => onChange({ ...filters, range })} />
      </div>
      <div className="flex flex-col gap-0.5">
        <span className="text-ink-muted text-2xs uppercase">Received</span>
        <Segmented label="Origin" options={ORIGINS} value={filters.origin} onChange={(origin) => onChange({ ...filters, origin })} />
      </div>
    </form>
  );
}

function Feed({ filters, refreshKey }: { filters: Filters; refreshKey: number }) {
  const first = useApiResource((signal) => news.feed(feedQuery(filters), { signal }), [filters, refreshKey], {
    refreshMs: 60_000,
    isEmpty: (data) => data.articles.length === 0,
  });
  const [more, setMore] = useState<ArticleSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  useEffect(() => {
    setMore([]);
    setCursor(first.data?.next_cursor ?? null);
    setMoreError(null);
  }, [first.data]);

  const loadMore = () => {
    if (!cursor) return;
    setLoadingMore(true);
    news
      .feed(feedQuery(filters, cursor))
      .then((page) => {
        setMore((items) => [...items, ...page.articles]);
        setCursor(page.next_cursor);
      })
      .catch((reason: unknown) => setMoreError(reason instanceof Error ? reason.message : "Unknown error"))
      .finally(() => setLoadingMore(false));
  };

  const articles = [...(first.data?.articles ?? []), ...more];
  return (
    <Panel title="Headlines" subtitle={first.data ? `${articles.length} shown` : undefined} bodyClassName="">
      {first.status === "loading" && <Loading label="Loading news…" />}
      {first.status === "error" && (
        <div className="p-3">
          <ErrorState message={first.error ?? ""} onRetry={first.reload} />
        </div>
      )}
      {first.status === "empty" && (
        <EmptyState title="No articles match these filters.">
          Atlas shows only articles it has stored. Widen the time range, or run a historical
          retrieval (right) or the news stream worker to collect news.
        </EmptyState>
      )}
      {articles.length > 0 && (
        <ol aria-label="News articles" className="divide-border/60 divide-y">
          {articles.map((article) => (
            <ArticleRow key={article.article_id} article={article} />
          ))}
        </ol>
      )}
      {moreError && (
        <div className="p-3">
          <ErrorState message={moreError} onRetry={loadMore} compact />
        </div>
      )}
      {cursor && (
        <div className="border-border border-t p-2 text-center">
          <Button variant="ghost" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : "Load older articles"}
          </Button>
        </div>
      )}
      {first.data && (
        <p className="text-ink-faint border-border border-t px-3 py-2 text-2xs">{first.data.notes.join(" ")}</p>
      )}
    </Panel>
  );
}

function ArticleRow({ article }: { article: ArticleSummary }) {
  const categories = article.event?.categories ?? [];
  const primary = categories.find((c) => c.is_primary);
  const secondary = categories.filter((c) => !c.is_primary && c.category !== "OTHER");
  return (
    <li className="px-3 py-2.5" aria-label={article.headline}>
      <div className="flex flex-wrap items-center gap-1.5 text-2xs">
        <Timestamp iso={article.published_at} seconds={false} />
        <span className="text-ink-faint">·</span>
        <span className="font-medium">{article.source ?? "unknown source"}</span>
        {article.author && <span className="text-ink-muted">{article.author}</span>}
        <OriginBadge origin={article.first_origin} />
        {article.current_revision > 1 && (
          <Badge tone="info" title="The provider updated this article; every version is kept">
            rev {article.current_revision}
          </Badge>
        )}
      </div>
      <h3 className="mt-1 text-sm leading-snug font-medium">
        <Link to={`/news/articles/${article.article_id}`} className="hover:text-accent">
          {article.headline}
        </Link>
      </h3>
      {article.summary && <p className="text-ink-muted mt-1 line-clamp-2 text-xs">{article.summary}</p>}
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {primary && <CategoryBadge category={primary.category} view={primary} />}
        {secondary.map((c) => (
          <CategoryBadge key={`${c.source}-${c.category}`} category={c.category} view={c} />
        ))}
        <AssetChips links={article.assets} />
        <SourceLink url={article.url} />
      </div>
    </li>
  );
}

// ------------------------------------------------------------------ side panels
function CategoryPanel({ range, refreshKey }: { range: Range; refreshKey: number }) {
  const summary = useApiResource((signal) => news.summary(rangeStart(range === "all" ? "30d" : range), { signal }), [
    range,
    refreshKey,
  ]);
  const counts = summary.data
    ? EVENT_CATEGORIES.map((c) => [c, summary.data?.by_primary_category[c] ?? 0] as const).filter(([, n]) => n > 0)
    : [];
  const max = Math.max(1, ...counts.map(([, n]) => n));
  return (
    <Panel title="Primary categories" subtitle={range === "all" ? "last 30 d" : `last ${range}`}>
      {summary.status === "error" && <ErrorState message={summary.error ?? ""} onRetry={summary.reload} compact />}
      {summary.status === "loading" && <Loading />}
      {summary.data && (
        <>
          <p className="text-ink-muted mb-2 text-2xs">
            {formatCount(summary.data.articles)} articles ·{" "}
            {formatCount(summary.data.by_first_origin.REALTIME_STREAM ?? 0)} real-time ·{" "}
            {formatCount(summary.data.by_first_origin.HISTORICAL_REST ?? 0)} retrieved later
          </p>
          {counts.length === 0 ? (
            <p className="text-ink-faint text-xs">No articles in this window.</p>
          ) : (
            <ul aria-label="Category counts" className="flex flex-col gap-1 text-xs">
              {counts.map(([category, n]) => (
                <li key={category} className="grid grid-cols-[7.5rem_1fr_2.5rem] items-center gap-2">
                  <span className="truncate">{CATEGORY_LABELS[category]}</span>
                  <span className="bg-panel-raised h-1.5 rounded-sm">
                    <span className="bg-accent/70 block h-1.5 rounded-sm" style={{ width: `${(n / max) * 100}%` }} />
                  </span>
                  <span className="num text-right">{n}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="text-ink-faint mt-2 text-2xs">Keyword estimates (news-taxonomy-v1), not verified labels.</p>
        </>
      )}
    </Panel>
  );
}

function JobsPanel({ refreshKey, onStarted }: { refreshKey: number; onStarted: () => void }) {
  const jobs = useApiResource((signal) => news.jobs({ signal }), [refreshKey], { refreshMs: 10_000 });
  const [symbols, setSymbols] = useState("");
  const [days, setDays] = useState("1");
  const [token, setToken] = useState("");
  const [message, setMessage] = useState<{ tone: "positive" | "negative"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const span = Number(days);
    if (!Number.isFinite(span) || span <= 0 || span > 366) {
      setMessage({ tone: "negative", text: "Days must be between 0 and 366." });
      return;
    }
    const end = new Date();
    const start = new Date(end.getTime() - span * 86_400_000);
    setBusy(true);
    news
      .startBackfill(
        {
          symbols: symbols.split(",").map((s) => s.trim()).filter(Boolean),
          start: start.toISOString(),
          end: end.toISOString(),
        },
        token || undefined,
      )
      .then((job) => {
        setMessage({ tone: "positive", text: `Retrieval ${job.id.slice(0, 8)} started.` });
        onStarted();
      })
      .catch((reason: unknown) =>
        setMessage({
          tone: "negative",
          text: reason instanceof ApiError || reason instanceof Error ? reason.message : "Request failed",
        }),
      )
      .finally(() => setBusy(false));
  };
  return (
    <Panel title="Historical retrieval">
      <form onSubmit={submit} aria-label="Historical news retrieval" className="flex flex-col gap-2 text-xs">
        <label className="flex flex-col gap-0.5">
          <span className="text-ink-muted text-2xs uppercase">Symbols (empty = all news)</span>
          <input
            value={symbols}
            onChange={(e) => setSymbols(e.target.value)}
            placeholder="AAPL,MSFT"
            aria-label="Symbols to retrieve"
            className="border-border-strong bg-surface focus:border-accent outline-none h-6 rounded-sm border px-1.5"
          />
        </label>
        <div className="flex gap-2">
          <label className="flex flex-col gap-0.5">
            <span className="text-ink-muted text-2xs uppercase">Days back</span>
            <input
              value={days}
              onChange={(e) => setDays(e.target.value)}
              inputMode="decimal"
              aria-label="Days back"
              className="border-border-strong bg-surface focus:border-accent outline-none h-6 w-16 rounded-sm border px-1.5"
            />
          </label>
          <label className="flex flex-1 flex-col gap-0.5">
            <span className="text-ink-muted text-2xs uppercase">Operator token</span>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              aria-label="Operator token"
              className="border-border-strong bg-surface focus:border-accent outline-none h-6 rounded-sm border px-1.5"
            />
          </label>
        </div>
        <Button type="submit" disabled={busy}>
          <Download aria-hidden className="size-3.5" />
          Retrieve
        </Button>
        {message && <Notice tone={message.tone}>{message.text}</Notice>}
        <p className="text-ink-faint text-2xs">
          Retrieved articles are labelled as retrieved later: Atlas treats them as available from
          the retrieval time, not their publication time.
        </p>
      </form>
      <div className="mt-3">
        {jobs.status === "error" && <ErrorState message={jobs.error ?? ""} onRetry={jobs.reload} compact />}
        {jobs.data && jobs.data.length === 0 && <p className="text-ink-faint text-xs">No retrievals yet.</p>}
        {jobs.data && jobs.data.length > 0 && (
          <DataTable label="Recent news retrievals">
            <thead>
              <tr>
                <th className={TH}>Started</th>
                <th className={TH}>Kind</th>
                <th className={TH}>Status</th>
                <th className={`${TH} text-right`}>New</th>
                <th className={`${TH} text-right`}>Dup</th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.map((job) => (
                <tr key={job.id} title={job.error ?? `${job.symbols.join(",") || "all news"}`}>
                  <td className={TD}>
                    <Timestamp iso={job.created_at} seconds={false} />
                  </td>
                  <td className={TD}>{job.kind === "STREAM_RECOVERY" ? "recovery" : "backfill"}</td>
                  <td className={TD}>
                    <NewsJobStatusBadge status={job.status} />
                  </td>
                  <td className={`${TD} num text-right`}>{formatCount(job.articles_new)}</td>
                  <td className={`${TD} num text-right`}>{formatCount(job.duplicates)}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </div>
    </Panel>
  );
}
