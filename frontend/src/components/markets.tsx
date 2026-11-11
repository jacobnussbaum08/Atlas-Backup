import { Link } from "react-router";

import type { Resource } from "../hooks/useApiResource";
import { humanize, MISSING } from "../lib/format";
import type { AssetSummary, Page } from "../types/discovery";
import type { Classification, MarketConditions } from "../types/intelligence";
import { OriginBadge } from "./provenance";
import { Badge, DataTable, EmptyState, ErrorState, Loading, Panel, TD, TH, Timestamp, type Tone } from "./ui";

const LABEL_TONES: Record<string, Tone> = {
  BULLISH: "positive",
  BEARISH: "negative",
  SIDEWAYS: "neutral",
  TRANSITIONING: "caution",
  LOW: "neutral",
  NORMAL: "neutral",
  ELEVATED: "caution",
  EXTREME: "negative",
  STABLE: "positive",
  UNSETTLED: "caution",
  STRESSED: "negative",
  UNCERTAIN: "neutral",
};

function ClassificationCell({ c }: { c: Classification }) {
  const confidence = Number(c.confidence);
  return (
    <div className="flex min-w-0 items-center gap-2" title={c.reasons.join("\n")}>
      <span className="text-ink-muted text-2xs uppercase">{humanize(c.dimension)}</span>
      <Badge tone={LABEL_TONES[c.label] ?? "neutral"}>{humanize(c.label)}</Badge>
      <span className="num text-ink-faint text-2xs">
        {Number.isFinite(confidence) ? `${Math.round(confidence * 100)}% evidence` : MISSING}
        {c.quality !== "OK" && ` · ${humanize(c.quality)}`}
      </span>
    </div>
  );
}

/** Benchmark regime (trend, volatility, stability) from the newest recorded Phase 3B run. */
export function MarketContextStrip({ market }: { market: Resource<MarketConditions> }) {
  const m = market.data;
  return (
    <Panel
      className="mb-3"
      title="Market regime"
      subtitle={
        m?.available ? (
          <span className="flex items-center gap-1.5">
            <OriginBadge origin="RECORDED" />
            {m.benchmark_symbol ?? "benchmark"} · as of <Timestamp iso={m.as_of} />
          </span>
        ) : undefined
      }
      actions={<Link to="/intelligence" className="text-accent text-xs hover:underline">Market Intelligence</Link>}
    >
      {market.error && !m ? (
        <ErrorState message={market.error} onRetry={market.reload} compact />
      ) : !m ? (
        <Loading />
      ) : !m.available || m.classifications.length === 0 ? (
        <p className="text-ink-muted text-xs">
          No market-intelligence classification is recorded yet. {m.notes[0] ?? ""}
        </p>
      ) : (
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          {m.classifications
            .filter((c) => c.timeframe === "1Day")
            .map((c) => (
              <ClassificationCell key={`${c.dimension}-${c.timeframe}`} c={c} />
            ))}
          {m.participation && m.participation.status === "OK" && (
            <span className="text-ink-faint text-2xs">
              {m.participation.members_evaluated}/{m.participation.members_total} universe members evaluated
            </span>
          )}
        </div>
      )}
    </Panel>
  );
}

/** The Phase 3A active watchlist, by rank, linking to each asset's chart. */
export function WatchlistPanel({ watchlist }: { watchlist: Resource<Page<AssetSummary>> }) {
  const w = watchlist.data;
  return (
    <Panel
      className="mt-3"
      title="Active watchlist"
      subtitle={w ? `${w.total} member(s) · ranked by the stored watchlist score` : undefined}
      actions={<Link to="/discovery" className="text-accent text-xs hover:underline">Asset Discovery</Link>}
      bodyClassName=""
    >
      {watchlist.error && !w ? (
        <div className="p-3">
          <ErrorState message={watchlist.error} onRetry={watchlist.reload} />
        </div>
      ) : !w ? (
        <Loading label="Loading watchlist…" />
      ) : w.items.length === 0 ? (
        <div className="px-3">
          <EmptyState title="The active watchlist is empty">
            Members are chosen by the discovery worker's scoring job from the qualified universe.
          </EmptyState>
        </div>
      ) : (
        <DataTable label="Active watchlist">
          <thead>
            <tr>
              <th scope="col" className={`${TH} text-right`}>Rank</th>
              <th scope="col" className={TH}>Symbol</th>
              <th scope="col" className={TH}>Name</th>
              <th scope="col" className={TH}>Type</th>
              <th scope="col" className={TH}>Exchange</th>
              <th scope="col" className={`${TH} text-right`}>Score</th>
              <th scope="col" className={TH}>Shortable</th>
              <th scope="col" className={TH}>Chart</th>
            </tr>
          </thead>
          <tbody>
            {w.items.map((a) => (
              <tr key={a.asset_id}>
                <td className={`${TD} num text-right`}>{a.watchlist_rank ?? MISSING}</td>
                <td className={`${TD} num font-medium`}>
                  <Link to={`/discovery/assets/${a.asset_id}`} className="hover:text-accent">
                    {a.symbol}
                  </Link>
                </td>
                <td className={`${TD} text-ink-muted max-w-56 truncate`}>{a.name ?? MISSING}</td>
                <td className={TD}>{humanize(a.instrument_type)}</td>
                <td className={`${TD} num`}>{a.exchange ?? MISSING}</td>
                <td className={`${TD} num text-right`}>{a.watchlist_score === null ? MISSING : Number(a.watchlist_score).toFixed(3)}</td>
                <td className={TD}>{a.shortable === null ? MISSING : a.shortable ? "yes" : "no"}</td>
                <td className={TD}>
                  <Link to={`/markets/${encodeURIComponent(a.symbol)}`} className="text-accent hover:underline">
                    Open
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}
