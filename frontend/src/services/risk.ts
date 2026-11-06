import type {
  AssessmentDetail,
  AssessmentResult,
  AssessmentSummary,
  BreakerEvent,
  ConfigVersion,
  KillSwitchResult,
  RiskOverview,
} from "../types/risk";
import { buildUrl, getJson, postJson } from "./api";

/**
 * Typed clients for the Phase 6B risk endpoints. All reads, plus the one fail-safe control:
 * engaging the kill switch (operator token). Releasing it, resetting breakers and changing
 * limits are operator CLI actions; the dashboard cannot loosen risk.
 */
type Opts = { signal?: AbortSignal };

export const risk = {
  state: (opts: Opts = {}) => getJson<RiskOverview>("/api/v1/risk/state", opts),
  assessments: (result: AssessmentResult | null, opts: Opts = {}) =>
    getJson<AssessmentSummary[]>(buildUrl("/api/v1/risk/assessments", { result, limit: 50 }), opts),
  assessment: (id: string, opts: Opts = {}) =>
    getJson<AssessmentDetail>(`/api/v1/risk/assessments/${encodeURIComponent(id)}`, opts),
  breakerEvents: (opts: Opts = {}) =>
    getJson<BreakerEvent[]>(buildUrl("/api/v1/risk/circuit-breakers/events", { limit: 50 }), opts),
  configurations: (opts: Opts = {}) =>
    getJson<ConfigVersion[]>(buildUrl("/api/v1/risk/configurations", { limit: 20 }), opts),
  engageKillSwitch: (reason: string, operatorToken?: string) =>
    postJson<KillSwitchResult>("/api/v1/risk/kill-switch/engage", { reason }, operatorToken ? { operatorToken } : {}),
};
