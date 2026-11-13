import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type { NotificationFeed, Performance } from "../types/performance";

// TEST FIXTURES shaped like the API responses. Invented numbers, not results.
const ACCOUNTS = [{ id: "acct-1", name: "Atlas paper account", purpose: "PRIMARY" }];
const STATS = {
  trades: 2,
  wins: 1,
  losses: 1,
  win_rate: "0.500000",
  net_pnl: "120.00",
  realized_pnl: "125.00",
  costs: "5.00",
  average_win: "320.00",
  average_loss: "-200.00",
  profit_factor: "1.600000",
  expectancy: "60.00",
};
const PERFORMANCE: Performance = {
  account_id: "acct-1",
  account_name: "Atlas paper account",
  currency: "USD",
  simulated: true,
  disclaimer: "Simulated paper trading: fixture.",
  curve: {
    days: 1,
    checkpoints_excluded: 2,
    first_day: "2026-11-16",
    last_day: "2026-11-16",
    points: [{ day: "2026-11-16", equity: "100120.00" }],
    drawdown: [{ day: "2026-11-16", drawdown: "0.000000" }],
  },
  summary: {
    total_return: null,
    max_drawdown: "0.000000",
    current_drawdown: "0.000000",
    daily_returns: 0,
    minimum: 20,
    sharpe: null,
    sortino: null,
    volatility_annualized: null,
    reason: "0 daily returns; at least 20 are needed",
  },
  monthly_returns: [],
  trades: STATS,
  by_strategy: [{ ...STATS, strategy_id: "mean_reversion" }],
  by_direction: [{ ...STATS, direction: "LONG" }],
  by_exit_reason: [{ ...STATS, exit_reason: "TAKE_PROFIT" }],
  recent_trades: [],
  benchmark: { symbol: "SPY", return: null, start: null, end: null, reason: "the equity curve needs at least two days" },
};
const FEED: NotificationFeed = {
  account_id: "acct-1",
  kinds: ["POSITION_OPENED", "POSITION_CLOSED", "TRADE_REJECTED"],
  note: "fixture",
  items: [
    {
      id: "POSITION_CLOSED:2",
      kind: "POSITION_CLOSED",
      severity: "INFO",
      occurred_at: "2026-11-16T15:00:00Z",
      title: "Closed PGR (paper): net +120.00 USD",
      message: "closed (TAKE_PROFIT)",
      subject: {},
      simulated: true,
    },
    {
      id: "TRADE_REJECTED:1",
      kind: "TRADE_REJECTED",
      severity: "INFO",
      occurred_at: "2026-11-16T05:30:00Z",
      title: "Risk rejected: LONG ALL",
      message: "KILL_SWITCH",
      subject: {},
      simulated: true,
    },
  ],
};

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

describe("Performance (spec 22.6)", () => {
  beforeEach(() => installFakeLive());

  it("shows sample sizes and withholds ratios below their minimum", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/portfolio/accounts": { body: ACCOUNTS },
      "/api/v1/portfolio/performance": { body: PERFORMANCE },
    });
    renderAt("/performance");
    expect(await screen.findByRole("heading", { name: "Performance" })).toBeInTheDocument();
    expect(await screen.findByText("Simulated paper trading: fixture.")).toBeInTheDocument();
    expect(screen.getByText(/0 daily returns; at least 20 are needed/)).toBeInTheDocument();
    expect(screen.getByText("Not enough recorded valuations")).toBeInTheDocument();
    expect(screen.getByText(/Not available: the equity curve needs at least two days/)).toBeInTheDocument();
    const byStrategy = screen.getByRole("table", { name: "performance by strategy" });
    expect(within(byStrategy).getByText("+120.00 USD")).toBeInTheDocument();
  });
});

describe("Notifications (spec 23)", () => {
  beforeEach(() => installFakeLive());

  it("lists stored events newest first and filters by kind", async () => {
    const stub = stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/portfolio/accounts": { body: ACCOUNTS },
      "/api/v1/notifications": { body: FEED },
    });
    renderAt("/notifications");
    expect(await screen.findByText("Closed PGR (paper): net +120.00 USD")).toBeInTheDocument();
    expect(screen.getByText("Risk rejected: LONG ALL")).toBeInTheDocument();
    expect(screen.getByText(/Telegram and email delivery are not built/)).toBeInTheDocument();
    screen.getByRole("radio", { name: "Trade rejected" }).click();
    await screen.findByText("Closed PGR (paper): net +120.00 USD");
    const kinds = stub.calls.map((c) => c.url.searchParams.get("kind"));
    expect(kinds).toContain("TRADE_REJECTED");
  });
});
