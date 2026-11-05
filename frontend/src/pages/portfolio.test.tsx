import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type {
  AccountView,
  EntryDetail,
  EntrySummary,
  Holding,
  PnlBreakdown,
  PortfolioFigures,
  PortfolioOverview,
  ReconciliationRun,
  TrialBalance,
} from "../types/portfolio";

// TEST FIXTURES: payloads shaped like /api/v1/portfolio and /api/v1/accounting responses.
const ACCOUNT: AccountView = {
  id: "acct-1",
  name: "Atlas paper account",
  purpose: "PRIMARY",
  base_currency: "USD",
  status: "ACTIVE",
  created_at: "2026-11-04T21:00:00Z",
  entries: 1,
  opening_funding: "100000.00",
  opening_funding_posted_at: "2026-11-04T21:00:00Z",
};

const ZERO = "0.00000000";
const FIGURES: PortfolioFigures = {
  functional_currency: "USD",
  contributed_capital: "100000.00000000",
  cash: "100000.00000000",
  restricted_cash: ZERO,
  long_value: ZERO,
  short_liability: ZERO,
  accrued_costs: ZERO,
  fx_translation: ZERO,
  total_assets: "100000.00000000",
  total_liabilities: ZERO,
  equity: "100000.00000000",
  equity_by_components: "100000.00000000",
  realized_pnl: ZERO,
  unrealized_pnl: ZERO,
  realized_fx: ZERO,
  unrealized_fx: ZERO,
  commissions: ZERO,
  fees: ZERO,
  borrow_costs: ZERO,
  funding_costs: ZERO,
  total_costs: ZERO,
  fx_clearing: ZERO,
  investment_return: ZERO,
  available_capital: "100000.00000000",
  long_exposure: ZERO,
  short_exposure: ZERO,
  gross_exposure: ZERO,
  net_exposure: ZERO,
};

const OVERVIEW: PortfolioOverview = {
  account: ACCOUNT,
  figures: FIGURES,
  cash_by_currency: [
    {
      currency: "USD",
      cash: "100000.00000000",
      cash_functional: "100000.00000000",
      restricted: "0",
      restricted_functional: "0",
      accrued_payables: "0",
      translation_functional: "0",
    },
  ],
  positions: { long: 0, short: 0 },
  latest_snapshot: null,
  reconciliation: {
    id: "run-1",
    status: "PASSED",
    checks_total: 18,
    checks_failed: 0,
    entry_sequence: 1,
    current: true,
    trigger: "INIT",
    finished_at: "2026-11-04T21:00:01Z",
  },
  accounting_errors: [],
  disclaimer:
    "Simulated paper account. Opening funding is simulated contributed capital, not a return; no order has been executed and no broker is connected.",
};

const ENTRY: EntrySummary = {
  id: "entry-1",
  sequence: 1,
  event_id: "opening-funding",
  entry_type: "OPENING_FUNDING",
  source: "SYSTEM",
  source_ref: "simulated opening funding",
  template: "opening_funding.v1",
  effective_at: "2026-11-04T21:00:00Z",
  posted_at: "2026-11-04T21:00:00Z",
  description: "Simulated opening funding",
  functional_currency: "USD",
  functional_total: "100000.00000000",
  line_count: 2,
  reverses_entry_id: null,
  reversed_by_entry_id: null,
};

const DETAIL: EntryDetail = {
  ...ENTRY,
  payload: { amount: "100000.00", currency: "USD" },
  details: {},
  movements: [],
  lines: [
    { line_number: 1, account_code: "1000", account_name: "Cash", side: "DEBIT", currency: "USD", debit: "100000.00000000", credit: "0", functional_debit: "100000.00000000", functional_credit: "0", rate_basis: "FUNCTIONAL", fx_rate: "1", fx_rate_id: null, asset_id: null, symbol: null, memo: null },
    { line_number: 2, account_code: "3000", account_name: "Contributed capital: simulated opening funding", side: "CREDIT", currency: "USD", debit: "0", credit: "100000.00000000", functional_debit: "0", functional_credit: "100000.00000000", rate_basis: "FUNCTIONAL", fx_rate: "1", fx_rate_id: null, asset_id: null, symbol: null, memo: null },
  ],
};

