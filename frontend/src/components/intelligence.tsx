import { Badge, type Tone } from "./ui";
import { humanize, MISSING, toNumber } from "../lib/format";
import type { Classification, CorrelationCell, Dimension, RegimePoint } from "../types/intelligence";

/** Phase 3B visual vocabulary: labels, probability bars, correlation heatmap, regime timeline. */

const TONES: Record<string, Tone> = {
  BULLISH: "positive",
  BEARISH: "negative",
  SIDEWAYS: "neutral",
  TRANSITIONING: "caution",
  LOW: "info",
  NORMAL: "neutral",
  ELEVATED: "caution",
  EXTREME: "negative",
  STABLE: "positive",
  UNSETTLED: "caution",
  STRESSED: "negative",
  UNCERTAIN: "accent",
};

const FILLS: Record<Tone, string> = {
  positive: "var(--color-positive)",
  negative: "var(--color-negative)",
  caution: "var(--color-caution)",
  info: "var(--color-info)",
  accent: "var(--color-accent)",
  neutral: "var(--color-ink-faint)",
};

export function labelTone(label: string): Tone {
  return TONES[label] ?? "neutral";
}

export function LabelBadge({ label, title }: { label: string; title?: string }) {
  return (
    <Badge tone={labelTone(label)} dot {...(title ? { title } : {})}>
      {humanize(label)}
    </Badge>
  );
}

/** A fraction (0.42) as a whole percentage ("42%"); missing stays missing. */
export function share(value: string | number | null | undefined, digits = 0): string {
  const n = toNumber(value);
  return n === null ? MISSING : `${(n * 100).toFixed(digits)}%`;
}

