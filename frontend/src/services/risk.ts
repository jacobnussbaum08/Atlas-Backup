import type {
  AssessmentDetail,
  AssessmentResult,
  AssessmentSummary,
  BreakerEvent,
  ConfigVersion,
  KillSwitchResult,
  Limits,
  Reservation,
  ReservationEvent,
  RiskOverview,
} from "../types/risk";
import { buildUrl, getJson, postJson } from "./api";

/**
 * Typed clients for the Phase 6B/6C risk endpoints. All reads, plus the one fail-safe
 * control: engaging the kill switch (operator token). Releasing it, resetting breakers,
 * changing limits and every reservation command are CLI or service actions; the
 * dashboard cannot loosen risk, reserve, release or execute anything.
 */
type Opts = { signal?: AbortSignal };

export const risk = {
  state: (opts: Opts = {}) => getJson<RiskOverview>("/api/v1/risk/state", opts),
  limits: (opts: Opts = {}) => getJson<Limits>("/api/v1/risk/limits", opts),
  assessments: (result: AssessmentResult | null, opts: Opts = {}) =>
    getJson<AssessmentSummary[]>(buildUrl("/api/v1/risk/assessments", { result, limit: 50 }), opts),
  assessment: (id: string, opts: Opts = {}) =>
    getJson<AssessmentDetail>(`/api/v1/risk/assessments/${encodeURIComponent(id)}`, opts),
  breakerEvents: (opts: Opts = {}) =>
    getJson<BreakerEvent[]>(buildUrl("/api/v1/risk/circuit-breakers/events", { limit: 50 }), opts),
  configurations: (opts: Opts = {}) =>
    getJson<ConfigVersion[]>(buildUrl("/api/v1/risk/configurations", { limit: 20 }), opts),
  reservations: (activeOnly: boolean, opts: Opts = {}) =>
    getJson<Reservation[]>(
      buildUrl("/api/v1/risk/reservations", { active: activeOnly ? "true" : null, limit: 50 }),
      opts,
    ),
  reservationEvents: (eventType: string | null, opts: Opts = {}) =>
    getJson<ReservationEvent[]>(
      buildUrl("/api/v1/risk/reservations/events", { event_type: eventType, limit: 50 }),
      opts,
    ),
  engageKillSwitch: (reason: string, operatorToken?: string) =>
    postJson<KillSwitchResult>("/api/v1/risk/kill-switch/engage", { reason }, operatorToken ? { operatorToken } : {}),
};
