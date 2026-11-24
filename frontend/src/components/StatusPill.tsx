export type StatusTone = "positive" | "negative" | "caution" | "neutral";

const TONE_CLASSES: Record<StatusTone, string> = {
  positive: "border-positive/40 bg-positive/10 text-positive",
  negative: "border-negative/40 bg-negative/10 text-negative",
  caution: "border-caution/40 bg-caution/10 text-caution",
  neutral: "border-border bg-panel-raised text-ink-muted",
};

const DOT_CLASSES: Record<StatusTone, string> = {
  positive: "bg-positive",
  negative: "bg-negative",
  caution: "bg-caution",
  neutral: "bg-ink-muted",
};

export function StatusPill({ tone, label }: { tone: StatusTone; label: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-sm border px-1.5 py-px text-2xs font-medium tracking-wide uppercase ${TONE_CLASSES[tone]}`}
    >
      <span aria-hidden className={`size-1.5 rounded-full ${DOT_CLASSES[tone]}`} />
      {label}
    </span>
  );
}
