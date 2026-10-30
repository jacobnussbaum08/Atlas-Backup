import { HeartPulse, RefreshCw } from "lucide-react";

import { StatusPill } from "../components/StatusPill";
import { useDashboardStatus } from "../layouts/DashboardLayout";

const COMPONENT_LABELS: Record<string, string> = {
  database: "PostgreSQL",
  migrations: "Database migrations",
  redis: "Redis",
};

function formatTimestamp(iso: string): string {
  return new Date(iso).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
}

export function SystemHealthPage() {
  const { health, readiness, error, loading, refresh } = useDashboardStatus();

  return (
    <section aria-labelledby="page-title" className="max-w-4xl">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <HeartPulse aria-hidden className="text-accent size-6" />
          <h1 id="page-title" className="text-xl font-semibold">
            System Health
          </h1>
        </div>
        <button
          type="button"
          onClick={refresh}
          className="border-border bg-panel hover:bg-panel-raised inline-flex items-center gap-2 rounded border px-3 py-1.5 text-sm"
        >
          <RefreshCw aria-hidden className="size-4" />
          Refresh
        </button>
      </div>

      {error && (
        <div
          role="alert"
          className="border-negative/40 bg-negative/10 text-negative mt-6 rounded-lg border p-4 text-sm"
        >
          {error}. Check that the backend is running.
        </div>
      )}

      {loading && !readiness && !error && <p className="text-ink-muted mt-6 text-sm">Checking…</p>}

      {health && (
        <dl className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ["API", health.status === "ok" ? "Live" : health.status],
            ["Trading mode", health.trading_mode],
            ["Environment", health.environment],
            ["Uptime", `${Math.floor(health.uptime_seconds)} s`],
          ].map(([label, value]) => (
            <div key={label} className="border-border bg-panel rounded-lg border p-3">
              <dt className="text-ink-muted text-xs">{label}</dt>
              <dd className="mt-1 font-mono text-sm">{value}</dd>
            </div>
          ))}
        </dl>
      )}

      {readiness && (
        <div className="border-border bg-panel mt-6 overflow-x-auto rounded-lg border">
          <table className="w-full text-left text-sm">
            <caption className="text-ink-muted px-4 pt-3 text-left text-xs">
              Readiness checked {formatTimestamp(readiness.checked_at)}
            </caption>
            <thead className="text-ink-muted text-xs">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">Component</th>
                <th scope="col" className="px-4 py-2 font-medium">Status</th>
                <th scope="col" className="px-4 py-2 text-right font-medium">Latency</th>
                <th scope="col" className="px-4 py-2 font-medium">Detail</th>
              </tr>
            </thead>
            <tbody>
              {readiness.components.map((component) => (
                <tr key={component.name} className="border-border border-t">
                  <td className="px-4 py-2.5">{COMPONENT_LABELS[component.name] ?? component.name}</td>
                  <td className="px-4 py-2.5">
                    <StatusPill
                      tone={component.status === "ok" ? "positive" : "negative"}
                      label={component.status === "ok" ? "OK" : "Error"}
                    />
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono tabular-nums">
                    {component.latency_ms === null ? "—" : `${component.latency_ms.toFixed(1)} ms`}
                  </td>
                  <td className="text-ink-muted px-4 py-2.5 font-mono text-xs">{component.detail ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
