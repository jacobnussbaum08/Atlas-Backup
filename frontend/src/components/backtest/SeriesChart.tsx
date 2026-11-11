import { AreaSeries, ColorType, createChart, type IChartApi, LineSeries, type Time } from "lightweight-charts";
import { useEffect, useMemo, useRef } from "react";

/** Colours come from the theme tokens so the chart matches tables and badges. */
function themeColor(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(`--color-${name}`).trim();
  return value || fallback;
}

export interface ChartLine {
  label: string;
  tone: "accent" | "info" | "caution" | "negative" | "neutral";
  /** [session date (YYYY-MM-DD), value]; values are numbers for drawing only. */
  points: readonly (readonly [string, number])[];
  area?: boolean;
}

/**
 * Daily lines of one backtest (equity, gross equity, benchmark, drawdown). The values are
 * the stored results; the chart only draws them.
 */
export function SeriesChart({ lines, height = 260, label }: { lines: ChartLine[]; height?: number; label: string }) {
  const container = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const colors = useMemo(
    () => ({
      accent: themeColor("accent", "#4c9eeb"),
      info: themeColor("info", "#38bdf8"),
      caution: themeColor("caution", "#e5a50a"),
      negative: themeColor("negative", "#ef5350"),
      neutral: themeColor("ink-faint", "#5b6573"),
      text: themeColor("ink-muted", "#8a95a5"),
      grid: themeColor("border", "#1f2732"),
      panel: themeColor("panel", "#0f141b"),
    }),
    [],
  );

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
      rightPriceScale: { borderColor: colors.grid },
      timeScale: { borderColor: colors.grid },
      // A results page is read by scrolling: the wheel scrolls the page, not the chart.
      handleScroll: { mouseWheel: false },
      handleScale: { mouseWheel: false },
    });
    chartRef.current = chart;
    for (const line of lines) {
      const color = colors[line.tone];
      const series = line.area
        ? chart.addSeries(AreaSeries, {
            lineColor: color,
            topColor: `${color}00`,
            bottomColor: `${color}55`,
            lineWidth: 1,
            title: line.label,
          })
        : chart.addSeries(LineSeries, { color, lineWidth: 2, title: line.label });
      series.setData(line.points.map(([time, value]) => ({ time: time as Time, value })));
    }
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [lines, colors]);

  return (
    <figure aria-label={label} className="m-0">
      <div ref={container} style={{ height }} className="w-full" />
      <figcaption className="text-ink-faint mt-1 flex flex-wrap gap-3 text-2xs">
        {lines.map((line) => (
          <span key={line.label} className="inline-flex items-center gap-1">
            <span aria-hidden className="inline-block h-0.5 w-3" style={{ background: colors[line.tone] }} />
            {line.label}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}
