import type { MeasurementStatus, ResultStatus, Verdict } from "../types/validation";
import { MISSING } from "./format";

/**
 * Display helpers for validation statistics. These are analytical numbers (JSON floats)
 * computed from the Decimal replays; they are formatted for reading only.
 */
export function pct(value: number | null | undefined, { signed = true, places = 2 } = {}): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return MISSING;
  const s = (value * 100).toFixed(places);
  return `${signed && value > 0 ? "+" : ""}${s}%`;
}

export function num(value: number | string | null | undefined, places = 2, { signed = false } = {}): string {
  if (value === null || value === undefined || value === "") return MISSING;
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return MISSING;
  return `${signed && n > 0 ? "+" : ""}${n.toLocaleString("en-US", { minimumFractionDigits: places, maximumFractionDigits: places })}`;
}

export const VERDICT_TONE: Record<Verdict, "caution" | "negative" | "info"> = {
  INSUFFICIENT_EVIDENCE: "caution",
  NOT_ROBUST: "negative",
  CANDIDATE_FOR_REVIEW: "info",
};

export const VERDICT_LABEL: Record<Verdict, string> = {
  INSUFFICIENT_EVIDENCE: "insufficient evidence",
  NOT_ROBUST: "not robust",
  CANDIDATE_FOR_REVIEW: "candidate for human review",
};

export const SAMPLE_LABEL: Record<ResultStatus, string> = {
  OK: "",
  INSUFFICIENT_SAMPLE: "small sample",
  NOT_APPLICABLE: "n/a",
  NOT_RUN: "not run",
};

export const MEASUREMENT_TONE: Record<MeasurementStatus, "positive" | "caution" | "negative" | "neutral"> = {
  OK: "positive",
  WARNING: "caution",
  BREACH: "negative",
  INSUFFICIENT_DATA: "neutral",
};

/** Equity points for drawing only, compounded from stored daily returns across windows. */
export function stitchedSeries(windows: [string, number, number][][], start = 100_000): [string, number][] {
  const out: [string, number][] = [];
  let value = start;
  for (const series of windows) {
    for (const [day, r] of series) {
      value *= 1 + r;
      out.push([day, value]);
    }
  }
  return out;
}

export function drawdownSeries(equity: [string, number][], start = 100_000): [string, number][] {
  let peak = start;
  return equity.map(([day, v]) => {
    peak = Math.max(peak, v);
    return [day, peak > 0 ? -((peak - v) / peak) * 100 : 0];
  });
}
