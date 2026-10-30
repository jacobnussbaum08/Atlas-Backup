// Mirrors backend/app/api/health.py and backend/app/monitoring/health.py.

export type TradingMode = "PAPER";

export interface HealthResponse {
  status: "ok";
  service: string;
  version: string;
  environment: string;
  trading_mode: TradingMode;
  time: string;
  uptime_seconds: number;
}

export interface ComponentHealth {
  name: string;
  status: "ok" | "error";
  latency_ms: number | null;
  detail: string | null;
}

export interface ReadinessResponse {
  status: "ready" | "not_ready";
  trading_mode: TradingMode;
  checked_at: string;
  components: ComponentHealth[];
}
