// Test fixtures shaped like real API responses. Used only by tests.
import type { HealthResponse, ReadinessResponse } from "../types/health";

export const HEALTH_FIXTURE: HealthResponse = {
  status: "ok",
  service: "atlas-ai-trader",
  version: "0.1.0",
  environment: "development",
  trading_mode: "PAPER",
  time: "2026-01-02T14:30:00Z",
  uptime_seconds: 42.5,
};

export const READY_FIXTURE: ReadinessResponse = {
  status: "ready",
  trading_mode: "PAPER",
  checked_at: "2026-01-02T14:30:00Z",
  components: [
    { name: "database", status: "ok", latency_ms: 1.2, detail: null },
    { name: "migrations", status: "ok", latency_ms: 0.8, detail: "at head ['0001']" },
    { name: "redis", status: "ok", latency_ms: 0.5, detail: null },
  ],
};

export const NOT_READY_FIXTURE: ReadinessResponse = {
  ...READY_FIXTURE,
  status: "not_ready",
  components: [
    { name: "database", status: "ok", latency_ms: 1.2, detail: null },
    { name: "migrations", status: "ok", latency_ms: 0.8, detail: "at head ['0001']" },
    { name: "redis", status: "error", latency_ms: 2.0, detail: "ConnectionError: refused" },
  ],
};

type Route = { status: number; body: unknown } | "network-error";

/** Install a fetch stub answering by path. */
export function stubFetch(routes: Record<string, Route>): void {
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const path = typeof input === "string" ? input : input.toString();
    const route = routes[path];
    if (route === undefined) throw new Error(`Unexpected fetch: ${path}`);
    if (route === "network-error") throw new TypeError("Failed to fetch");
    return new Response(JSON.stringify(route.body), {
      status: route.status,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
}
