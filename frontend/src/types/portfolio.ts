/**
 * Shapes of the read-only /api/v1/portfolio and /api/v1/accounting responses (Phase 6A).
 * Money, quantities and rates arrive as decimal strings and are only formatted for display.
 */

export type Decimal = string;

export interface AccountView {
  id: string;
  name: string;
  purpose: "PRIMARY" | "CONTROLLED_TEST";
  base_currency: string;
  status: string;
  created_at: string;
  entries: number;
  opening_funding: Decimal | null;
  opening_funding_posted_at: string | null;
}

export interface PortfolioFigures {
  functional_currency: string;
  contributed_capital: Decimal;
  cash: Decimal;
  restricted_cash: Decimal;
  long_value: Decimal;
  short_liability: Decimal;
  accrued_costs: Decimal;
  fx_translation: Decimal;
  total_assets: Decimal;
  total_liabilities: Decimal;
  equity: Decimal;
  equity_by_components: Decimal;
  realized_pnl: Decimal;
  unrealized_pnl: Decimal;
  realized_fx: Decimal;
  unrealized_fx: Decimal;
  commissions: Decimal;
  fees: Decimal;
  borrow_costs: Decimal;
  funding_costs: Decimal;
  total_costs: Decimal;
  fx_clearing: Decimal;
  investment_return: Decimal;
  available_capital: Decimal;
  long_exposure: Decimal;
  short_exposure: Decimal;
  gross_exposure: Decimal;
  net_exposure: Decimal;
}

export interface CashRow {
  currency: string;
  cash: Decimal;
  cash_functional: Decimal;
  restricted: Decimal;
  restricted_functional: Decimal;
  accrued_payables: Decimal;
  translation_functional: Decimal;
}

export interface ValuationIssue {
  kind: string;
  symbol?: string;
  currency?: string;
  asset_id?: string;
  detail?: string | null;
  observed_at?: string | null;
}

export interface SnapshotSummary {
  id: string;
  as_of: string;
  valuation_status: "COMPLETE" | "STALE" | "INCOMPLETE";
  entry_sequence: number;
  entries_since: number;
  equity: Decimal;
  issues: ValuationIssue[];
}

export interface RunSummary {
  id: string;
  status: "PASSED" | "FAILED";
  checks_total: number;
  checks_failed: number;
  entry_sequence: number;
  current: boolean;
  trigger: string;
  finished_at: string;
}

export interface CheckResult {
  check_code: string;
  scope: string;
  currency: string | null;
  expected: Decimal | null;
  actual: Decimal | null;
  difference: Decimal | null;
  status: "PASS" | "FAIL";
  severity: "INFO" | "HIGH";
  message: string;
}

/** One recorded valuation checkpoint (GET /portfolio/snapshots). */
export interface PortfolioSnapshot {
  id: string;
  as_of: string;
  valuation_status: "COMPLETE" | "STALE" | "INCOMPLETE";
  entry_sequence: number;
  valuation_entry_id: string | null;
  equity: Decimal;
  contributed_capital: Decimal;
  investment_return: Decimal;
  cash: Decimal;
  long_value: Decimal;
  short_liability: Decimal;
  realized_pnl: Decimal;
  unrealized_pnl: Decimal;
  issues: ValuationIssue[];
}

export interface PortfolioOverview {
  account: AccountView;
  figures: PortfolioFigures;
  cash_by_currency: CashRow[];
  positions: { long: number; short: number };
  latest_snapshot: SnapshotSummary | null;
  reconciliation: RunSummary | null;
  accounting_errors: CheckResult[];
  disclaimer: string;
}

export interface Holding {
  asset_id: string;
  symbol: string;
  asset_class: string;
  currency: string;
  side: "LONG" | "SHORT";
  quantity: Decimal;
  cost_basis: Decimal;
  average_price: Decimal;
  valuation_adjustment: Decimal;
  carried_value: Decimal;
  carried_value_functional: Decimal;
  realized_pnl: Decimal;
  last_price: Decimal | null;
  last_price_status: "FRESH" | "STALE" | "MISSING" | null;
  last_price_source: string | null;
  last_price_observed_at: string | null;
  last_price_provenance: Record<string, unknown> | null;
  valued_at: string | null;
}

export interface PnlBreakdown {
  functional_currency: string;
  totals: Record<string, Decimal>;
  by_asset: (Record<string, string> & { asset_id: string; symbol: string; currency: string })[];
  unattributed: Record<string, Decimal>;
  fx_by_currency: Record<string, Record<string, Decimal>>;
  note: string;
}

export interface EntrySummary {
  id: string;
  sequence: number;
  event_id: string;
  entry_type: string;
  source: string;
  source_ref: string | null;
  template: string;
  effective_at: string;
  posted_at: string;
  description: string;
  functional_currency: string;
  functional_total: Decimal;
  line_count: number;
  reverses_entry_id: string | null;
  reversed_by_entry_id: string | null;
}

export interface EntryLine {
  line_number: number;
  account_code: string;
  account_name: string;
  side: "DEBIT" | "CREDIT";
  currency: string;
  debit: Decimal;
  credit: Decimal;
  functional_debit: Decimal;
  functional_credit: Decimal;
  rate_basis: string;
  fx_rate: Decimal | null;
  fx_rate_id: number | null;
  asset_id: string | null;
  symbol: string | null;
  memo: string | null;
}

export interface EntryDetail extends EntrySummary {
  payload: Record<string, unknown>;
  details: Record<string, unknown>;
  lines: EntryLine[];
  movements: {
    asset_id: string;
    symbol: string;
    movement_type: string;
    currency: string;
    quantity_delta: Decimal;
    quantity_after: Decimal;
    cost_delta: Decimal;
    cost_after: Decimal;
    price: Decimal | null;
    realized_pnl: Decimal;
    realized_pnl_functional: Decimal;
  }[];
}

export interface BalanceRow {
  account_code: string;
  account_name: string;
  account_type: string;
  normal_side: string;
  currency: string;
  debit_total: Decimal;
  credit_total: Decimal;
  balance: Decimal;
  functional_balance: Decimal;
}

export interface TrialBalance {
  rows: BalanceRow[];
  by_currency: Record<string, { debits: Decimal; credits: Decimal }>;
  functional: { debits: Decimal; credits: Decimal };
  balanced: boolean;
}

export interface ReconciliationRun {
  run: RunSummary;
  results: CheckResult[];
}
