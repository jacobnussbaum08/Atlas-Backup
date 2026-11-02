/**
 * Candle handling for the price chart. Pure functions, unit-tested.
 *
 * Rules (backend: market_bars is revision-aware and append-only):
 * - One candle per bar timestamp. The highest revision wins; a lower or equal revision
 *   arriving later is a duplicate and is ignored.
 * - Candles are sorted by time and must sit on the timeframe's grid (intraday: multiples
 *   of the bar length in UTC; daily: New York midnight). Off-grid or impossible OHLC
 *   values are reported and not drawn, never "fixed".
 * - QUARANTINED candles are not drawn; their slot stays empty and is marked.
 * - Live bars follow Alpaca's bar semantics: a minute bar is published once its minute
 *   is over (LIVE_INITIAL), late trades produce a revision (LIVE_UPDATED), the daily bar
 *   is re-published while it forms (LIVE_RUNNING, incomplete). No candle is ever
 *   synthesised from trades.
 * - Missing volume stays missing (no volume bar), distinct from a genuine zero.
 */
import type { Candle, LiveBar } from "../types/marketData";

export const CHART_TIMEFRAMES = ["1Min", "5Min", "15Min", "1Hour", "1Day"] as const;
export type ChartTimeframe = (typeof CHART_TIMEFRAMES)[number];

export const TIMEFRAME_SECONDS: Record<ChartTimeframe, number> = {
  "1Min": 60,
  "5Min": 300,
  "15Min": 900,
  "1Hour": 3600,
  "1Day": 86_400,
};

/** Timeframes the provider streams; others change only when history is retrieved. */
export const LIVE_TIMEFRAMES: ReadonlySet<string> = new Set(["1Min", "1Day"]);

/** Statuses the chart requests: valid bars, still-forming bars, and quarantined slots. */
export const CHART_QUALITY = ["VALID", "INCOMPLETE", "QUARANTINED"] as const;

export interface ChartBar {
  time: number; // UTC seconds of the bar's start
  eventTime: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  revision: number;
  isComplete: boolean;
  quality: string;
  source: string;
  observationKind: string;
}

export type CandleIssueKind = "invalid_ohlc" | "off_grid" | "duplicate" | "out_of_order";

export interface CandleIssue {
  kind: CandleIssueKind;
  eventTime: string;
  detail: string;
}

type BarLike = Pick<
  Candle,
  | "event_time"
  | "open"
  | "high"
  | "low"
  | "close"
  | "volume"
  | "revision"
  | "is_complete"
  | "quality_status"
  | "source"
  | "observation_kind"
>;

const NEW_YORK = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hourCycle: "h23",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

export function isOnGrid(seconds: number, timeframe: ChartTimeframe): boolean {
  if (timeframe === "1Day") {
    return NEW_YORK.format(new Date(seconds * 1000)) === "00:00:00";
  }
  return seconds % TIMEFRAME_SECONDS[timeframe] === 0;
}

export function toChartBar(bar: BarLike): ChartBar | null {
  const values = [bar.open, bar.high, bar.low, bar.close].map(Number);
  if (values.some((v) => !Number.isFinite(v))) return null;
  const [open, high, low, close] = values as [number, number, number, number];
  const volume = bar.volume === null ? null : Number(bar.volume);
  return {
    time: Math.floor(Date.parse(bar.event_time) / 1000),
    eventTime: bar.event_time,
    open,
    high,
    low,
    close,
    volume: volume !== null && Number.isFinite(volume) ? volume : null,
    revision: bar.revision,
    isComplete: bar.is_complete,
    quality: bar.quality_status,
    source: bar.source,
    observationKind: bar.observation_kind,
  };
}

export function ohlcProblem(bar: ChartBar): string | null {
  if (bar.low > bar.high) return `low ${bar.low} above high ${bar.high}`;
  if (bar.open > bar.high || bar.open < bar.low) return `open ${bar.open} outside low-high`;
  if (bar.close > bar.high || bar.close < bar.low) return `close ${bar.close} outside low-high`;
  if (bar.volume !== null && bar.volume < 0) return `negative volume ${bar.volume}`;
  return null;
}

/**
 * Turn API candles into a strictly ascending, one-per-timestamp series.
 * Problems are returned, not hidden.
 */
