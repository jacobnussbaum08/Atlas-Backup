import type {
  ExecutionAnalytics,
  ExecutionModelVersion,
  ExecutionRequest,
  OrderEvent,
  OrderGroup,
  OrdersSummary,
  PaperBrokerStatus,
  PaperOrder,
  PaperOrderDetail,
  PaperPosition,
  PaperPositionDetail,
  PositionsSummary,
} from "../types/orders";
import { buildUrl, getJson } from "./api";

/**
 * Typed clients for the Phase 7A-7B paper order, position and execution endpoints. GET
 * only: the dashboard shows simulated paper orders and positions; it cannot submit, cancel,
 * fill, close or approve anything, and no brokerage is behind any of it.
 */
type Opts = { signal?: AbortSignal };
const account = (accountId: string | null) => (accountId ? { account_id: accountId } : {});

export const orders = {
  list: (accountId: string | null, group: OrderGroup | null, opts: Opts = {}) =>
    getJson<PaperOrder[]>(buildUrl("/api/v1/paper-orders", { ...account(accountId), group, limit: 100 }), opts),
  summary: (accountId: string | null, opts: Opts = {}) =>
    getJson<OrdersSummary>(buildUrl("/api/v1/paper-orders/summary", account(accountId)), opts),
  detail: (id: string, opts: Opts = {}) =>
    getJson<PaperOrderDetail>(`/api/v1/paper-orders/${encodeURIComponent(id)}`, opts),
  events: (accountId: string | null, opts: Opts = {}) =>
    getJson<OrderEvent[]>(buildUrl("/api/v1/paper-orders/events", { ...account(accountId), limit: 50 }), opts),
  refused: (accountId: string | null, opts: Opts = {}) =>
    getJson<ExecutionRequest[]>(
      buildUrl("/api/v1/paper-orders/requests", { ...account(accountId), decision: "REFUSED", limit: 50 }),
      opts,
    ),
  broker: (opts: Opts = {}) => getJson<PaperBrokerStatus>("/api/v1/paper-broker/status", opts),
};

export const positions = {
  list: (accountId: string | null, status: "open" | "closed", opts: Opts = {}) =>
    getJson<PaperPosition[]>(buildUrl("/api/v1/paper-positions", { ...account(accountId), status, limit: 200 }), opts),
  summary: (accountId: string | null, opts: Opts = {}) =>
    getJson<PositionsSummary>(buildUrl("/api/v1/paper-positions/summary", account(accountId)), opts),
  detail: (id: string, opts: Opts = {}) =>
    getJson<PaperPositionDetail>(`/api/v1/paper-positions/${encodeURIComponent(id)}`, opts),
};

export const execution = {
  analytics: (accountId: string | null, opts: Opts = {}) =>
    getJson<ExecutionAnalytics>(buildUrl("/api/v1/paper-execution/analytics", account(accountId)), opts),
  models: (opts: Opts = {}) => getJson<ExecutionModelVersion[]>("/api/v1/paper-execution/models", opts),
};
