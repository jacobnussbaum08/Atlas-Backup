import { ChevronLeft, ChevronRight, Play } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { Link } from "react-router";

import {
  AssetStatusBadge,
  DiscoveryJobBadge,
  Flag,
  InstrumentTypeLabel,
  LAYER_LABELS,
  OutcomeBadge,
} from "../components/discovery";
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
  Stat,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { type Resource, useApiResource } from "../hooks/useApiResource";
import { formatCount, humanize, MISSING } from "../lib/format";
import { ApiError } from "../services/api";
import { type AssetQuery, discovery } from "../services/discovery";
import type {
  AssetStatus,
  AssetSummary,
  DiscoveryJobKind,
  DiscoverySummary,
  JobRequestResponse,
  QualificationOutcome,
  SortKey,
  UniverseLayer,
} from "../types/discovery";

const PAGE_SIZE = 50;
type Tab = UniverseLayer | "ALL";
const TABS: readonly { value: Tab; label: string }[] = [
  { value: "DISCOVERY_POOL", label: "Discovery pool" },
  { value: "QUALIFIED_UNIVERSE", label: "Qualified" },
  { value: "ACTIVE_WATCHLIST", label: "Watchlist" },
  { value: "ALL", label: "All instruments" },
];
const SORTS: readonly { value: SortKey; label: string }[] = [
  { value: "symbol", label: "Symbol" },
  { value: "name", label: "Name" },
  { value: "exchange", label: "Exchange" },
  { value: "rank", label: "Watchlist rank" },
  { value: "score", label: "Watchlist score" },
  { value: "first_discovered_at", label: "First discovered" },
  { value: "last_verified_at", label: "Last verified" },
];
const EXCHANGES = ["XNYS", "XNAS", "ARCX", "XASE", "BATS", "OTCM"] as const;

export function DiscoveryPage() {
  const summary = useApiResource((signal) => discovery.summary({ signal }), []);
  const [lastRun, setLastRun] = useState<JobRequestResponse | null>(null);
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Asset Discovery"
        description="Instruments catalogued from the configured provider's directory, the subset that passes Atlas's eligibility rules, and the subset ranked for analytical attention. Ranking is analytical priority only: never a trading signal."
        actions={<RefreshButton onClick={summary.reload} loading={summary.loading} />}
      />
      <SummaryPanel resource={summary} />
      <div className="mt-3 grid gap-3 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <JobsPanel refreshKey={lastRun?.job.id ?? ""} />
        <RunPanel onStarted={setLastRun} />
      </div>
      <UniversePanel />
      <RulesPanel />
    </section>
  );
}

function SummaryPanel({ resource }: { resource: Resource<DiscoverySummary> }) {
  if (resource.status === "loading") return <Loading label="Loading discovery summary…" />;
  if (resource.status === "error" || !resource.data)
    return <ErrorState message={resource.error ?? "No data"} onRetry={resource.reload} />;
  const s = resource.data;
  const last = s.last_successful_discovery;
  const inactive = (s.by_status.INACTIVE ?? 0) + (s.by_status.DELISTED ?? 0);
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8" aria-label="Discovery summary">
        <Stat label="Discovered" value={formatCount(s.catalogued)} hint={`${s.provider} directory`} />
        <Stat label="Discovery pool" value={formatCount(s.discovery_pool)} hint="active instruments" />
        <Stat label="Qualified" value={formatCount(s.qualified)} hint="pass every mandatory rule" tone="positive" />
        <Stat label="Watchlist" value={formatCount(s.watchlist)} hint="analytical priority" tone="accent" />
        <Stat label="New listings" value={formatCount(s.new_listings)} hint={`last ${s.recent_days} days`} />
        <Stat
          label="Disqualified"
          value={formatCount(s.recently_disqualified)}
          hint={`removed, last ${s.recent_days} days`}
          tone={s.recently_disqualified ? "caution" : undefined}
        />
        <Stat
          label="Inactive"
          value={formatCount(inactive)}
          hint={`${formatCount(s.by_status.DELISTED ?? 0)} delisted · ${formatCount(s.recently_deactivated)} recent`}
        />
        <Stat
          label="Last discovery"
          value={last ? <Timestamp iso={last.completed_at} seconds={false} /> : "never"}
          hint={last ? humanize(last.status) : "no completed run"}
          tone={last ? undefined : "caution"}
        />
      </div>
      {s.notes.map((note) => (
        <Notice key={note}>{note}</Notice>
      ))}
      {s.reference_capabilities && (
        <Notice tone="info">
          Not supplied by {s.reference_capabilities.provider}: {s.reference_capabilities.not_supplied.join("; ")}.
        </Notice>
      )}
    </div>
  );
}

