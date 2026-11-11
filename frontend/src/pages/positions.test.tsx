import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type { AccountView } from "../types/portfolio";
import type {
  ExecutionAnalytics,
  PaperPosition,
  PaperPositionDetail,
  PositionsSummary,
  Stats,
} from "../types/orders";

// TEST FIXTURES: payloads shaped like /api/v1/paper-positions and /paper-execution
// responses. Simulated positions and fills only.
const ACCOUNT: AccountView = {
  id: "acct-1",
  name: "Atlas paper account",
  purpose: "PRIMARY",
  base_currency: "USD",
  status: "ACTIVE",
  created_at: "2026-11-01T00:00:00Z",
  entries: 9,
  opening_funding: "100000.00",
  opening_funding_posted_at: "2026-11-01T00:00:00Z",
};

function position(extra: Partial<PaperPosition> = {}): PaperPosition {
  return {
    id: "pos-1",
    simulated: true,
    account_id: "acct-1",
    asset_id: "asset-1",
    symbol: "PXA",
    asset_class: "EQUITY",
    currency: "USD",
    direction: "LONG",
    status: "OPEN",
    entry_order_id: "ord-1",
    reservation_id: "res-1",
    strategy_proposal_id: null,
    ai_decision_id: null,
    strategy_id: null,
    origin: "OPERATOR",
    quantity_opened: "10",
    quantity_closed: "0",
    open_quantity: "10",
    average_entry_price: "99.58",
    average_exit_price: null,
    current_price: "100.4",
    current_price_at: "2026-11-04T15:00:00Z",
    current_price_source: "QUOTE_MID",
    unrealized_pnl: "8.2",
    realized_pnl: "0",
    commissions: "1",
    fees: "0",
    borrow_accrued: "0",
    borrow_paid: "0",
    net_pnl: "-1",
    net_return: "-0.001",
    stop_price: "94.7",
    initial_stop_price: "94.7",
    trailing_stop_price: "99.4",
    trail_amount: "1",
    trail_percent: null,
    target_price: "101",
    invalidation_price: null,
    expires_at: null,
    trigger_reason: null,
    triggered_at: null,
    exit_reason: null,
    monitor_status: "ACTIVE",
    opened_at: "2026-11-04T14:31:00Z",
    closed_at: null,
    holding_seconds: 5400,
    updated_at: "2026-11-04T15:00:00Z",
    ...extra,
  };
}

const CLOSED = position({
  id: "pos-2",
  symbol: "PXB",
  status: "CLOSED",
  quantity_closed: "10",
  open_quantity: "0",
  average_exit_price: "101",
  realized_pnl: "14.2",
  commissions: "2",
  fees: "0.04",
  net_pnl: "12.16",
  net_return: "0.00122",
  exit_reason: "TAKE_PROFIT",
  strategy_id: "momentum_fixture",
  closed_at: "2026-11-04T16:00:00Z",
  holding_seconds: 9000,
});

const DETAIL: PaperPositionDetail = {
  notice: "Simulated paper position of the isolated Atlas paper broker.",
  position: CLOSED,
  events: [
    ["OPENED", "LONG 10 PXB opened at 99.58 (simulated fill)", "10"],
    ["PROTECTION_SET", "stop 94.7 (approved), target 101", "10"],
    ["REDUCED", "-10 at 101 (TAKE_PROFIT); realized 14.2 USD", "0"],
    ["CLOSED", "closed (TAKE_PROFIT): net 12.16 USD (simulated)", "0"],
  ].map(([event_type, reason, after], i) => ({
    sequence: i + 1,
    event_type: event_type as string,
    occurred_at: `2026-11-04T1${4 + i}:00:00Z`,
    actor: "system:execution-engine",
    reason: reason as string,
    quantity: "10",
    price: null,
    open_quantity_after: after as string,
    order_id: null,
    journal_entry_id: null,
    details: {},
  })),
  orders: [],
  fills: [
    {
      id: "fill-1",
      order_id: "ord-1",
      side: "BUY",
      quantity: "10",
      price: "99.58",
      commission: "1",
      fees: "0",
      source: "SIMULATION",
      journal_entry_id: "je-1",
      filled_at: "2026-11-04T14:31:00Z",
      detail: {
        basis: "QUOTE",
        execution_model_id: 1,
        reference_price: "99.525",
        touch_price: "99.55",
        arrival_price: "99.525",
        spread: "0.05",
        slippage_bps: "5.5",
        impact_bps: "1",
        shortfall_bps: "5.5",
        liquidity: "100",
        participation: "0.1",
        commission: "1",
        regulatory_fees: "0",
        exchange_fees: "0",
        gap: false,
        observations: {},
        assumptions: ["touch of a fresh quote"],
      },
    },
  ],
  close_requests: [],
};

const SUMMARY: PositionsSummary = {
  simulated: true,
  open: 1,
  closed: 1,
  long: 1,
  short: 0,
  unrealized_pnl: "8.2",
  realized_pnl: "14.2",
  net_pnl_closed: "12.16",
  winners: 1,
  losers: 0,
  exit_reasons: { TAKE_PROFIT: 1 },
  stale: 0,
};

