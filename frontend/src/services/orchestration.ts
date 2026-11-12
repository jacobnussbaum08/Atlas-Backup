import type {
  ControlEvent,
  ControlResult,
  EmergencyResult,
  IntentStatus,
  OrchestrationJob,
  OrchestrationStatus,
  TradeIntent,
} from "../types/orchestration";
import { buildUrl, getJson, postJson } from "./api";

/**
 * Typed clients for the Phase 10A orchestration endpoints. Reads, plus the fail-safe
 * controls only (operator token): pause, disable and emergency stop. Enabling, resuming and
 * clearing an emergency are operator CLI actions; the dashboard cannot start trading.
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
  pause: (reason: string, operatorToken?: string) =>
    postJson<ControlResult>("/api/v1/orchestration/controls/pause", { reason }, token(operatorToken)),
  disable: (reason: string, operatorToken?: string) =>
    postJson<ControlResult>("/api/v1/orchestration/controls/disable", { reason }, token(operatorToken)),
  emergencyStop: (reason: string, operatorToken?: string) =>
    postJson<EmergencyResult>("/api/v1/orchestration/controls/emergency-stop", { reason }, token(operatorToken)),
};
