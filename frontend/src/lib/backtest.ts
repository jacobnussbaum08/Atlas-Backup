import type { MetricRow, MetricStatus, RunStatus } from "../types/backtests";
import { MISSING } from "./format";
import { formatDecimal, toPlain } from "./money";

/** A ratio string ("0.0123") as an exact percentage string ("1.23"), no floating point. */
export function ratioToPercentString(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value === "") return null;
  const plain = toPlain(value);
  const match = /^([+-]?)(\d+)(?:\.(\d+))?$/.exec(plain);
  if (!match) return null;
  const [, sign = "", integer = "0", fraction = ""] = match;
  const padded = fraction.padEnd(2, "0");
  const shifted = `${integer}${padded.slice(0, 2)}`.replace(/^0+(?=\d)/, "");
  const rest = padded.slice(2);
  return `${sign}${shifted}${rest ? `.${rest}` : ""}`;
}

export function formatRatio(value: string | null | undefined, { signed = true } = {}): string {
  const percent = ratioToPercentString(value);
  if (percent === null) return MISSING;
  return `${formatDecimal(percent, { places: 2, signed })}%`;
}

export function metricValue(row: MetricRow | undefined): string {
  if (!row || row.value === null) return MISSING;
  if (row.unit === "ratio" && /return|drawdown|rate|exposure|costs_pct/.test(row.metric)) {
    return formatRatio(row.value, { signed: /return/.test(row.metric) });
  }
  if (row.unit === "USD") return formatDecimal(row.value, { places: 2, signed: /pnl/.test(row.metric) });
  if (row.unit === "count") return formatDecimal(row.value, { places: 0 });
  return formatDecimal(row.value, { places: row.unit === "sessions" ? 2 : 4 });
}

export function metricMap(rows: MetricRow[], scope = "PORTFOLIO", key = "portfolio"): Map<string, MetricRow> {
  return new Map(rows.filter((r) => r.scope === scope && r.scope_key === key).map((r) => [r.metric, r]));
}

export const STATUS_TONE: Record<RunStatus, "positive" | "negative" | "info" | "neutral"> = {
  COMPLETED: "positive",
  FAILED: "negative",
  RUNNING: "info",
  QUEUED: "neutral",
};

export const METRIC_STATUS_LABEL: Record<MetricStatus, string> = {
  OK: "",
  INSUFFICIENT_SAMPLE: "small sample",
  NOT_APPLICABLE: "n/a",
};

/** Decimal strings to numbers for drawing a chart only (never for displayed values). */
export function chartNumber(value: string): number {
  return Number(value);
}
