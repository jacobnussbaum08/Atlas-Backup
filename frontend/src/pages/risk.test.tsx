import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type { AssessmentDetail, AssessmentSummary, BreakerEvent, ConfigVersion, RiskOverview } from "../types/risk";

// TEST FIXTURES: payloads shaped like /api/v1/risk responses. No market data.
const BREAKERS = [
  "DAILY_LOSS",
  "WEEKLY_LOSS",
  "MAX_DRAWDOWN",
  "EXCESSIVE_EXPOSURE",
  "DATA_QUALITY",
  "RECONCILIATION_FAILURE",
  "EXECUTION_SAFETY",
  "KILL_SWITCH",
];

const VERSION: ConfigVersion = {
  id: "cfg-1",
  version: 1,
  change_kind: "INITIAL",
  previous_version_id: null,
  hard_limits: { risk_per_trade_pct: "0.005", max_open_positions: 8, daily_loss_limit_pct: "0.02", weekly_loss_limit_pct: "0.05", max_drawdown_pct: "0.10" },
  preferences: { slippage_bps: "5" },
  hard_limits_sha256: "a".repeat(64),
  preferences_sha256: "b".repeat(64),
  changes: [{ section: "hard_limits", field: "risk_per_trade_pct", previous: null, new: "0.005" }],
  actor: "operator:jacob",
  reason: "starting values",
  created_at: "2026-11-04T21:00:00Z",
};

function overview(tripped: string[] = []): RiskOverview {
  return {
    account: { id: "acct-1", name: "Atlas paper account", purpose: "PRIMARY", base_currency: "USD" },
    trading_halted: tripped.length > 0,
    active_breakers: tripped,
    kill_switch: tripped.includes("KILL_SWITCH") ? "TRIPPED" : "CLEAR",
    breakers: BREAKERS.map((b) => ({
      breaker: b,
      status: tripped.includes(b) ? "TRIPPED" : "CLEAR",
      recovery: b === "DATA_QUALITY" ? "AUTO_WHEN_CLEAR" : "OPERATOR_RESET",
      since: tripped.includes(b) ? "2026-11-04T21:05:00Z" : null,
      last_event: tripped.includes(b) ? "TRIPPED" : null,
      actor: tripped.includes(b) ? "operator:jacob" : null,
      reason: tripped.includes(b) ? "manual halt" : null,
      events: tripped.includes(b) ? 1 : 0,
    })),
    limits: {
      account_id: "acct-1",
      configured: true,
      message: null,
      active: VERSION,
      note: "Initial Atlas starting values. They are not optimal or recommended settings and have not been tuned.",
      governance: "Hard limits change only through a new immutable version written by an operator.",
      breaker_recovery: {},
      breaker_response: {},
      timezone_policy: "XNYS calendar (America/New_York)",
    },
    live: {
      as_of: "2026-11-04T22:30:00Z",
      recorded: false,
      mark_status: "COMPLETE",
      mark_issues: [],
      ledger_sequence: 3,
      functional_currency: "USD",
      equity: "96000.00000000",
      contributed_capital: "100000.00000000",
      cash: "60120.00000000",
      available_capital: "60120.00000000",
      long_exposure: "35880.00000000",
      short_exposure: "0",
      gross_exposure: "35880.00000000",
      net_exposure: "35880.00000000",
      margin_used: "0",
      pending_exposure: "0",
      pending_source: "none: risk reservations arrive in Phase 6C",
      of_equity: { gross_exposure: "0.37375", net_exposure: "0.37375", long_exposure: "0.37375", short_exposure: "0", margin_used: "0" },
      open_positions: 1,
      daily_loss: "4000.00000000",
      daily_baseline: "100000.00000000",
      weekly_loss: "4000.00000000",
      weekly_baseline: "100000.00000000",
      peak_equity: "100000.00000000",
      drawdown: "4000.00000000",
      available_risk_budget: "0",
      baselines: {
        DAILY: { period: "2026-11-04", period_start: "2026-11-03T21:00:00Z", equity: "100000.00", basis: "FUNDED_IN_PERIOD", observed_at: "2026-11-03T22:30:00Z", source: "OPENING_FUNDING:opening_funding" },
        WEEKLY: null,
      },
      reconciliation: { status: "PASSED", current: true, run_id: "run-1" },
      conditions: [],
      breakdown: {
        assets: [
          {
            asset_id: "asset-1",
            symbol: "RSKA",
            asset_class: "EQUITY",
            direction: "LONG",
            quantity: "400.000000000000000000",
            exposure: "35880.00000000",
            of_equity: "0.37375",
            margin_required: "0",
            sector: "SEMICONDUCTORS",
            cluster: null,
            price: "89.7000000000",
            price_status: "FRESH",
            price_source: "BAR_CLOSE",
            price_observed_at: "2026-11-04T05:00:00Z",
          },
        ],
        asset_classes: [{ asset_class: "EQUITY", long: "35880", short: "0", gross: "35880", net: "35880", of_equity: "0.37375" }],
        sectors: [{ sector: "SEMICONDUCTORS", long: "35880", short: "0", gross: "35880", net: "35880", of_equity: "0.37375" }],
        clusters: [],
      },
    },
    latest_snapshot: null,
    assessment_counts: { PASS_FOR_RESERVATION: 1, REJECTED: 1, INSUFFICIENT_DATA: 0, RISK_HALTED: 0 },
    reserved_exposure: { amount: "0", note: "risk reservations do not exist before Phase 6C" },
    disclaimer: "Simulated paper account. The risk engine assesses proposals; it executes nothing.",
  };
}

