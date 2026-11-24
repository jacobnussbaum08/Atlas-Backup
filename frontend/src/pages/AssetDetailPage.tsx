import { ArrowLeft, Radio } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router";

import { type ChartMarker, PriceChart } from "../components/market/PriceChart";
import {
  FeedBadge,
  FreshnessBadge,
  GapStatusBadge,
  QualityBadge,
  SeverityBadge,
  SourceBadge,
  streamTone,
} from "../components/status";
import {
  Badge,
  DataTable,
  EmptyState,
  ErrorState,
  KeyValues,
  Loading,
  Notice,
  Panel,
  RefreshButton,
  Segmented,
  TD,
  TH,
  Timestamp,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { useCandles } from "../hooks/useCandles";
import { useLiveUpdates } from "../hooks/useLiveUpdates";
import { useNow } from "../hooks/useNow";
import { useSnapshots } from "../hooks/useSnapshots";
import { useDashboardStatus } from "../layouts/DashboardLayout";
import {
  CHART_TIMEFRAMES,
  type ChartBar,
  type ChartTimeframe,
  firstAtOrAfter,
  LIVE_TIMEFRAMES,
  RANGE_OPTIONS,
} from "../lib/candles";
import {
  direction,
  formatAge,
  formatDuration,
  formatPercent,
  formatPrice,
  formatQuantity,
  formatSigned,
  humanize,
  MISSING,
  toNumber,
} from "../lib/format";
import { quoteSide } from "../lib/snapshots";
import { marketData } from "../services/marketData";
import type { AssetSnapshot, CompletenessResponse, Trade } from "../types/marketData";

const DAY_MS = 86_400_000;
/** The completeness endpoint evaluates at most 31 days. */
const COMPLETENESS_MAX_MS = 31 * DAY_MS;

export function AssetDetailPage() {
  const { symbol: raw = "" } = useParams();
  const symbol = raw.toUpperCase();
  const now = useNow(1_000);
  const { resource, data } = useSnapshots([symbol]);
  const snapshot = data?.snapshots[0] ?? null;

  if (resource.status === "loading") return <Loading label={`Loading ${symbol}…`} />;
  if (resource.status === "error" && !data) {
    const notFound = /not found|not monitored/i.test(resource.error ?? "");
    return (
      <section aria-labelledby="page-title">
        <BackLink />
        <h1 id="page-title" className="text-base font-semibold">{symbol}</h1>
        <div className="mt-3">
          {notFound ? (
            <EmptyState title={`${symbol} is not a monitored asset`}>
              It has no active mapping at the configured provider. <Link className="text-accent" to="/markets">See monitored assets</Link>.
            </EmptyState>
          ) : (
            <ErrorState message={resource.error ?? ""} onRetry={resource.reload} />
          )}
        </div>
      </section>
    );
  }
  if (!snapshot) {
    return (
      <section aria-labelledby="page-title">
        <BackLink />
        <h1 id="page-title" className="text-base font-semibold">{symbol}</h1>
        <EmptyState title={`${symbol} is not a monitored asset`}>
          It has no active mapping at the configured provider. <Link className="text-accent" to="/markets">See monitored assets</Link>.
        </EmptyState>
      </section>
    );
  }
  return <AssetDetail snapshot={snapshot} now={now} onRefresh={resource.reload} refreshing={resource.loading} />;
}

function BackLink() {
  return (
    <Link to="/markets" className="text-ink-muted hover:text-ink mb-2 inline-flex items-center gap-1 text-xs">
      <ArrowLeft aria-hidden className="size-3" /> Markets
    </Link>
  );
}

function AssetDetail({
  snapshot,
  now,
  onRefresh,
  refreshing,
}: {
  snapshot: AssetSnapshot;
  now: number;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const symbol = snapshot.asset.symbol;
  const [timeframe, setTimeframe] = useState<ChartTimeframe>("1Min");
  const [rangeIndex, setRangeIndex] = useState(0);
  const [rangeTick, setRangeTick] = useState(0);
  const options = RANGE_OPTIONS[timeframe];
  const option = options[Math.min(rangeIndex, options.length - 1)] ?? options[0];
  const days = option?.days ?? 1;

  // The range is fixed when chosen (end = now at that moment); live bars extend it.
  const range = useMemo(() => {
    const end = new Date();
    return { start: new Date(end.getTime() - days * DAY_MS).toISOString(), end: end.toISOString() };
  }, [days, timeframe, rangeTick]);

  const liveTimeframe = LIVE_TIMEFRAMES.has(timeframe);
  const streamHealth = useDashboardStatus().stream.data?.health;
  const series = useCandles(symbol, timeframe, range, { live: liveTimeframe });
  const completenessStart = new Date(Math.max(Date.parse(range.start), Date.parse(range.end) - COMPLETENESS_MAX_MS)).toISOString();
  const completeness = useApiResource(
    (signal) =>
      marketData.completeness(
        { symbol, data_type: "BAR", timeframe, start: completenessStart, end: range.end },
        { signal },
      ),
    [symbol, timeframe, completenessStart, range.end],
  );
  const markers = useMemo(() => gapMarkers(series.bars, completeness.data), [series.bars, completeness.data]);
  const up = direction(snapshot.change);

  return (
    <section aria-labelledby="page-title">
      <BackLink />
      <div className="border-border bg-panel mb-3 flex flex-wrap items-end gap-x-6 gap-y-2 rounded-sm border px-3 py-2.5">
        <div>
          <div className="flex items-baseline gap-2">
            <h1 id="page-title" className="text-lg font-semibold tracking-tight">{symbol}</h1>
            <span className="text-ink-muted text-xs">{snapshot.asset.name ?? MISSING}</span>
          </div>
          <p className="text-ink-faint num text-2xs">
            {snapshot.asset.asset_class} · {snapshot.asset.exchange_code ?? MISSING} · {snapshot.asset.quote_currency}
          </p>
        </div>
        <div>
          <p className="num text-xl font-medium">
            {snapshot.last_price ? formatPrice(snapshot.last_price.value) : MISSING}
            <span className="text-ink-faint ml-1 text-xs">{snapshot.asset.quote_currency}</span>
          </p>
          <p className="text-ink-faint text-2xs">
            {snapshot.last_price
              ? `${snapshot.last_price.basis === "TRADE" ? "last trade" : "1-min bar close"} · `
              : "no stored price · "}
            <Timestamp iso={snapshot.last_price?.event_time} />
          </p>
        </div>
        <div title={snapshot.previous_close_note ?? undefined}>
          <p className={`num text-sm font-medium ${up === "up" ? "text-positive" : up === "down" ? "text-negative" : "text-ink-muted"}`}>
            {formatSigned(snapshot.change)} ({formatPercent(snapshot.change_percent)})
          </p>
          <p className="text-ink-faint text-2xs">
            {snapshot.previous_close
              ? `vs ${formatPrice(snapshot.previous_close.value)} prev. close (${snapshot.previous_close.session_date})`
              : "no previous close"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge>{humanize(snapshot.provider)}</Badge>
          <FeedBadge feed={snapshot.feed} coverage={snapshot.feed_coverage} />
          <SourceBadge source={snapshot.last_price?.source} />
          <FreshnessBadge freshness={snapshot.freshness} />
          <QualityBadge status={snapshot.quality_status} />
        </div>
        <div className="ml-auto">
          <RefreshButton
            loading={refreshing}
            onClick={() => {
              onRefresh();
              setRangeTick((t) => t + 1);
              completeness.reload();
            }}
          />
        </div>
      </div>
      {snapshot.previous_close_note && !snapshot.previous_close && (
        <div className="mb-3"><Notice tone="caution">Change not shown: {snapshot.previous_close_note}.</Notice></div>
      )}

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-3">
          <Panel
            title="Price"
            subtitle={`${snapshot.feed.toUpperCase()} · ${series.meta?.adjustment ?? "RAW"} prices · times in display zone`}
            bodyClassName="p-0"
            actions={
              <>
                <Segmented
                  label="Timeframe"
                  value={timeframe}
                  onChange={(tf) => {
                    setTimeframe(tf);
                    setRangeIndex(0);
                  }}
                  options={CHART_TIMEFRAMES.map((tf) => ({ value: tf, label: tf }))}
                />
                <Segmented
                  label="Range"
                  value={String(Math.min(rangeIndex, options.length - 1))}
                  onChange={(v) => setRangeIndex(Number(v))}
                  options={options.map((o, i) => ({ value: String(i), label: o.label }))}
                />
              </>
            }
          >
            <ChartStatusBar series={series} liveTimeframe={liveTimeframe} timeframe={timeframe} streamHealth={streamHealth} />
            {series.status === "loading" && series.bars.length === 0 && <Loading label="Loading candles…" />}
            {series.status === "error" && (
              <div className="p-3"><ErrorState message={series.error ?? ""} onRetry={series.reload} /></div>
            )}
            {series.status === "empty" && (
              <div className="px-3">
                <EmptyState title={`No stored ${timeframe} candles for ${symbol} in the last ${option?.label}`}>
                  Atlas only charts bars stored in PostgreSQL.{" "}
                  <Link
                    className="text-accent"
                    to={`/ingestion?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&start=${encodeURIComponent(range.start)}&end=${encodeURIComponent(range.end)}`}
                  >
                    Request this history from the provider
                  </Link>
                  .
                </EmptyState>
              </div>
            )}
            {series.bars.length > 0 && (
              <PriceChart
                bars={series.bars}
                timeframe={timeframe}
                update={series.update}
                markers={markers}
                lastTrade={toNumber(snapshot.latest_trade?.price)}
              />
            )}
            <ChartNotes series={series} />
          </Panel>
          <CoveragePanel completeness={completeness.data} error={completeness.error} timeframe={timeframe} />
        </div>

        <div className="min-w-0 space-y-3">
          <QuotePanel snapshot={snapshot} now={now} />
          <TradeTape symbol={symbol} now={now} />
          <QualityPanel snapshot={snapshot} />
        </div>
      </div>
    </section>
  );
}

const STREAM_DELIVERING = new Set(["HEALTHY", "IDLE", "DEGRADED"]);

function ChartStatusBar({
  series,
  liveTimeframe,
  timeframe,
  streamHealth,
}: {
  series: ReturnType<typeof useCandles>;
  liveTimeframe: boolean;
  timeframe: string;
  streamHealth: string | undefined;
}) {
  const delivering = streamHealth !== undefined && STREAM_DELIVERING.has(streamHealth);
  const forming = series.bars.filter((b) => !b.isComplete || b.quality === "INCOMPLETE").length;
  const quarantined = series.bars.filter((b) => b.quality === "QUARANTINED").length;
  const revised = series.bars.filter((b) => b.revision > 0).length;
  return (
    <div className="border-border text-ink-muted flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-3 py-1.5 text-2xs">
      {liveTimeframe ? (
        <span className="flex items-center gap-1">
          <Radio aria-hidden className={`size-3 ${series.live === "open" && delivering ? "text-info" : ""}`} />
          {series.live !== "open"
            ? `Live updates ${humanize(series.live)}`
            : delivering
              ? "Live updates on"
              : `Live updates connected, but the provider stream is ${humanize(streamHealth ?? "unknown")}: no new bars`}
          {series.liveBars > 0 && <span className="num"> · {series.liveBars} received</span>}
        </span>
      ) : (
        <span title="The provider streams 1-minute and daily bars only">
          Stored bars only: {timeframe} is not streamed by the provider
        </span>
      )}
      <span className="num">{series.bars.length.toLocaleString()} candles</span>
      {revised > 0 && <span className="num">{revised} revised (latest revision shown)</span>}
      {forming > 0 && <span className="text-caution num">{forming} forming (faded)</span>}
      {quarantined > 0 && <span className="text-negative num">{quarantined} quarantined (not drawn)</span>}
      {series.truncated && <span className="text-caution">Range truncated at 20,000 candles</span>}
    </div>
  );
}

function ChartNotes({ series }: { series: ReturnType<typeof useCandles> }) {
  if (!series.meta && series.issues.length === 0) return null;
  return (
    <div className="border-border space-y-1 border-t px-3 py-2">
      {series.issues.length > 0 && (
        <Notice tone="negative">
          {series.issues.length} candle(s) failed client-side checks and are not drawn:{" "}
          {series.issues.slice(0, 3).map((i) => `${i.eventTime} ${i.kind} (${i.detail})`).join("; ")}
        </Notice>
      )}
      {series.meta?.notes.map((note) => (
        <p key={note} className="text-ink-faint text-2xs">{note}</p>
      ))}
    </div>
  );
}

/**
 * Markers for intervals without completeness evidence and for known gaps, placed on the
 * first candle after each interval. Several intervals ending at the same candle share
 * one marker with a count (the worst tone wins).
 */
export function gapMarkers(bars: readonly ChartBar[], completeness: CompletenessResponse | null): ChartMarker[] {
  if (!completeness || bars.length === 0) return [];
  const byTime = new Map<number, { tone: ChartMarker["tone"]; labels: string[] }>();
  const rank = { accent: 0, caution: 1, negative: 2 } as const;
  const add = (time: number, tone: ChartMarker["tone"], label: string) => {
    const entry = byTime.get(time) ?? { tone, labels: [] };
    if (rank[tone] > rank[entry.tone]) entry.tone = tone;
    entry.labels.push(label);
    byTime.set(time, entry);
  };
  const place = (endIso: string, tone: ChartMarker["tone"], label: string) => {
    const t = Math.floor(Date.parse(endIso) / 1000);
    const bar = bars[Math.min(firstAtOrAfter(bars, t), bars.length - 1)];
    if (bar) add(bar.time, tone, label);
  };
  for (const interval of completeness.missing) place(interval.end, "caution", "unverified");
  for (const gap of completeness.known_gaps) {
    place(gap.gap_end, gap.status === "UNAVAILABLE" ? "negative" : "caution", humanize(gap.status).toLowerCase());
  }
  for (const bar of bars) if (bar.quality === "QUARANTINED") add(bar.time, "negative", "quarantined");
  return [...byTime.entries()]
    .sort(([a], [b]) => a - b)
    .map(([time, { tone, labels }]) => {
      const unique = [...new Set(labels)];
      const text = labels.length === 1 ? (unique[0] ?? "") : `${labels.length}× ${unique.join("/")}`;
      return { time, tone, text };
    });
}

function CoveragePanel({
  completeness,
  error,
  timeframe,
}: {
  completeness: CompletenessResponse | null;
  error: string | null;
  timeframe: string;
}) {
  const tone = { COMPLETE: "positive", INCOMPLETE: "caution", MISSING: "negative", NOT_EXPECTED: "neutral" } as const;
  return (
    <Panel title="Data coverage" subtitle={`${timeframe} bars · last ≤31 days of the range · backend completeness check`}>
      {error && <ErrorState message={error} compact />}
      {!completeness && !error && <Loading />}
      {completeness && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge tone={tone[completeness.status]} dot>{humanize(completeness.status)}</Badge>
            <span className="text-ink-muted">
              {completeness.missing.length} interval(s) without completeness evidence ·{" "}
              {completeness.known_gaps.length} known open gap(s)
            </span>
          </div>
          {completeness.missing.length > 0 && (
            <DataTable label="Intervals without completeness evidence">
              <thead>
                <tr>
                  <th className={TH}>From</th>
                  <th className={TH}>To</th>
                  <th className={TH}>Length</th>
                </tr>
              </thead>
              <tbody>
                {completeness.missing.slice(0, 8).map((i) => (
                  <tr key={i.start}>
                    <td className={TD}><Timestamp iso={i.start} seconds={false} /></td>
                    <td className={TD}><Timestamp iso={i.end} seconds={false} /></td>
                    <td className={`${TD} num`}>{formatDuration(i.start, i.end)}</td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          )}
          {completeness.known_gaps.length > 0 && (
            <ul className="space-y-1 text-xs">
              {completeness.known_gaps.slice(0, 8).map((g) => (
                <li key={g.id} className="flex flex-wrap items-center gap-2">
                  <GapStatusBadge status={g.status} />
                  <Timestamp iso={g.gap_start} seconds={false} /> → <Timestamp iso={g.gap_end} seconds={false} />
                  <span className="text-ink-faint">{humanize(g.cause)}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="text-ink-faint text-2xs">
            Minutes without a bar inside verified time had no trades on this feed; they are not missing data.
            Unverified intervals have no retrieval or uninterrupted stream proving completeness.
          </p>
        </div>
      )}
    </Panel>
  );
}

function QuotePanel({ snapshot, now }: { snapshot: AssetSnapshot; now: number }) {
  const { stream } = useDashboardStatus();
  const quote = snapshot.latest_quote;
  const trade = snapshot.latest_trade;
  const bid = quote ? quoteSide(quote.bid_price, quote.bid_size) : null;
  const ask = quote ? quoteSide(quote.ask_price, quote.ask_size) : null;
  const streamHealth = stream.data?.health;
  const streamDown = streamHealth !== undefined && ["NOT_RUNNING", "FAILED", "STALE", "DISABLED"].includes(streamHealth);
  return (
    <Panel title="Quote and last trade" subtitle={`${snapshot.feed.toUpperCase()} only`}>
      {streamDown && (
        <div className="mb-2">
          <Notice tone={streamTone(streamHealth)}>
            Live stream {humanize(streamHealth ?? "")}: values below are the last stored, not current.
          </Notice>
        </div>
      )}
      <KeyValues
        items={[
          {
            label: "Bid",
            value: quote ? (
              bid ? <span className="num">{formatPrice(bid.price)} <span className="text-ink-faint">× {formatQuantity(bid.size)}</span></span>
                : <span className="text-ink-faint" title="One-sided quote: price 0 means no bid on this feed">no bid</span>
            ) : MISSING,
          },
          {
            label: "Ask",
            value: quote ? (
              ask ? <span className="num">{formatPrice(ask.price)} <span className="text-ink-faint">× {formatQuantity(ask.size)}</span></span>
                : <span className="text-ink-faint" title="One-sided quote: price 0 means no ask on this feed">no ask</span>
            ) : MISSING,
          },
          { label: "Quote time", value: quote ? <span><Timestamp iso={quote.event_time} /> <span className="text-ink-faint">{formatAge(quote.event_time, now)}</span></span> : MISSING },
          { label: "Last trade", value: trade ? <span className="num">{formatPrice(trade.price)} <span className="text-ink-faint">× {formatQuantity(trade.size)}</span></span> : MISSING },
          { label: "Trade time", value: trade ? <span><Timestamp iso={trade.event_time} /> <span className="text-ink-faint">{formatAge(trade.event_time, now)}</span></span> : MISSING },
          { label: "Sources", value: <span className="flex gap-1">{quote && <SourceBadge source={quote.source} />}{trade && <SourceBadge source={trade.source} />}</span> },
        ]}
      />
      {!quote && (
        <p className="text-ink-muted mt-2 text-2xs">
          No stored quotes. Quotes are collected only by the live stream; quote history is not recovered by
          default (MARKET_DATA_RECOVERY_DATA_TYPES), so earlier quotes are unavailable, not zero.
        </p>
      )}
      <p className="text-ink-faint mt-2 text-2xs">Sizes are in the provider's units (Alpaca: round lots for quotes).</p>
    </Panel>
  );
}

function TradeTape({ symbol, now }: { symbol: string; now: number }) {
  const resource = useApiResource(
    (signal) => {
      const end = new Date();
      const start = new Date(end.getTime() - 7 * DAY_MS);
      return marketData.trades(
        { symbol, start: start.toISOString(), end: end.toISOString(), limit: 25, newest_first: true },
        { signal },
      );
    },
    [symbol],
    { isEmpty: (d) => d.trades.length === 0 },
  );
  // A new stored trade for this symbol refetches the (small) tape, at most every 2 s,
  // keeping the current rows visible meanwhile.
  const lastRefetch = useRef(0);
  useLiveUpdates({
    trade: (trade) => {
      if (trade.symbol !== symbol || Date.now() - lastRefetch.current < 2_000) return;
      lastRefetch.current = Date.now();
      resource.reload();
    },
  });
  const trades: Trade[] = resource.data?.trades ?? [];
  return (
    <Panel title="Recent trades" subtitle="last 25 stored · 7 days" bodyClassName="">
      {resource.status === "loading" && <Loading />}
      {resource.status === "error" && <div className="p-2"><ErrorState message={resource.error ?? ""} onRetry={resource.reload} compact /></div>}
      {resource.status === "empty" && <div className="px-3"><EmptyState title="No stored trades in the last 7 days" /></div>}
      {trades.length > 0 && (
        <DataTable label="Recent trades">
          <thead>
            <tr>
              <th className={TH}>Time</th>
              <th className={`${TH} text-right`}>Price</th>
              <th className={`${TH} text-right`}>Size</th>
              <th className={TH}>Src</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.id}>
                <td className={`${TD} num text-ink-muted`} title={t.event_time}>{formatAge(t.event_time, now)}</td>
                <td className={`${TD} num text-right`}>{formatPrice(t.price)}</td>
                <td className={`${TD} num text-right`}>{formatQuantity(t.size)}</td>
                <td className={TD}>{t.source === "LIVE_STREAM" ? "S" : "H"}</td>
              </tr>
            ))}
          </tbody>
        </DataTable>
      )}
    </Panel>
  );
}

function QualityPanel({ snapshot }: { snapshot: AssetSnapshot }) {
  const resource = useApiResource(
    (signal) => marketData.qualityEvents({ asset_id: snapshot.asset.asset_id, limit: 15 }, { signal }),
    [snapshot.asset.asset_id],
    { isEmpty: (d) => d.length === 0, refreshMs: 60_000 },
  );
  const counts = snapshot.quality_events_24h;
  return (
    <Panel title="Data quality" subtitle="events for this asset" bodyClassName="">
      <div className="flex flex-wrap gap-1.5 px-3 pt-2 text-2xs">
        {["CRITICAL", "ERROR", "WARNING", "INFO"].map((s) => (
          <span key={s} className="flex items-center gap-1">
            <SeverityBadge severity={s} /> <span className="num">{counts[s] ?? 0}</span>
          </span>
        ))}
        <span className="text-ink-faint">last 24h</span>
        {snapshot.open_gaps > 0 && <Badge tone="caution">{snapshot.open_gaps} open gap(s)</Badge>}
      </div>
      {resource.status === "loading" && <Loading />}
      {resource.status === "error" && <div className="p-2"><ErrorState message={resource.error ?? ""} onRetry={resource.reload} compact /></div>}
      {resource.status === "empty" && <div className="px-3"><EmptyState title="No quality events recorded" /></div>}
      {resource.data && resource.data.length > 0 && (
        <ul className="divide-border/60 mt-1 divide-y">
          {resource.data.map((e) => (
            <li key={e.id} className="px-3 py-1.5 text-xs">
              <div className="flex items-center gap-1.5">
                <SeverityBadge severity={e.severity} />
                <span className="font-medium">{humanize(e.event_type)}</span>
                <span className="text-ink-faint ml-auto"><Timestamp iso={e.detected_at} seconds={false} /></span>
              </div>
              <p className="text-ink-muted mt-0.5 line-clamp-2" title={e.message}>{e.message}</p>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
