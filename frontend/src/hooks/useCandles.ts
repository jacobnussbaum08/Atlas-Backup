import { useCallback, useEffect, useRef, useState } from "react";

import {
  applyLiveBar,
  CHART_QUALITY,
  type CandleIssue,
  type ChartBar,
  type ChartTimeframe,
  type LiveAction,
  MAX_CHART_CANDLES,
  normalizeCandles,
} from "../lib/candles";
import type { LiveConnectionState } from "../services/liveUpdates";
import { marketData } from "../services/marketData";
import type { Candle, CandlesResponse, LiveBar } from "../types/marketData";
import { useLiveUpdates } from "./useLiveUpdates";

const PAGE_SIZE = 10_000;

export interface CandleSeries {
  status: "loading" | "success" | "empty" | "error";
  bars: readonly ChartBar[];
  issues: CandleIssue[];
  meta: Omit<CandlesResponse, "candles"> | null;
  error: string | null;
  truncated: boolean;
  update: { version: number; action: LiveAction | "load" };
  liveBars: number;
  live: LiveConnectionState;
  reload: () => void;
}

/** Page through /candles (``next_after``) until the range is loaded or the cap is hit. */
export async function loadCandles(
  query: { symbol: string; timeframe: string; start: string; end: string },
  signal: AbortSignal,
): Promise<{ response: CandlesResponse; candles: Candle[]; truncated: boolean }> {
  let after: string | undefined;
  const candles: Candle[] = [];
  let response: CandlesResponse;
  for (;;) {
    response = await marketData.candles(
      { ...query, quality: CHART_QUALITY, limit: PAGE_SIZE, ...(after ? { after } : {}) },
      { signal },
    );
    candles.push(...response.candles);
    if (!response.next_after) return { response, candles, truncated: false };
    if (candles.length >= MAX_CHART_CANDLES) return { response, candles, truncated: true };
    after = response.next_after;
  }
}

/**
 * Stored candles for one symbol/timeframe/range, then live bars from the shared stream.
 * Live bars only exist for the timeframes the provider streams (1Min, 1Day); for others
 * the subscription is not opened.
 */
export function useCandles(
  symbol: string,
  timeframe: ChartTimeframe,
  range: { start: string; end: string },
  { live: liveEnabled }: { live: boolean },
): CandleSeries {
  const [state, setState] = useState<Omit<CandleSeries, "reload" | "live">>({
    status: "loading",
    bars: [],
    issues: [],
    meta: null,
    error: null,
    truncated: false,
    update: { version: 0, action: "load" },
    liveBars: 0,
  });
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  const loaded = useRef(false);
  // Live bars that arrive while the history is still loading are applied afterwards.
  const pending = useRef<LiveBar[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    loaded.current = false;
    pending.current = [];
    setState((s) => ({ ...s, status: "loading", error: null }));
    loadCandles({ symbol, timeframe, start: range.start, end: range.end }, controller.signal)
      .then(({ response, candles, truncated }) => {
        const { candles: _omit, ...meta } = response;
        let { bars, issues } = normalizeCandles(candles, timeframe);
        for (const live of pending.current) bars = applyLiveBar(bars, live, timeframe).bars as ChartBar[];
        pending.current = [];
        loaded.current = true;
        setState((s) => ({
          status: bars.length === 0 ? "empty" : "success",
          bars,
          issues,
          meta,
          error: null,
          truncated,
          update: { version: s.update.version + 1, action: "load" },
          liveBars: 0,
        }));
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setState((s) => ({
          ...s,
          status: "error",
          error: reason instanceof Error ? reason.message : "Unknown error",
        }));
      });
    return () => controller.abort();
  }, [symbol, timeframe, range.start, range.end, tick]);

  const live = useLiveUpdates(
    {
      bar: (bar) => {
        if (bar.symbol !== symbol || bar.timeframe !== timeframe) return;
        if (Date.parse(bar.event_time) < Date.parse(range.start)) return;
        if (!loaded.current) {
          pending.current.push(bar);
          return;
        }
        setState((s) => {
          const result = applyLiveBar(s.bars, bar, timeframe);
          if (result.action === "ignore") return s;
          if (result.action === "reload") {
            reload();
            return s;
          }
          return {
            ...s,
            status: "success",
            bars: result.bars,
            update: { version: s.update.version + 1, action: result.action },
            liveBars: s.liveBars + 1,
          };
        });
      },
      resync: () => reload(),
    },
    liveEnabled,
  );

  return { ...state, live, reload };
}