const ASSESSMENT: AssessmentSummary = {
  id: "as-1",
  assessed_at: "2026-11-04T22:31:00Z",
  valid_until: "2026-11-04T22:36:00Z",
  result: "REJECTED",
  source: "OPERATOR",
  strategy_proposal_id: null,
  ai_decision_id: null,
  asset_id: "asset-1",
  symbol: "RSKA",
  asset_class: "EQUITY",
  direction: "LONG",
  entry: "89.7000000000",
  stop: null,
  target: null,
  requested_quantity: null,
  permitted_quantity: null,
  functional_currency: "USD",
  equity: "96000.00000000",
  risk_budget: null,
  potential_loss: null,
  notional: null,
  estimated_costs: null,
  binding_constraint: null,
  config_version_id: "cfg-1",
  ledger_sequence: 3,
  requested_by: "operator:cli",
  engine_version: "6B.1",
  reasons: ["MISSING_STOP"],
};

const DETAIL: AssessmentDetail = {
  ...ASSESSMENT,
  checks: [
    { ordinal: 1, check: "circuit_breakers", outcome: "PASS", reason_code: null, message: "no circuit breaker is active", measured: null, limit: null, unit: null, details: {} },
    { ordinal: 2, check: "stop", outcome: "FAIL", reason_code: "MISSING_STOP", message: "a stop level is required by policy and the proposal has none", measured: null, limit: null, unit: null, details: {} },
  ],
  calculations: {},
  exposure_after: {},
  inputs_sha256: "c".repeat(64),
  content_sha256: "d".repeat(64),
  note: "PASS_FOR_RESERVATION is not an execution approval.",
};

const EVENTS: BreakerEvent[] = [];

function routes(state: RiskOverview, extra: Record<string, unknown> = {}) {
  return {
    ...LAYOUT_ROUTES,
    "/api/v1/risk/state": { body: state },
    "/api/v1/risk/assessments": { body: [ASSESSMENT] },
    "/api/v1/risk/assessments/as-1": { body: DETAIL },
    "/api/v1/risk/circuit-breakers/events": { body: EVENTS },
    "/api/v1/risk/configurations": { body: [VERSION] },
    ...extra,
  };
}

function renderRisk() {
  return render(
    <DisplayZoneProvider>
      <MemoryRouter initialEntries={["/risk"]}>
        <App pollIntervalMs={60_000} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

beforeEach(() => {
  installFakeLive();
});

describe("Risk Center", () => {
  it("shows limits, exposure, losses and breakers from the API", async () => {
    stubApi(routes(overview()) as never);
    renderRisk();
    expect(await screen.findByRole("heading", { name: "Risk Center" })).toBeInTheDocument();
    expect(await screen.findByText("96,000.00 USD")).toBeInTheDocument();
    expect(screen.getByText(/not optimal or recommended/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Circuit breakers" })).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Daily loss used" })).toHaveAttribute("aria-valuenow", "100");
    const positions = screen.getByRole("table", { name: "Positions" });
    expect(within(positions).getByText("RSKA")).toBeInTheDocument();
    expect(within(positions).getByText("37.38%")).toBeInTheDocument();
    expect(screen.getByText("risk reservations do not exist before Phase 6C")).toBeInTheDocument();
    expect(screen.getByText(/configuration version 1/)).toBeInTheDocument();
  });

  it("lists assessments with their reasons and expands the recorded checks", async () => {
    stubApi(routes(overview()) as never);
    renderRisk();
    const table = await screen.findByRole("table", { name: "Risk assessments" });
    expect(within(table).getByText("Missing stop")).toBeInTheDocument();
    fireEvent.click(within(table).getByRole("button", { name: "Show checks for RSKA" }));
    const checks = await screen.findByRole("table", { name: "Assessment checks" });
    expect(within(checks).getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText("PASS_FOR_RESERVATION is not an execution approval.")).toBeInTheDocument();
  });

  it("shows a halt and cannot release the kill switch from the dashboard", async () => {
    stubApi(routes(overview(["KILL_SWITCH"])) as never);
    renderRisk();
    expect(await screen.findByText("New entries are halted")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Engage kill switch/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /release/i })).toBeNull();
    expect(screen.getAllByText(/kill-switch release/).length).toBeGreaterThan(0);
  });

  it("engages the kill switch with a reason and the operator token", async () => {
    let posted: { body: unknown; token: string | null } | null = null;
    stubApi(
      routes(overview(), {
        "/api/v1/risk/kill-switch/engage": (_url: URL, init?: RequestInit) => {
          posted = {
            body: JSON.parse(String(init?.body)),
            token: new Headers(init?.headers).get("X-Atlas-Operator-Token"),
          };
          return { body: { account_id: "acct-1", engaged: true, already_engaged: false, release: "cli" } };
        },
      }) as never,
    );
    renderRisk();
    const form = await screen.findByRole("form", { name: "Engage the kill switch" });
    const button = within(form).getByRole("button", { name: /Engage kill switch/ });
    expect(button).toBeDisabled();
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "market dislocation" } });
    fireEvent.change(within(form).getByLabelText("Operator token"), { target: { value: "tok" } });
    fireEvent.click(button);
    expect(await within(form).findByText("Kill switch engaged: new entries are halted.")).toBeInTheDocument();
    expect(posted).toEqual({ body: { reason: "market dislocation" }, token: "tok" });
  });

  it("explains an account that does not exist yet", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      "/api/v1/risk/state": {
        status: 404,
        body: { error: { code: "not_found", message: "no primary paper account exists yet: run `python -m app.accounting.cli init`" } },
      },
    } as never);
    renderRisk();
    expect(await screen.findByText("The paper account has not been created yet")).toBeInTheDocument();
  });
});
