import { FlaskConical } from "lucide-react";

/**
 * Always-visible label: Atlas monitors market data and executes simulated paper trades
 * only (spec §15.4, §24.1). There is no real-money trading and no order execution.
 */
export function PaperModeBadge() {
  return (
    <span
      role="status"
      aria-label="Paper trading mode: market monitoring only, all execution is simulated"
      className="border-caution/50 bg-caution/10 text-caution inline-flex items-center gap-1.5 rounded-sm border px-2 py-0.5 font-mono text-2xs font-semibold tracking-wider uppercase"
    >
      <FlaskConical aria-hidden className="size-3" />
      Paper trading · Simulated · Monitoring only
    </span>
  );
}