export function normalizeCandles(
  candles: readonly BarLike[],
  timeframe: ChartTimeframe,
): { bars: ChartBar[]; issues: CandleIssue[] } {
  const issues: CandleIssue[] = [];
  const byTime = new Map<number, ChartBar>();
  let previous = -Infinity;
  for (const candle of candles) {
    const bar = toChartBar(candle);
    if (bar === null) {
      issues.push({ kind: "invalid_ohlc", eventTime: candle.event_time, detail: "non-numeric" });
      continue;
    }
    if (bar.time < previous) {
      issues.push({ kind: "out_of_order", eventTime: bar.eventTime, detail: "sorted locally" });
    }
    previous = Math.max(previous, bar.time);
    const problem = ohlcProblem(bar);
    if (problem) {
      issues.push({ kind: "invalid_ohlc", eventTime: bar.eventTime, detail: problem });
      continue;
    }
    if (!isOnGrid(bar.time, timeframe)) {
      issues.push({ kind: "off_grid", eventTime: bar.eventTime, detail: `not on the ${timeframe} grid` });
      continue;
    }
    const existing = byTime.get(bar.time);
    if (existing) {
      issues.push({
        kind: "duplicate",
        eventTime: bar.eventTime,
        detail: `revisions ${existing.revision} and ${bar.revision}; highest kept`,
      });
      if (bar.revision <= existing.revision) continue;
    }
    byTime.set(bar.time, bar);
  }
  const bars = [...byTime.values()].sort((a, b) => a.time - b.time);
  return { bars, issues };
}

export type LiveAction = "append" | "update" | "insert" | "ignore" | "reload";

/**
 * Apply one live bar to the current series. Returns the new series (unchanged array when
 * ignored) and what the chart has to do:
 * - append/update: the last bar is new or revised (cheap incremental chart update),
 * - insert: an earlier bar arrived (e.g. a backfill) or was revised: redraw the series,
 * - reload: a bar's current revision is no longer displayable: refetch from the API.
 */
export function applyLiveBar(
  bars: readonly ChartBar[],
  live: LiveBar,
  timeframe: ChartTimeframe,
): { bars: readonly ChartBar[]; action: LiveAction } {
  if (live.timeframe !== timeframe) return { bars, action: "ignore" };
  const bar = toChartBar(live);
  if (bar === null || ohlcProblem(bar) !== null || !isOnGrid(bar.time, timeframe)) {
    return { bars, action: "ignore" };
  }
  const index = findIndex(bars, bar.time);
  const existing = index >= 0 ? bars[index] : undefined;
  if (existing && bar.revision <= existing.revision) return { bars, action: "ignore" };
  if (!(CHART_QUALITY as readonly string[]).includes(bar.quality)) {
    // e.g. a revision was rejected: the API decides what is current now.
    return { bars, action: existing ? "reload" : "ignore" };
  }
  const last = bars[bars.length - 1];
  if (existing) {
    const next = bars.slice();
    next[index] = bar;
    return { bars: next, action: index === bars.length - 1 ? "update" : "insert" };
  }
  if (!last || bar.time > last.time) return { bars: [...bars, bar], action: "append" };
  const next = [...bars, bar].sort((a, b) => a.time - b.time);
  return { bars: next, action: "insert" };
}

function findIndex(bars: readonly ChartBar[], time: number): number {
  let lo = 0;
  let hi = bars.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const value = (bars[mid] as ChartBar).time;
    if (value === time) return mid;
    if (value < time) lo = mid + 1;
    else hi = mid - 1;
  }
  return -1;
}

/** Index of the first bar at or after ``time`` (bars.length when none). */
export function firstAtOrAfter(bars: readonly ChartBar[], time: number): number {
  let lo = 0;
  let hi = bars.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((bars[mid] as ChartBar).time < time) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export interface RangeOption {
  label: string;
  days: number;
}

/** Selectable history per timeframe (calendar days back from now), first = default. */
export const RANGE_OPTIONS: Record<ChartTimeframe, RangeOption[]> = {
  "1Min": [
    { label: "4D", days: 4 },
    { label: "1D", days: 1 },
    { label: "7D", days: 7 },
  ],
  "5Min": [
    { label: "7D", days: 7 },
    { label: "14D", days: 14 },
    { label: "1M", days: 31 },
  ],
  "15Min": [
    { label: "14D", days: 14 },
    { label: "1M", days: 31 },
    { label: "3M", days: 92 },
  ],
  "1Hour": [
    { label: "1M", days: 31 },
    { label: "3M", days: 92 },
    { label: "6M", days: 183 },
  ],
  "1Day": [
    { label: "6M", days: 183 },
    { label: "1Y", days: 366 },
    { label: "3Y", days: 1096 },
  ],
};

/** Upper bound on candles loaded for one chart (pages of the API's 10,000 maximum). */
export const MAX_CHART_CANDLES = 20_000;