const BALANCES: TrialBalance = {
  rows: [
    { account_code: "1000", account_name: "Cash", account_type: "ASSET", normal_side: "DEBIT", currency: "USD", debit_total: "100000.00000000", credit_total: "0", balance: "100000.00000000", functional_balance: "100000.00000000" },
    { account_code: "3000", account_name: "Contributed capital: simulated opening funding", account_type: "EQUITY", normal_side: "CREDIT", currency: "USD", debit_total: "0", credit_total: "100000.00000000", balance: "100000.00000000", functional_balance: "100000.00000000" },
  ],
  by_currency: { USD: { debits: "100000.00000000", credits: "100000.00000000" } },
  functional: { debits: "100000.00000000", credits: "100000.00000000" },
  balanced: true,
};

const PNL: PnlBreakdown = {
  functional_currency: "USD",
  totals: { realized_pnl: ZERO, unrealized_pnl: ZERO, realized_fx: ZERO, unrealized_fx: ZERO, commissions: ZERO, fees: ZERO, borrow_costs: ZERO, funding_costs: ZERO },
  by_asset: [],
  unattributed: {},
  fx_by_currency: {},
  note: "Realized P/L is proceeds minus average cost on closed quantity.",
};

const RUN: ReconciliationRun = {
  run: OVERVIEW.reconciliation!,
  results: [
    { check_code: "ENTRY_BALANCE", scope: "all entries", currency: null, expected: null, actual: null, difference: null, status: "PASS", severity: "INFO", message: "1 entries balance" },
    { check_code: "TRIAL_BALANCE", scope: "currency USD", currency: "USD", expected: "0", actual: "0", difference: "0", status: "PASS", severity: "INFO", message: "debits minus credits: 0" },
  ],
};

const HOLDING: Holding = {
  asset_id: "asset-1",
  symbol: "FXA",
  asset_class: "EQUITY",
  currency: "USD",
  side: "SHORT",
  quantity: "-50.000000000000000000",
  cost_basis: "1000.00000000",
  average_price: "20.0000000000",
  valuation_adjustment: "85.00000000",
  carried_value: "1085.00000000",
  carried_value_functional: "1085.00000000",
  realized_pnl: "0",
  last_price: "21.7000000000",
  last_price_status: "FRESH",
  last_price_source: "BAR_CLOSE",
  last_price_observed_at: "2026-11-03T05:00:00Z",
  last_price_provenance: { table: "market_bars", id: 7 },
  valued_at: "2026-11-04T01:30:00Z",
};

function routes(overrides: Record<string, unknown> = {}) {
  return {
    ...LAYOUT_ROUTES,
    "/api/v1/portfolio/accounts": { body: [ACCOUNT] },
    "/api/v1/portfolio/overview": { body: OVERVIEW },
    "/api/v1/portfolio/holdings": { body: [] },
    "/api/v1/portfolio/pnl": { body: PNL },
    "/api/v1/accounting/journal": { body: [ENTRY] },
    "/api/v1/accounting/journal/entry-1": { body: DETAIL },
    "/api/v1/accounting/balances": { body: BALANCES },
    "/api/v1/accounting/reconciliation/run-1": { body: RUN },
    ...overrides,
  } as Parameters<typeof stubApi>[0];
}

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider>
      <MemoryRouter initialEntries={[path]}>
        <App pollIntervalMs={0} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

beforeEach(() => {
  installFakeLive();
});

