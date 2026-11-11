import { WifiOff } from "lucide-react";

import type { WorkerHealth } from "../types/system";
import { Badge, type Tone } from "./ui";

/**
 * Where a displayed value comes from. Every dashboard figure is one of these, and pages
 * label panels with it so live, stored, simulated and missing data are never confused.
 */
export type Origin = "LIVE" | "RECORDED" | "HISTORICAL" | "SIMULATED" | "REPLAY" | "UNAVAILABLE";

const ORIGIN: Record<Origin, { tone: Tone; label: string; title: string }> = {
  LIVE: {
    tone: "info",
    label: "Live",
    title: "Computed now by the backend from the newest stored data (refreshed while this page is open).",
  },
  RECORDED: {
    tone: "neutral",
    label: "Recorded",
    title: "Read from records Atlas stored at the time; shown as recorded, never recomputed.",
  },
  HISTORICAL: {
    tone: "neutral",
    label: "Historical",
    title: "Stored history (provider history or past recorded points).",
  },
  SIMULATED: {
    tone: "caution",
    label: "Simulated",
    title: "Paper trading: simulated orders, fills and money. No real brokerage or funds are involved.",
  },
  REPLAY: {
    tone: "accent",
    label: "Replay",
    title: "A backtest or validation replay of stored history, not the paper account.",
  },
  UNAVAILABLE: {
    tone: "negative",
    label: "Unavailable",
    title: "The backend could not provide this data; nothing is shown in its place.",
  },
};

export function OriginBadge({ origin, title }: { origin: Origin; title?: string }) {
  const o = ORIGIN[origin];
  return (
    <Badge tone={o.tone} title={title ?? o.title}>
      {o.label}
    </Badge>
  );
}

export const WORKER_TONES: Record<WorkerHealth, Tone> = {
  RUNNING: "positive",
  DEGRADED: "caution",
  STALE: "negative",
  STOPPED: "neutral",
  FAILED: "negative",
  DISABLED: "neutral",
  NOT_REPORTED: "neutral",
  NO_HEARTBEAT: "info",
};

export const WORKER_LABELS: Record<WorkerHealth, string> = {
  RUNNING: "Running",
  DEGRADED: "Degraded",
  STALE: "Stale heartbeat",
  STOPPED: "Stopped",
  FAILED: "Failed",
  DISABLED: "Disabled",
  NOT_REPORTED: "Never reported",
  NO_HEARTBEAT: "No heartbeat",
};

/**
 * Shown under the top bar when the API cannot be reached: anything still on screen is the
 * last data received, and is labelled as such.
 */
export function DisconnectedBanner({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="border-negative/50 bg-negative/10 text-negative flex flex-wrap items-center gap-2 border-b px-3 py-1.5 text-xs md:px-5"
    >
      <WifiOff aria-hidden className="size-3.5 shrink-0" />
      <span className="font-semibold">Disconnected from the Atlas API.</span>
      <span className="text-ink-muted">
        {message}. Values still on screen are the last ones received and may be stale; nothing is
        being updated.
      </span>
      <button type="button" onClick={onRetry} className="text-accent ml-auto font-medium hover:underline">
        Retry now
      </button>
    </div>
  );
}
