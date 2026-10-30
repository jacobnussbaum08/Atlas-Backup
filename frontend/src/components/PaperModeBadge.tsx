import { FlaskConical } from "lucide-react";

/** Always-visible label: Atlas executes simulated paper trades only (spec §15.4, §24.1). */
export function PaperModeBadge() {
  return (
    <span
      role="status"
      aria-label="Paper trading mode: all execution is simulated"
      className="inline-flex items-center gap-1.5 rounded border border-caution/40 bg-caution/10 px-2 py-0.5 font-mono text-xs font-semibold tracking-wider text-caution uppercase"
    >
      <FlaskConical aria-hidden className="size-3.5" />
      Paper trading · Simulated
    </span>
  );
}
