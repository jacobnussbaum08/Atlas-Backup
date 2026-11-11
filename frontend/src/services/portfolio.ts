import type {
  AccountView,
  EntryDetail,
  EntrySummary,
  Holding,
  PnlBreakdown,
  PortfolioOverview,
  PortfolioSnapshot,
  ReconciliationRun,
  RunSummary,
  TrialBalance,
} from "../types/portfolio";
import { buildUrl, getJson } from "./api";

/** Typed clients for the portfolio and accounting endpoints (all read-only). */
type Opts = { signal?: AbortSignal };
const account = (accountId: string | null) => (accountId ? { account_id: accountId } : {});

export const portfolio = {
  accounts: (opts: Opts = {}) => getJson<AccountView[]>("/api/v1/portfolio/accounts", opts),
  overview: (accountId: string | null, opts: Opts = {}) =>
    getJson<PortfolioOverview>(buildUrl("/api/v1/portfolio/overview", account(accountId)), opts),
  holdings: (accountId: string | null, opts: Opts = {}) =>
    getJson<Holding[]>(buildUrl("/api/v1/portfolio/holdings", account(accountId)), opts),
  /** Recorded valuation checkpoints, newest first (at most 200). */
  snapshots: (accountId: string | null, opts: Opts = {}) =>
    getJson<PortfolioSnapshot[]>(buildUrl("/api/v1/portfolio/snapshots", { ...account(accountId), limit: 200 }), opts),
  pnl: (accountId: string | null, opts: Opts = {}) =>
    getJson<PnlBreakdown>(buildUrl("/api/v1/portfolio/pnl", account(accountId)), opts),
  journal: (accountId: string | null, opts: Opts = {}) =>
    getJson<EntrySummary[]>(buildUrl("/api/v1/accounting/journal", { ...account(accountId), limit: 50 }), opts),
  entry: (entryId: string, opts: Opts = {}) =>
    getJson<EntryDetail>(`/api/v1/accounting/journal/${encodeURIComponent(entryId)}`, opts),
  balances: (accountId: string | null, opts: Opts = {}) =>
    getJson<TrialBalance>(buildUrl("/api/v1/accounting/balances", account(accountId)), opts),
  reconciliationRuns: (accountId: string | null, opts: Opts = {}) =>
    getJson<RunSummary[]>(buildUrl("/api/v1/accounting/reconciliation", account(accountId)), opts),
  reconciliationRun: (runId: string, opts: Opts = {}) =>
    getJson<ReconciliationRun>(`/api/v1/accounting/reconciliation/${encodeURIComponent(runId)}`, opts),
};