describe("Portfolio & Accounting page", () => {
  it("shows the $100,000 simulated funding apart from returns, with balanced books", async () => {
    stubApi(routes());
    renderAt("/portfolio");
    const funding = await screen.findByText("Opening simulated funding");
    expect(within(funding.parentElement!).getByText("100,000.00 USD")).toBeInTheDocument();
    expect(within(funding.parentElement!).getByText("Contributed capital, not a return")).toBeInTheDocument();
    const ret = screen.getByText("Investment return").parentElement!;
    expect(within(ret).getByText("0.00 USD")).toBeInTheDocument();
    expect(within(ret).getByText(/excludes funding/)).toBeInTheDocument();
    expect(screen.getByText(/not a return; no order has been executed/)).toBeInTheDocument();
    expect(screen.getByText("Balanced")).toBeInTheDocument();
    expect(await screen.findByText("Debits = credits")).toBeInTheDocument();
    expect(screen.getByText("No open positions")).toBeInTheDocument();
    expect(await screen.findByText("Passed")).toBeInTheDocument();
    expect(screen.getByText("No valuation checkpoint yet")).toBeInTheDocument();
    // Read-only: no trading controls anywhere on the page.
    expect(screen.queryByRole("button", { name: /buy|sell|order|submit|fund/i })).toBeNull();
  });

  it("expands a journal entry into its debit and credit lines", async () => {
    stubApi(routes());
    renderAt("/portfolio");
    fireEvent.click(await screen.findByText("Simulated opening funding"));
    const lines = await screen.findByRole("table", { name: "Lines of entry 1" });
    expect(within(lines).getByText("Cash")).toBeInTheDocument();
    expect(within(lines).getByText("Contributed capital: simulated opening funding")).toBeInTheDocument();
    expect(within(lines).getAllByText("100,000.00")).toHaveLength(4);
  });

  it("explains how to create the account when none exists", async () => {
    stubApi(
      routes({
        "/api/v1/portfolio/accounts": { body: [] },
        "/api/v1/portfolio/overview": {
          status: 404,
          body: { error: { code: "not_found", message: "no primary paper account exists yet: run `python -m app.accounting.cli init`" } },
        },
      }),
    );
    renderAt("/portfolio");
    expect(await screen.findByText("The paper account has not been created yet")).toBeInTheDocument();
    expect(screen.getByRole("main").textContent).not.toMatch(/100,000/);
  });

  it("raises reconciliation failures and stale valuations prominently", async () => {
    stubApi(
      routes({
        "/api/v1/portfolio/overview": {
          body: {
            ...OVERVIEW,
            positions: { long: 0, short: 1 },
            latest_snapshot: {
              id: "snap-1",
              as_of: "2026-11-04T01:30:00Z",
              valuation_status: "STALE",
              entry_sequence: 5,
              entries_since: 0,
              equity: "99907.45",
              issues: [{ kind: "PRICE_STALE", symbol: "FXB", detail: "not from the latest session (2026-11-03)" }],
            },
            reconciliation: { ...OVERVIEW.reconciliation!, status: "FAILED", checks_failed: 1 },
            accounting_errors: [
              { check_code: "CURRENCY_BALANCES", scope: "1000 USD", currency: "USD", expected: "99999.00", actual: "100000.00", difference: "1.00", status: "FAIL", severity: "HIGH", message: "balance: expected 99999.00, found 100000.00" },
            ],
          },
        },
        "/api/v1/portfolio/holdings": { body: [HOLDING] },
      }),
    );
    renderAt("/portfolio");
    expect(await screen.findByText(/1 high-priority accounting error/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing has been corrected automatically/)).toBeInTheDocument();
    expect(screen.getByText(/is stale: positions without a fresh price/)).toBeInTheDocument();
    expect(screen.getByText(/Price stale: FXB/)).toBeInTheDocument();
    const errors = screen.getByRole("list", { name: "Accounting errors" });
    expect(within(errors).getByText(/CURRENCY_BALANCES/)).toBeInTheDocument();
    const holdings = await screen.findByRole("table", { name: "Holdings" });
    expect(within(holdings).getByText("SHORT")).toBeInTheDocument();
    expect(within(holdings).getByText("−50")).toBeInTheDocument();
    // A short whose buy-back value rose by 85.00 shows an unrealized loss.
    expect(within(holdings).getByText("−85.00")).toBeInTheDocument();
  });

  it("is in the navigation as a built section", async () => {
    stubApi(routes());
    renderAt("/portfolio");
    const nav = screen.getByRole("navigation", { name: "Dashboard sections" });
    expect(within(nav).getByRole("link", { name: "Portfolio & Accounting" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Portfolio & Accounting" })).toBeInTheDocument();
  });
});