/** A signed fraction (0.0123) as a signed percentage ("+1.23%"). */
export function signedShare(value: string | number | null | undefined, digits = 2): string {
  const n = toNumber(value);
  if (n === null) return MISSING;
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}${Math.abs(n * 100).toFixed(digits)}%`;
}

export function ProbabilityBar({
  classification,
  labels,
}: {
  classification: Classification;
  labels: readonly string[];
}) {
  const parts = labels
    .map((label) => ({ label, p: toNumber(classification.probabilities[label]) ?? 0 }))
    .filter((part) => part.p > 0);
  const summary = parts.map((part) => `${humanize(part.label)} ${share(part.p)}`).join(", ");
  return (
    <div>
      <div
        role="img"
        aria-label={`${humanize(classification.dimension)} probabilities: ${summary}`}
        className="border-border flex h-2.5 w-full overflow-hidden rounded-sm border"
      >
        {parts.map((part) => (
          <span
            key={part.label}
            title={`${humanize(part.label)} ${share(part.p, 1)}`}
            style={{ width: `${part.p * 100}%`, background: FILLS[labelTone(part.label)] }}
          />
        ))}
      </div>
      <ul className="text-ink-muted mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs">
        {labels.map((label) => (
          <li key={label} className="num">
            <span
              aria-hidden
              className="mr-1 inline-block size-1.5 rounded-full align-middle"
              style={{ background: FILLS[labelTone(label)] }}
            />
            {humanize(label)} {share(classification.probabilities[label])}
          </li>
        ))}
      </ul>
    </div>
  );
}

function correlationColor(r: number): string {
  const strength = Math.round(Math.min(1, Math.abs(r)) * 85);
  const base = r >= 0 ? "var(--color-accent)" : "var(--color-negative)";
  return `color-mix(in oklab, ${base} ${strength}%, var(--color-panel))`;
}

export function CorrelationHeatmap({ symbols, cells }: { symbols: string[]; cells: CorrelationCell[] }) {
  const lookup = new Map<string, CorrelationCell>();
  for (const cell of cells) {
    lookup.set(`${cell.a}|${cell.b}`, cell);
    lookup.set(`${cell.b}|${cell.a}`, cell);
  }
  return (
    <div className="overflow-x-auto">
      <table className="text-2xs border-separate border-spacing-px" aria-label="Correlation matrix">
        <thead>
          <tr>
            <th className="sr-only">Instrument</th>
            {symbols.map((s) => (
              <th key={s} scope="col" className="text-ink-muted h-12 w-7 align-bottom font-medium">
                <span className="inline-block -rotate-60 whitespace-nowrap">{s}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {symbols.map((row) => (
            <tr key={row}>
              <th scope="row" className="text-ink-muted pr-1 text-right font-medium whitespace-nowrap">
                {row}
              </th>
              {symbols.map((col) => {
                if (row === col)
                  return (
                    <td key={col} className="bg-panel-raised text-ink-faint h-7 w-7 text-center" aria-label={`${row} itself`}>
                      ·
                    </td>
                  );
                const cell = lookup.get(`${row}|${col}`);
                const r = toNumber(cell?.correlation);
                if (!cell || r === null)
                  return (
                    <td
                      key={col}
                      className="text-ink-faint h-7 w-7 text-center"
                      title={`${row}/${col}: too few shared observations (${cell?.observations ?? 0})`}
                    >
                      n/a
                    </td>
                  );
                return (
                  <td
                    key={col}
                    className="num h-7 w-7 text-center text-ink"
                    style={{ background: correlationColor(r) }}
                    title={`${row}/${col}: r = ${r.toFixed(2)} [${toNumber(cell.ci_low)?.toFixed(2) ?? MISSING}, ${toNumber(cell.ci_high)?.toFixed(2) ?? MISSING}], n = ${cell.observations}`}
                  >
                    {r.toFixed(2).replace(/^(-?)0\./, "$1.")}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const ROWS: readonly { key: "trend" | "volatility" | "stability"; dimension: Dimension }[] = [
  { key: "trend", dimension: "TREND" },
  { key: "volatility", dimension: "VOLATILITY" },
  { key: "stability", dimension: "STABILITY" },
];

/** One coloured cell per decision time and dimension; reconstructed points are hatched. */
export function RegimeTimeline({ points }: { points: RegimePoint[] }) {
  return (
    <div className="space-y-1.5" aria-label="Regime timeline">
      {ROWS.map(({ key, dimension }) => (
        <div key={key} className="flex items-center gap-2">
          <span className="text-ink-muted w-20 shrink-0 text-2xs font-semibold uppercase">{humanize(dimension)}</span>
          <div className="flex min-w-0 flex-1 gap-px overflow-hidden">
            {points.map((point) => {
              const c = point[key];
              const day = point.as_of.slice(0, 10);
              return (
                <span
                  key={point.as_of}
                  className="h-4 min-w-[3px] flex-1 rounded-[1px]"
                  title={`${day} · ${c ? humanize(c.label) : "not computed"} · ${humanize(point.basis)}`}
                  style={{
                    background: c ? FILLS[labelTone(c.label)] : "transparent",
                    opacity: point.basis === "RECONSTRUCTED" ? 0.55 : 1,
                    backgroundImage:
                      point.basis === "RECONSTRUCTED"
                        ? "repeating-linear-gradient(45deg, transparent 0 2px, rgba(0,0,0,0.35) 2px 3px)"
                        : undefined,
                  }}
                />
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

export interface RegimeChange {
  as_of: string;
  dimension: Dimension;
  from: string;
  to: string;
  basis: string;
}

/** Label changes between consecutive points, newest first. */
export function regimeChanges(points: RegimePoint[]): RegimeChange[] {
  const changes: RegimeChange[] = [];
  for (const { key, dimension } of ROWS) {
    let previous: string | null = null;
    for (const point of points) {
      const label = point[key]?.label ?? null;
      if (label !== null && previous !== null && label !== previous) {
        changes.push({ as_of: point.as_of, dimension, from: previous, to: label, basis: point.basis });
      }
      if (label !== null) previous = label;
    }
  }
  return changes.sort((a, b) => b.as_of.localeCompare(a.as_of));
}
