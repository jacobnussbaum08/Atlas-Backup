import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type { Protocol, Scorecard } from "../types/evidence";

// TEST FIXTURES shaped like /api/v1/evidence responses. Invented numbers, not results.
const PROTOCOL: Protocol = {
  id: "p1",
  name: "atlas-paper-evidence",
  version: 1,
  criteria_sha256: "9".repeat(64),
  rationale: "fixture",
  registered_by: "operator:test",
  registered_at: "2026-11-12T16:16:23Z",
};

const CARD: Scorecard = {
  id: "s1",
  verdict: "NOT_READY",
  as_of: "2026-11-12T20:00:00Z",
  result_sha256: "a".repeat(64),
  entries: [
    { subject: "momentum", classification: "REJECTED", reasons: ["out of sample: net expectancy -3.1 per trade is not positive"] },
    { subject: "PORTFOLIO", classification: "INSUFFICIENT_EVIDENCE", reasons: [] },
  ],
  result: {
    statement: "fixture",
    protocol: { id: "p1", name: "atlas-paper-evidence", version: 1, criteria_sha256: "9".repeat(64), registered_at: "2026-11-12T16:16:23Z" },
    as_of: "2026-11-12T20:00:00Z",
    verdict: "NOT_READY",
    strategies: {
      momentum: {
        classification: "REJECTED",
        reasons: ["out of sample: net expectancy -3.1 per trade is not positive"],
        rejections: ["out of sample: net expectancy -3.1 per trade is not positive"],
        criteria: [
          { code: "OOS_TRADES", section: "OUT_OF_SAMPLE", status: "PASS", value: 140, threshold: 30, message: "140 closed out-of-sample trades (need 30)", blocking: true },
          { code: "PAPER_EVIDENCE", section: "PAPER", status: "NOT_AVAILABLE", value: null, threshold: null, message: "no paper trading", blocking: true },
        ],
        backtest: {
          source: "fixture",
          first: "2022-01-03",
          last: "2025-04-17",
          sessions: 830,
          net_return: 0.031,
          max_drawdown: 0.04,
          sharpe_annualized: 0.9,
          trade_statistics: { trades: 210, net_pnl: 3100, profit_factor: 1.3 },
          note: "in-sample: the parameters were chosen on this period; it passes no criterion",
        },
        out_of_sample: {
          trades: 140,
          sessions: 819,
          net_pnl: -434,
          costs: 120,
          profit_factor: 0.93,
          expectancy: -3.1,
          win_rate: 0.41,
          max_drawdown: 0.05,
          net_return: -0.004,
          sharpe_annualized: -0.2,
          by_direction: {},
          by_regime: {},
          deflated_sharpe: 0.1,
          pbo: 0.7,
          benchmark_sharpe_annualized: 0.8,
          cost_stress_net_pnl: -700,
          holdout: null,
          baselines: { benchmark: "SPY", benchmark_return: 0.35, equal_weight_universe_return: 0.2, cash_return: 0 },
          run: { run_id: "r1", period: ["2021-01-04", "2026-06-26"], verdict_8b: "NOT_ROBUST" },
        },
        paper: null,
      },
    },
    portfolio: {
      classification: "INSUFFICIENT_EVIDENCE",
      criteria: [
        { code: "PORTFOLIO_PAPER_TRADES", section: "PAPER", status: "INSUFFICIENT", value: 0, threshold: 200, message: "0 trades (need 200)", blocking: true },
      ],
      oos_trades: 140,
      oos_source: "fixture",
      data_audit: { id: "a1", status: "WARN", failures: [], warnings: ["survivorship bias: PRESENT"] },
    },
    paper_observation: null,
  },
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

describe("Evidence & Readiness", () => {
  beforeEach(() => installFakeLive());

  it("shows the verdict and keeps backtest, out-of-sample and paper evidence apart", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/evidence/scorecards/latest": { body: CARD },
      "/api/v1/evidence/protocols": { body: [PROTOCOL] },
    });
    renderAt("/evidence");
    expect(await screen.findByRole("heading", { name: "Evidence & Readiness" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Backtest (in sample)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Out of sample" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Paper trading" })).toBeInTheDocument();
    expect(screen.getByText("No paper account in this scorecard.")).toBeInTheDocument();
    expect(screen.getAllByText("Not ready").length).toBeGreaterThan(0);
    expect(screen.getByText(/never declared ready for live trading/)).toBeInTheDocument();
    const protocols = screen.getByRole("table", { name: "protocols" });
    expect(within(protocols).getByText("atlas-paper-evidence v1")).toBeInTheDocument();
  });

  it("says plainly when no scorecard exists", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/evidence/scorecards/latest": { status: 404, body: { error: { code: "not_found", message: "none" } } },
      "/api/v1/evidence/protocols": { body: [] },
    });
    renderAt("/evidence");
    expect(await screen.findByText("No readiness scorecard yet")).toBeInTheDocument();
  });
});
