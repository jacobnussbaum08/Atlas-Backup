import { Columns3, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { AssetTable, COLUMNS, type ColumnKey, DEFAULT_COLUMNS } from "../components/market/AssetTable";
import { MarketContextStrip, WatchlistPanel } from "../components/markets";
import { OriginBadge } from "../components/provenance";
import { Button, EmptyState, ErrorState, Loading, PageHeader, Panel, RefreshButton } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { useNow } from "../hooks/useNow";
import { useSnapshots } from "../hooks/useSnapshots";
import { formatAge, humanize } from "../lib/format";
import { discovery } from "../services/discovery";
import { intelligence } from "../services/intelligence";

const COLUMNS_KEY = "atlas.markets.columns";
/** Regime runs and watchlist scoring are daily jobs: a slow refresh is enough. */
export const CONTEXT_POLL_MS = 300_000;

function storedColumns(): ColumnKey[] {
  try {
    const raw = window.localStorage.getItem(COLUMNS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as string[];
      const known = new Set(COLUMNS.map((c) => c.key));
      const valid = parsed.filter((k): k is ColumnKey => known.has(k as ColumnKey));
      if (valid.length > 0) return valid;
    }
  } catch {
    // Use the defaults.
  }
  return [...DEFAULT_COLUMNS];
}

export function MarketsPage() {
  const now = useNow(1_000);
  const { resource, data, live } = useSnapshots();
  const [query, setQuery] = useState("");
  const [columns, setColumns] = useState<ColumnKey[]>(storedColumns);
  const [chooserOpen, setChooserOpen] = useState(false);
  const market = useApiResource((signal) => intelligence.market({ signal }), [], { refreshMs: CONTEXT_POLL_MS });
  const watchlist = useApiResource(
    (signal) => discovery.assets("ACTIVE_WATCHLIST", { sort: "rank", order: "asc", limit: 50 }, { signal }),
    [],
    { refreshMs: CONTEXT_POLL_MS },
  );

  const rows = useMemo(() => {
    const q = query.trim().toUpperCase();
    if (!data) return [];
    if (!q) return data.snapshots;
    return data.snapshots.filter(
      (s) => s.asset.symbol.includes(q) || (s.asset.name ?? "").toUpperCase().includes(q),
    );
  }, [data, query]);

  const toggleColumn = (key: ColumnKey) => {
    setColumns((current) => {
      const next = current.includes(key) ? current.filter((k) => k !== key) : [...current, key];
      try {
        window.localStorage.setItem(COLUMNS_KEY, JSON.stringify(next));
      } catch {
        // Not persisted.
      }
      return next;
    });
  };

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Markets"
        description="Monitored assets with live prices from stored data, the recorded market regime and the active watchlist. Select a row for its chart, quotes and trades."
        actions={
          <>
            <label className="border-border-strong bg-panel focus-within:border-accent flex h-7 items-center gap-1.5 rounded-sm border px-2">
              <Search aria-hidden className="text-ink-faint size-3.5" />
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search symbol or name"
                aria-label="Search by symbol or name"
                className="placeholder:text-ink-faint w-44 bg-transparent text-xs outline-none"
              />
            </label>
            <div className="relative">
              <Button onClick={() => setChooserOpen((o) => !o)} aria-expanded={chooserOpen} aria-haspopup="true">
                <Columns3 aria-hidden className="size-3.5" />
                Columns
              </Button>
              {chooserOpen && (
                <fieldset className="border-border-strong bg-panel-raised absolute right-0 z-20 mt-1 w-48 rounded-sm border p-2 shadow-lg">
                  <legend className="sr-only">Visible columns</legend>
                  {COLUMNS.map((c) => (
                    <label key={c.key} className="hover:bg-panel flex items-center gap-2 rounded-sm px-1.5 py-1 text-xs">
                      <input
                        type="checkbox"
                        checked={columns.includes(c.key)}
                        disabled={c.fixed}
                        onChange={() => toggleColumn(c.key)}
                        className="accent-accent"
                      />
                      {c.label}
                    </label>
                  ))}
                </fieldset>
              )}
            </div>
            <RefreshButton onClick={resource.reload} loading={resource.loading} />
          </>
        }
      />

      <MarketContextStrip market={market} />

      <Panel
        title={<span className="inline-flex items-center gap-1.5">Monitored assets <OriginBadge origin="LIVE" title="Snapshots computed by the backend from the newest stored trades, quotes and bars; updated over the live feed while it is connected." /></span>}
        subtitle={
          data
            ? `${data.snapshots.length} asset(s) · ${data.provider} · ${data.feed.toUpperCase()} · updated ${formatAge(data.checked_at, now)} · live feed ${humanize(live)}`
            : undefined
        }
        bodyClassName=""
      >
        {resource.status === "loading" && <Loading label="Loading assets…" />}
        {resource.status === "error" && (
          <div className="p-3">
            <ErrorState message={resource.error ?? ""} onRetry={resource.reload} />
          </div>
        )}
        {resource.status === "empty" && (
          <div className="px-3">
            <EmptyState title="No monitored assets">The backend reports no assets mapped to the configured provider.</EmptyState>
          </div>
        )}
        {data && data.snapshots.length > 0 && rows.length === 0 && (
          <div className="px-3">
            <EmptyState title={`No asset matches "${query}"`}>Search matches symbols and names.</EmptyState>
          </div>
        )}
        {rows.length > 0 && <AssetTable rows={rows} columns={columns} now={now} />}
        {resource.error && data && (
          <div className="p-2">
            <ErrorState message={`Refresh failed: ${resource.error}`} onRetry={resource.reload} compact />
          </div>
        )}
      </Panel>
      <WatchlistPanel watchlist={watchlist} />
      {data && (
        <ul className="text-ink-faint mt-2 space-y-0.5 text-2xs">
          {data.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
          <li>Freshness: Live = streamed within {data.snapshots[0]?.freshness_threshold_seconds ?? "the"}s; Historical = newest data came from provider history; Closed = outside the feed's hours.</li>
        </ul>
      )}
    </section>
  );
}