function JobsPanel({ refreshKey }: { refreshKey: string }) {
  const jobs = useApiResource((signal) => discovery.jobs({ limit: 15 }, { signal }), [refreshKey], {
    isEmpty: (page) => page.items.length === 0,
  });
  return (
    <Panel title="Discovery jobs" subtitle="newest first" actions={<RefreshButton onClick={jobs.reload} loading={jobs.loading} />} bodyClassName="">
      {jobs.status === "loading" && <Loading label="Loading jobs…" />}
      {jobs.status === "error" && <ErrorState message={jobs.error ?? ""} onRetry={jobs.reload} compact />}
      {jobs.status === "empty" && (
        <EmptyState title="No discovery job has run yet">Start one below, or enable the discovery worker.</EmptyState>
      )}
      {jobs.data && jobs.data.items.length > 0 && (
        <DataTable label="Discovery jobs">
          <thead>
            <tr>
              {["Kind", "Status", "Created", "Finished", "Attempt", "Result", "Error"].map((h) => (
                <th key={h} className={TH} scope="col">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {jobs.data.items.map((job) => (
              <tr key={job.id}>
                <td className={TD}>
                  {humanize(job.kind)}
                  {typeof job.params.mode === "string" && <span className="text-ink-faint"> · {job.params.mode.toLowerCase()}</span>}
                  {typeof job.params.layer === "string" && <span className="text-ink-faint"> · {LAYER_LABELS[job.params.layer as UniverseLayer]}</span>}
                </td>
                <td className={TD}>
                  <DiscoveryJobBadge status={job.status} />
                </td>
                <td className={TD}>
                  <Timestamp iso={job.created_at} />
                </td>
                <td className={TD}>
                  <Timestamp iso={job.completed_at} />
                </td>
                <td className={`${TD} num`}>
                  {job.attempt}/{job.max_attempts}
                </td>
                <td className={`${TD} text-ink-muted`}>{jobResult(job.kind, job.counters)}</td>
                <td className={`${TD} text-negative max-w-72 truncate`} title={job.error ?? undefined}>
                  {job.error ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function jobResult(kind: DiscoveryJobKind, counters: Record<string, unknown>): string {
  const n = (key: string) => (typeof counters[key] === "number" ? (counters[key] as number) : 0);
  switch (kind) {
    case "DIRECTORY_DISCOVERY":
    case "METADATA_REFRESH":
      return `${formatCount(n("received"))} read · ${formatCount(n("new"))} new · ${formatCount(n("changed"))} changed · ${formatCount(n("malformed"))} rejected · ${formatCount(n("missing"))} missing`;
    case "QUALIFICATION":
      return `${formatCount(n("evaluated"))} evaluated · ${formatCount(n("QUALIFIED"))} qualified · ${formatCount(n("DISQUALIFIED"))} disqualified · ${formatCount(n("INSUFFICIENT_DATA"))} insufficient data`;
    case "WATCHLIST_SCORING":
      return `${formatCount(n("scored"))} scored · ${formatCount(n("selected"))} selected`;
    case "MEMBERSHIP_UPDATE":
      return `${formatCount(n("added"))} added · ${formatCount(n("removed"))} removed`;
  }
}

function RunPanel({ onStarted }: { onStarted: (job: JobRequestResponse) => void }) {
  const [mode, setMode] = useState<"INCREMENTAL" | "FULL">("INCREMENTAL");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "positive" | "negative" | "info"; text: string } | null>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const result = await discovery.run({ mode, pipeline: true }, token || undefined);
      setMessage(
        result.created
          ? { tone: "positive", text: `Started job ${result.job.id.slice(0, 8)}: discovery, then qualification, scoring and membership updates.` }
          : { tone: "info", text: `A discovery job is already ${result.job.status.toLowerCase()} (${result.job.id.slice(0, 8)}); not starting another.` },
      );
      onStarted(result);
    } catch (reason) {
      setMessage({
        tone: "negative",
        text: reason instanceof ApiError ? `${reason.message}${reason.status ? ` (HTTP ${reason.status})` : ""}` : String(reason),
      });
    } finally {
      setBusy(false);
    }
  };
  const input = "border-border-strong bg-surface focus:border-accent h-7 w-full rounded-sm border px-2 text-xs outline-none";
  return (
    <Panel title="Run discovery">
      <form onSubmit={(e) => void submit(e)} className="space-y-2.5" aria-label="Run discovery">
        <Segmented
          label="Listing"
          value={mode}
          onChange={setMode}
          options={[
            { value: "INCREMENTAL", label: "Active only", title: "Lists active instruments; looks up those that disappeared" },
            { value: "FULL", label: "Full", title: "Lists every status; records instruments absent from the directory" },
          ]}
        />
        <label className="text-ink-muted block text-2xs font-semibold uppercase">
          Operator token
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="off"
            className={`${input} mt-1 normal-case`}
            aria-label="Operator token"
            placeholder="only if ATLAS_OPERATOR_TOKEN is set"
          />
        </label>
        <Button type="submit" variant="primary" disabled={busy}>
          <Play aria-hidden className="size-3.5" />
          {busy ? "Starting…" : "Start discovery"}
        </Button>
        {message && <Notice tone={message.tone}>{message.text}</Notice>}
        <p className="text-ink-faint text-2xs">
          Reads the provider's asset directory (read-only) and re-evaluates the universe. History for qualification is
          retrieved for a bounded number of instruments per run. No orders are involved.
        </p>
      </form>
    </Panel>
  );
}

function UniversePanel() {
  const [tab, setTab] = useState<Tab>("DISCOVERY_POOL");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [exchange, setExchange] = useState("");
  const [status, setStatus] = useState<AssetStatus | "">("");
  const [outcome, setOutcome] = useState<QualificationOutcome | "">("");
  const [sort, setSort] = useState<SortKey>("symbol");
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  const [offset, setOffset] = useState(0);

  // Debounce typing; any filter change starts again at the first page.
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);
  useEffect(() => setOffset(0), [tab, query, exchange, status, outcome, sort, order]);
  // The watchlist is read by rank: switch the sort together with the tab (one request).
  const selectTab = (next: Tab) => {
    setTab(next);
    if (next === "ACTIVE_WATCHLIST") setSort("rank");
    else if (sort === "rank" || sort === "score") setSort("symbol");
  };

  const params: AssetQuery = {
    sort,
    order,
    limit: PAGE_SIZE,
    offset,
    ...(query ? { search: query } : {}),
    ...(exchange ? { exchange: [exchange] } : {}),
    ...(status ? { status: [status] } : {}),
    ...(outcome ? { outcome } : {}),
  };
  const page = useApiResource((signal) => discovery.assets(tab, params, { signal }), [tab, query, exchange, status, outcome, sort, order, offset], {
    isEmpty: (p) => p.items.length === 0,
  });
  const select = "border-border-strong bg-surface focus:border-accent h-7 rounded-sm border px-1.5 text-xs outline-none";
  const total = page.data?.total ?? 0;
  return (
    <Panel
      className="mt-3"
      title="Universe"
      subtitle={page.data ? `${formatCount(total)} instruments` : undefined}
      actions={<Segmented label="Universe layer" value={tab} onChange={selectTab} options={TABS} />}
      bodyClassName=""
    >
      <div className="border-border flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search symbol or name"
          aria-label="Search symbol or name"
          className={`${select} w-48`}
        />
        <select aria-label="Exchange" value={exchange} onChange={(e) => setExchange(e.target.value)} className={select}>
          <option value="">All exchanges</option>
          {EXCHANGES.map((mic) => (
            <option key={mic} value={mic}>
              {mic}
            </option>
          ))}
        </select>
        {tab === "ALL" && (
          <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value as AssetStatus | "")} className={select}>
            <option value="">All statuses</option>
            <option value="ACTIVE">Active</option>
            <option value="INACTIVE">Inactive</option>
            <option value="DELISTED">Delisted</option>
          </select>
        )}
        <select aria-label="Qualification" value={outcome} onChange={(e) => setOutcome(e.target.value as QualificationOutcome | "")} className={select}>
          <option value="">Any qualification</option>
          <option value="QUALIFIED">Qualified</option>
          <option value="DISQUALIFIED">Disqualified</option>
          <option value="INSUFFICIENT_DATA">Insufficient data</option>
        </select>
        <select aria-label="Sort by" value={sort} onChange={(e) => setSort(e.target.value as SortKey)} className={select}>
          {SORTS.map((s) => (
            <option key={s.value} value={s.value}>
              Sort: {s.label}
            </option>
          ))}
        </select>
        <Button variant="ghost" onClick={() => setOrder(order === "asc" ? "desc" : "asc")} aria-label="Sort order">
          {order === "asc" ? "Ascending" : "Descending"}
        </Button>
      </div>
      {page.status === "loading" && <Loading label="Loading instruments…" />}
      {page.status === "error" && (
        <div className="p-3">
          <ErrorState message={page.error ?? ""} onRetry={page.reload} />
        </div>
      )}
      {page.status === "empty" && (
        <EmptyState title="No instruments match">
          {tab === "QUALIFIED_UNIVERSE" || tab === "ACTIVE_WATCHLIST"
            ? "Nothing has qualified yet: qualification needs stored daily history, which is retrieved for a bounded number of instruments per run."
            : "Run discovery to catalogue the provider's directory, or change the filters."}
        </EmptyState>
      )}
      {page.data && page.data.items.length > 0 && <AssetTable rows={page.data.items} watchlist={tab === "ACTIVE_WATCHLIST"} />}
      {page.data && total > PAGE_SIZE && (
        <div className="flex items-center justify-end gap-2 px-3 py-2 text-xs">
          <span className="text-ink-muted num">
            {formatCount(offset + 1)}–{formatCount(Math.min(offset + PAGE_SIZE, total))} of {formatCount(total)}
          </span>
          <Button variant="ghost" aria-label="Previous page" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
            <ChevronLeft aria-hidden className="size-3.5" />
          </Button>
          <Button variant="ghost" aria-label="Next page" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
            <ChevronRight aria-hidden className="size-3.5" />
          </Button>
        </div>
      )}
      {page.data?.notes.map((note) => (
        <div key={note} className="px-3 pb-2">
          <Notice>{note}</Notice>
        </div>
      ))}
    </Panel>
  );
}

function AssetTable({ rows, watchlist }: { rows: AssetSummary[]; watchlist: boolean }) {
  const headers = [
    ...(watchlist ? ["Rank", "Score"] : []),
    "Symbol",
    "Name",
    "Exchange",
    "Type",
    "Status",
    "Tradable",
    "Shortable",
    "Easy to borrow",
    "Qualification",
    "Reasons",
    "Last verified",
  ];
  return (
    <DataTable label="Instruments">
      <thead>
        <tr>
          {headers.map((h) => (
            <th key={h} className={TH} scope="col">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((a) => (
          <tr key={a.asset_id} aria-label={a.symbol}>
            {watchlist && (
              <>
                <td className={`${TD} num`}>{a.watchlist_rank ?? MISSING}</td>
                <td className={`${TD} num`} title="0–100, relative to the other qualified instruments; open the instrument for the factors">
                  {a.watchlist_score ? Number(a.watchlist_score).toFixed(1) : MISSING}
                </td>
              </>
            )}
            <td className={`${TD} font-medium`}>
              <Link to={`/discovery/assets/${a.asset_id}`} className="text-accent hover:underline">
                {a.symbol}
              </Link>
            </td>
            <td className={`${TD} text-ink-muted max-w-64 truncate`} title={a.name ?? undefined}>
              {a.name ?? MISSING}
            </td>
            <td className={TD} title={a.provider_exchange ? `provider code ${a.provider_exchange}` : undefined}>
              {a.exchange ?? <span className="text-ink-faint">unmapped ({a.provider_exchange ?? "?"})</span>}
            </td>
            <td className={TD}>
              <InstrumentTypeLabel type={a.instrument_type} />
            </td>
            <td className={TD}>
              <AssetStatusBadge status={a.status} />
            </td>
            <td className={TD}>
              <Flag value={a.tradable} label="Tradable" />
            </td>
            <td className={TD}>
              <Flag value={a.shortable} label="Shortable" />
            </td>
            <td className={TD}>
              <Flag value={a.easy_to_borrow} label="Easy to borrow" />
            </td>
            <td className={TD}>
              <OutcomeBadge outcome={a.qualification_outcome} />
            </td>
            <td className={`${TD} text-ink-muted max-w-72 truncate`}>
              {reasons(a)}
            </td>
            <td className={TD}>
              <Timestamp iso={a.last_verified_at} seconds={false} />
            </td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function reasons(a: AssetSummary): string {
  if (a.failed_rules.length) return `Failed: ${a.failed_rules.map(humanize).join(", ")}`;
  if (a.unavailable_rules.length) return `Missing: ${a.unavailable_rules.map(humanize).join(", ")}`;
  return "";
}

function RulesPanel() {
  const config = useApiResource((signal) => discovery.config({ signal }), []);
  if (config.status === "loading") return null;
  if (config.status === "error" || !config.data)
    return (
      <div className="mt-3">
        <ErrorState message={config.error ?? ""} onRetry={config.reload} compact />
      </div>
    );
  const { rule_set: rules, scoring, notes } = config.data;
  const r = rules.config;
  const weights = (scoring.config.weights ?? {}) as Record<string, string>;
  return (
    <div className="mt-3 grid gap-3 lg:grid-cols-2">
      <Panel title="Qualification rules" subtitle={`${rules.code} v${rules.version}`}>
        <p className="text-ink-muted mb-2 text-xs">{rules.description}</p>
        <KeyValues
          items={[
            { label: "Volume basis", value: <Badge tone="caution">{String(r.feed).toUpperCase()}</Badge> },
            { label: "Exchanges", value: (r.exchanges as string[]).join(", ") },
            { label: "History", value: `≥ ${pct(r.min_history_coverage)} of the last ${String(r.history_sessions)} sessions` },
            { label: "Minimum price", value: `${String(r.min_price)} USD` },
            { label: "Avg daily volume", value: `≥ ${formatCount(Number(r.min_avg_daily_volume))} shares (${String(r.liquidity_sessions)} sessions)` },
            { label: "Median $ volume", value: `≥ ${formatCount(Number(r.min_median_dollar_volume))} USD` },
            { label: "Missing / invalid", value: `≤ ${pct(r.max_missing_session_ratio)} missing · ≤ ${pct(r.max_invalid_bar_ratio)} quarantined` },
            { label: "Spread (optional)", value: r.max_median_spread_bps ? `≤ ${String(r.max_median_spread_bps)} bps when ≥ ${String(r.min_quotes_for_spread)} quotes exist` : "off" },
            { label: "Volatility cap", value: r.max_annualized_volatility ? pct(r.max_annualized_volatility) : "off" },
          ]}
        />
      </Panel>
      <Panel title="Watchlist ranking" subtitle={`${scoring.code} v${scoring.version}`}>
        <p className="text-ink-muted mb-2 text-xs">{scoring.description}</p>
        <KeyValues
          items={[
            ...Object.entries(weights).map(([factor, weight]) => ({ label: humanize(factor), value: `weight ${weight}` })),
            { label: "Watchlist size", value: `${String(scoring.config.watchlist_size)} (+${String(scoring.config.retention_buffer)} retention)` },
          ]}
        />
        <div className="mt-2 space-y-1">
          {notes.map((note) => (
            <Notice key={note}>{note}</Notice>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function pct(value: unknown): string {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(n * 100 < 10 ? 1 : 0)}%` : MISSING;
}
