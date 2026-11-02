import {
  CandlestickSeries,
  type CandlestickData,
  ColorType,
  createChart,
  createSeriesMarkers,
  CrosshairMode,
  HistogramSeries,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  LineStyle,
  type SeriesMarker,
  type Time,
  TickMarkType,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";

import type { ChartBar, ChartTimeframe, LiveAction } from "../../lib/candles";
import { formatPrice, formatQuantity, humanize } from "../../lib/format";
import { type DisplayZone, timeZoneId, useDisplayZone, zoneLabel } from "../../lib/time";

/** Colours come from the theme tokens so the chart matches tables and badges. */
function themeColor(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(`--color-${name}`).trim();
  return value || fallback;
}

export interface ChartMarker {
  time: number;
  tone: "caution" | "negative" | "accent";
  text: string;
}

export interface ChartUpdate {
  /** Increments on every change so effects run once per change. */
  version: number;
  action: LiveAction | "load";
}

function formatter(zone: DisplayZone, options: Intl.DateTimeFormatOptions) {
  const tz = timeZoneId(zone);
  return new Intl.DateTimeFormat("en-GB", { hourCycle: "h23", ...options, ...(tz ? { timeZone: tz } : {}) });
}

export function tickLabel(seconds: number, type: TickMarkType, zone: DisplayZone): string {
  const date = new Date(seconds * 1000);
  switch (type) {
    case TickMarkType.Year:
      return formatter(zone, { year: "numeric" }).format(date);
    case TickMarkType.Month:
      return formatter(zone, { month: "short" }).format(date);
    case TickMarkType.DayOfMonth:
      return formatter(zone, { day: "2-digit", month: "short" }).format(date);
    case TickMarkType.TimeWithSeconds:
      return formatter(zone, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
    default:
      return formatter(zone, { hour: "2-digit", minute: "2-digit" }).format(date);
  }
}

function crosshairLabel(seconds: number, zone: DisplayZone, daily: boolean): string {
  const date = new Date(seconds * 1000);
  const text = formatter(zone, daily
    ? { year: "numeric", month: "short", day: "2-digit" }
    : { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
  return daily ? text : `${text} ${zoneLabel(zone, date)}`;
}

/**
 * Candlestick + volume chart (TradingView Lightweight Charts). Receives an already
 * validated, ascending, one-per-timestamp series (``lib/candles``). Incremental live
 * changes use ``series.update`` on the last bar; anything else redraws the series.
 */
export function PriceChart({
  bars,
  timeframe,
  update,
  markers,
  lastTrade,
  height = 420,
}: {
  bars: readonly ChartBar[];
  timeframe: ChartTimeframe;
  update: ChartUpdate;
  markers: readonly ChartMarker[];
  lastTrade: number | null;
  height?: number;
}) {
  const { zone } = useDisplayZone();
  const container = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLineRef = useRef<IPriceLine | null>(null);
  const barsRef = useRef(bars);
  barsRef.current = bars;
  const [hovered, setHovered] = useState<ChartBar | null>(null);
  const daily = timeframe === "1Day";

  const colors = useMemo(
    () => ({
      up: themeColor("positive", "#26a69a"),
      down: themeColor("negative", "#ef5350"),
      text: themeColor("ink-muted", "#8a95a5"),
      grid: themeColor("border", "#1f2732"),
      panel: themeColor("panel", "#0f141b"),
      caution: themeColor("caution", "#e5a50a"),
      accent: themeColor("accent", "#4c9eeb"),
      info: themeColor("info", "#38bdf8"),
    }),
    [],
  );

  // Create the chart once per mount; dispose on unmount.
  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: colors.panel },
        textColor: colors.text,
        fontFamily: "JetBrains Mono, ui-monospace, Consolas, monospace",
        fontSize: 11,
      },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: colors.grid },
      timeScale: { borderColor: colors.grid, timeVisible: true, secondsVisible: false, rightOffset: 4 },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: colors.up,
      downColor: colors.down,
      borderVisible: false,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
    chartRef.current = chart;
    candleRef.current = candles;
    volumeRef.current = volume;
    markersRef.current = createSeriesMarkers(candles, []);
    chart.subscribeCrosshairMove((param) => {
      if (param.time === undefined) {
        setHovered(null);
        return;
      }
      const t = param.time as number;
      setHovered(barsRef.current.find((b) => b.time === t) ?? null);
    });
    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      markersRef.current = null;
      priceLineRef.current = null;
    };
  }, [colors]);

  // Time axis and crosshair in the display zone.
  useEffect(() => {
    chartRef.current?.applyOptions({
      localization: { timeFormatter: (time: Time) => crosshairLabel(time as number, zone, daily) },
      timeScale: {
        timeVisible: !daily,
        tickMarkFormatter: (time: Time, type: TickMarkType) => tickLabel(time as number, type, zone),
      },
    });
  }, [zone, daily]);

  // Data: incremental for the last bar, full redraw otherwise.
  useEffect(() => {
    const candles = candleRef.current;
    const volume = volumeRef.current;
    if (!candles || !volume) return;
    const last = bars[bars.length - 1];
    if ((update.action === "append" || update.action === "update") && last) {
      candles.update(toCandle(last, colors));
      volume.update(toVolume(last, colors));
      return;
    }
    candles.setData(bars.map((b) => toCandle(b, colors)));
    volume.setData(bars.map((b) => toVolume(b, colors)));
    if (update.action === "load") chartRef.current?.timeScale().fitContent();
  }, [update.version]);

  useEffect(() => {
    const toneColor = { caution: colors.caution, negative: colors.down, accent: colors.accent };
    const list: SeriesMarker<Time>[] = markers.map((m) => ({
      time: m.time as UTCTimestamp,
      position: "aboveBar",
      shape: "square",
      color: toneColor[m.tone],
      text: m.text,
      size: 0.6,
    }));
    markersRef.current?.setMarkers(list);
  }, [markers, colors]);

  useEffect(() => {
    const candles = candleRef.current;
    if (!candles) return;
    if (lastTrade === null) {
      if (priceLineRef.current) candles.removePriceLine(priceLineRef.current);
      priceLineRef.current = null;
      return;
    }
    if (priceLineRef.current) {
      priceLineRef.current.applyOptions({ price: lastTrade });
    } else {
      priceLineRef.current = candles.createPriceLine({
        price: lastTrade,
        color: colors.info,
        lineStyle: LineStyle.Dotted,
        lineWidth: 1,
        axisLabelVisible: true,
        title: "last trade",
      });
    }
  }, [lastTrade, colors]);

  const shown = hovered ?? bars[bars.length - 1] ?? null;
  return (
    <div className="relative">
      {shown && <Legend bar={shown} hovered={hovered !== null} />}
      <div ref={container} style={{ height }} data-testid="price-chart" />
    </div>
  );
}

