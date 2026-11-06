import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type {
  AssessmentDetail,
  AssessmentSummary,
  BreakerEvent,
  ConfigVersion,
  Reservation,
  ReservationCapacity,
  ReservationEvent,
  RiskOverview,
} from "../types/risk";

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

// TEST FIXTURE reservation payloads (shaped like /api/v1/risk/reservations responses).
const CAPACITY: ReservationCapacity = {
  account_id: "acct-1",
  reserved: {
    active_count: 1,
    risk: "499.45000000",
    gross_exposure: "9188.46000000",
    long_exposure: "9188.46000000",
    short_exposure: "0",
    capital: "9190.84000000",
    margin: "0",
    event_sequence: 4,
  },
  available: {
    computed: true,
    as_of: "2026-11-04T22:30:00Z",
    mark_status: "COMPLETE",
    functional_currency: "USD",
    equity: "96000.00000000",
    risk_budget: "1500.55000000",
    risk_budget_before_reservations: "2000.00",
    per_trade_budget: "480.00",
    gross_exposure: "50931.54000000",
    gross_exposure_limit: "96000.00",
    net_long_room: "50931.54",
    net_short_room: "131880.00",
    short_exposure: "28800.00",
    margin: "48000.00",
    capital: "50929.16000000",
    positions: 6,
    reservation_issues: [],
  },
  counts: { APPROVED: 1, REJECTED: 1, EXPIRED: 0 },
  reconciliation: null,
  approvals_blocked: false,
  note: "A reservation holds risk budget, exposure, capital and margin for an approved trade intent. It is not an order.",
};

const RESERVATION: Reservation = {
  id: "rsv-1",
  account_id: "acct-1",
  idempotency_key: "worker-1:proposal-1",
  source: "STRATEGY_PROPOSAL",
  symbol: "RSKB",
  asset_class: "EQUITY",
  direction: "LONG",
  functional_currency: "USD",
  entry: "99.7000000000",
  stop: "94.7000000000",
  status: "PARTIALLY_CONSUMED",
  active: true,
  proposed_quantity: "500",
  approved_quantity: "92",
  consumed_quantity: "40",
  released_quantity: "0",
  remaining_quantity: "52",
  approved: { risk: "499.45", exposure: "9188.46", capital: "9190.84", margin: "0" },
  remaining: { risk: "282.30", exposure: "5193.48", capital: "5194.82", margin: "0" },
  execution_link: "ORDER_ATTACHED",
  order_ref: "order-1",
  expires_at: "2026-11-04T22:35:00Z",
  expiry_deferred_at: null,
  reasons: [{ code: "REQUESTED_QUANTITY_REDUCED", outcome: "RESTRICT", check: "requested_quantity", message: "requested 500, permitted 92" }],
  requested_by: "system:strategy-worker",
  requested_at: "2026-11-04T22:30:00Z",
};

const RESERVATION_EVENTS: ReservationEvent[] = [
  {
    id: 2,
    reservation_id: "rsv-2",
    symbol: "RSKC",
    account_sequence: 4,
    reservation_sequence: 2,
    event_type: "REJECTED",
    from_status: "PENDING",
    to_status: "REJECTED",
    quantity: "0",
    risk: "0",
    exposure: "0",
    resolution: null,
    actor: "system:risk-reservations",
    reason: "rejected: ASSESSMENT_REJECTED, MISSING_STOP",
    occurred_at: "2026-11-04T22:31:00Z",
  },
];

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
      pending_source: "1 active risk reservation(s)",
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
    reserved_exposure: CAPACITY,
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
    "/api/v1/risk/reservations": { body: [RESERVATION] },
    "/api/v1/risk/reservations/events": { body: RESERVATION_EVENTS },
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
    expect(screen.getByText(/configuration version 1/)).toBeInTheDocument();
  });

  it("shows reserved risk, available capacity, active reservations and their history", async () => {
    stubApi(routes(overview()) as never);
    renderRisk();
    const panel = (await screen.findByRole("heading", { name: "Risk reservations" })).closest("section") ?? document.body;
    const scope = within(panel as HTMLElement);
    expect(scope.getByText("499.45 USD")).toBeInTheDocument();
    expect(scope.getByText("1,500.55 USD")).toBeInTheDocument();
    expect(scope.getByText("50,931.54 USD")).toBeInTheDocument();
    const active = await screen.findByRole("table", { name: "Active reservations" });
    expect(within(active).getByText("RSKB")).toBeInTheDocument();
    expect(within(active).getByText("Partially consumed")).toBeInTheDocument();
    expect(within(active).getByText("282.30 USD")).toBeInTheDocument();
    const history = await screen.findByRole("table", { name: "Reservation events" });
    expect(within(history).getByText(/MISSING_STOP/)).toBeInTheDocument();
    // Read-only: nothing on the page reserves, releases or consumes.
    expect(screen.queryByRole("button", { name: /reserve|release|consume/i })).toBeNull();
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