const STATS: Stats = { count: "2", mean: "2.75", median: "2.75", max: "5.5", min: "0" };

const ANALYTICS: ExecutionAnalytics = {
  notice: "Simulated execution analytics.",
  simulated: true,
  fills: 2,
  orders_with_simulated_fills: 2,
  multi_fill_orders: 0,
  by_basis: { QUOTE: 1, TRADE_THROUGH: 1 },
  slippage_bps: STATS,
  slippage_bps_by_side: { BUY: STATS },
  impact_bps: STATS,
  shortfall_bps: STATS,
  participation: STATS,
  gaps: 0,
  fill_rates: { TAKE_PROFIT: { final: 1, filled: 1, partial: 0, unfilled: 0, fill_rate: "1" } },
  costs: {
    notional: "2005.8",
    commission: "2",
    regulatory_fees: "0.04",
    exchange_fees: "0",
    borrow_accrued: "0",
    total: "2.04",
    cost_bps_of_notional: "10.17",
  },
  evaluations: [{ outcome: "NO_FILL", code: "LIMIT_NOT_THROUGH", count: 3 }],
  execution_model_ids: [1],
  current_model: {
    code: "atlas-paper-conservative",
    version: 1,
    sha256: "abcdef0123456789",
    description: "Conservative simulated execution.",
    rules: ["touching a limit is not a fill: trades or bars must trade strictly through it"],
    classes: { EQUITY: { slippage_bps: "2", touch_participation: "0.5" } },
  },
  limitations: ["IEX quotes and trades are a small share of consolidated US volume"],
};

function routes() {
  return {
    ...LAYOUT_ROUTES,
    "/api/v1/portfolio/accounts": { body: [ACCOUNT] },
    "/api/v1/paper-positions/summary": { body: SUMMARY },
    "/api/v1/paper-positions": (url: URL) => ({
      body: url.searchParams.get("status") === "closed" ? [CLOSED] : [position()],
    }),
    "/api/v1/paper-positions/pos-2": { body: DETAIL },
    "/api/v1/paper-execution/analytics": { body: ANALYTICS },
  };
}

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider>
      <MemoryRouter initialEntries={[path]}>
        <App pollIntervalMs={60_000} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

beforeEach(() => {
  installFakeLive();
});

describe("Open Positions", () => {
  it("shows simulated open positions with mark, P/L, stop, target and holding time", async () => {
    const stub = stubApi(routes() as never);
    renderAt("/positions");
    expect(await screen.findByText(/Simulated positions only\./)).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Open positions" });
    expect(within(table).getByText("PXA")).toBeInTheDocument();
    expect(within(table).getByText("99.58")).toBeInTheDocument();
    expect(within(table).getByText("+8.20 USD")).toBeInTheDocument();
    expect(within(table).getByText("99.40")).toBeInTheDocument(); // the trailing stop binds
    expect(within(table).getByText("101.00")).toBeInTheDocument();
    expect(within(table).getByText("90m")).toBeInTheDocument();
    for (const name of [/close/i, /submit/i, /buy/i, /sell/i, /live/i]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(stub.calls.every((c) => (c.init?.method ?? "GET") === "GET")).toBe(true);
  });
});

describe("Closed Positions", () => {
  it("shows entry, exit, realized and net P/L, fees, exit reason and origin", async () => {
    stubApi(routes() as never);
    renderAt("/positions/closed");
    const table = await screen.findByRole("table", { name: "Closed positions" });
    expect(within(table).getByText("PXB")).toBeInTheDocument();
    expect(within(table).getByText("+14.20 USD")).toBeInTheDocument();
    expect(within(table).getByText("+12.16 USD")).toBeInTheDocument();
    expect(within(table).getByText("2.04 USD")).toBeInTheDocument();
    expect(within(table).getByText("+0.12%")).toBeInTheDocument();
    expect(within(table).getByText("Take profit")).toBeInTheDocument();
    expect(within(table).getByText("momentum_fixture")).toBeInTheDocument();
    fireEvent.click(within(table).getByText("PXB"));
    const events = await screen.findByRole("table", { name: "Position events" });
    expect(within(events).getByText(/closed \(TAKE_PROFIT\)/)).toBeInTheDocument();
    const fills = screen.getByRole("table", { name: "Position fills" });
    expect(within(fills).getByText(/Simulated · Quote/)).toBeInTheDocument();
    expect(within(fills).getByText("+5.50")).toBeInTheDocument();
  });
});

describe("Execution Analytics", () => {
  it("labels the analytics as simulated and shows slippage, fill rates, costs and assumptions", async () => {
    stubApi(routes() as never);
    renderAt("/execution-analytics");
    expect(await screen.findByText(/Simulated execution analytics\./)).toBeInTheDocument();
    const rates = await screen.findByRole("table", { name: "Fill rates" });
    expect(within(rates).getByText("Take profit")).toBeInTheDocument();
    expect(within(rates).getByText("100%")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Slippage statistics" })).toBeInTheDocument();
    expect(screen.getByText("10.17 bps")).toBeInTheDocument();
    expect(screen.getByText(/touching a limit is not a fill/)).toBeInTheDocument();
    expect(screen.getByText(/IEX quotes and trades/)).toBeInTheDocument();
  });
});