function toCandle(
  bar: ChartBar,
  colors: { up: string; down: string },
): CandlestickData<Time> | WhitespaceData<Time> {
  const time = bar.time as UTCTimestamp;
  if (bar.quality === "QUARANTINED") return { time }; // empty slot, marked separately
  const forming = !bar.isComplete || bar.quality === "INCOMPLETE";
  const base = bar.close >= bar.open ? colors.up : colors.down;
  const color = forming ? `${base}66` : base;
  return { time, open: bar.open, high: bar.high, low: bar.low, close: bar.close, color, wickColor: color };
}

function toVolume(
  bar: ChartBar,
  colors: { up: string; down: string },
): HistogramData<Time> | WhitespaceData<Time> {
  const time = bar.time as UTCTimestamp;
  // Missing volume stays missing: no bar, not a zero.
  if (bar.volume === null || bar.quality === "QUARANTINED") return { time };
  const base = bar.close >= bar.open ? colors.up : colors.down;
  return { time, value: bar.volume, color: `${base}55` };
}

function Legend({ bar, hovered }: { bar: ChartBar; hovered: boolean }) {
  const up = bar.close >= bar.open;
  const tone = up ? "text-positive" : "text-negative";
  return (
    <div className="num pointer-events-none absolute top-1 left-2 z-10 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs">
      {(["open", "high", "low", "close"] as const).map((k) => (
        <span key={k}>
          <span className="text-ink-faint">{k[0]?.toUpperCase()}</span>{" "}
          <span className={tone}>{formatPrice(bar[k])}</span>
        </span>
      ))}
      <span>
        <span className="text-ink-faint">V</span> {bar.volume === null ? "n/a" : formatQuantity(bar.volume)}
      </span>
      <span className="text-ink-faint">
        rev {bar.revision} · {humanize(bar.observationKind)} · {bar.source === "LIVE_STREAM" ? "stream" : "history"}
        {bar.quality !== "VALID" && <span className="text-caution"> · {bar.quality}</span>}
        {!bar.isComplete && <span className="text-caution"> · forming</span>}
        {!hovered && " · latest"}
      </span>
    </div>
  );
}
