import type {
  ControlEvent,
  ControlResult,
  EmergencyResult,
  IntentStatus,
  OpsAlert,
  OrchestrationJob,
  OrchestrationStatus,
  Protection,
  TradeIntent,
} from "../types/orchestration";
import { buildUrl, getJson, postJson } from "./api";

/**
 * Typed clients for the Phase 10A/10B orchestration endpoints. Reads (status, intents, jobs,
 * the protection gate, alerts), plus the fail-safe controls only (operator credential):
 * pause, disable and emergency stop. Enabling, resuming and clearing an emergency are
 * operator CLI actions; the dashboard cannot start trading.
 */
type Opts = { signal?: AbortSignal };
const token = (t?: string) => (t ? { operatorToken: t } : {});

export const orchestration = {
  status: (opts: Opts = {}) => getJson<OrchestrationStatus>("/api/v1/orchestration/status", opts),
  history: (opts: Opts = {}) =>
    getJson<ControlEvent[]>(buildUrl("/api/v1/orchestration/controls/history", { limit: 20 }), opts),
  intents: (status: IntentStatus | null, opts: Opts = {}) =>
    getJson<TradeIntent[]>(buildUrl("/api/v1/orchestration/intents", { status, limit: 50 }), opts),
  jobs: (opts: Opts = {}) =>
    getJson<OrchestrationJob[]>(buildUrl("/api/v1/orchestration/jobs", { limit: 30 }), opts),
  protection: (opts: Opts = {}) => getJson<Protection>("/api/v1/orchestration/protection", opts),
  alerts: (opts: Opts = {}) =>
    getJson<OpsAlert[]>(
      buildUrl("/api/v1/orchestration/alerts", { open_only: true, min_severity: "HIGH", limit: 20 }),
      opts,
    ),
  pause: (reason: string, operatorToken?: string) =>
    postJson<ControlResult>("/api/v1/orchestration/controls/pause", { reason }, token(operatorToken)),
  disable: (reason: string, operatorToken?: string) =>
    postJson<ControlResult>("/api/v1/orchestration/controls/disable", { reason }, token(operatorToken)),
  emergencyStop: (reason: string, operatorToken?: string) =>
    postJson<EmergencyResult>("/api/v1/orchestration/controls/emergency-stop", { reason }, token(operatorToken)),
};
