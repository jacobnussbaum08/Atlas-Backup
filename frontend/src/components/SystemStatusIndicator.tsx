import type { SystemStatus } from "../hooks/useSystemStatus";
import { StatusPill } from "./StatusPill";

export function SystemStatusIndicator({ status }: { status: SystemStatus }) {
  if (status.loading && !status.readiness && !status.error) {
    return <StatusPill tone="neutral" label="Checking…" />;
  }
  if (status.error || !status.readiness) {
    return <StatusPill tone="negative" label="API unreachable" />;
  }
  return status.readiness.status === "ready" ? (
    <StatusPill tone="positive" label="All systems ready" />
  ) : (
    <StatusPill tone="caution" label="Degraded" />
  );
}
