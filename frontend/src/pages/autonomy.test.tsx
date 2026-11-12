import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type {
  ControlEvent,
  OpsAlert,
  OrchestrationJob,
  OrchestrationStatus,
  Protection,
  TradeIntent,
  TradingMode,
} from "../types/orchestration";

// TEST FIXTURES: payloads shaped like /api/v1/orchestration responses. No market data.
function status(mode: TradingMode, isDefault = false): OrchestrationStatus {
  return {
    account_id: "acct-1",
    account_name: "Atlas paper account",
    simulated: true,
    control: {
      account_id: "acct-1",
      mode,
      entries_allowed: mode === "ENABLED",
      sequence: isDefault ? 0 : 3,
      since: isDefault ? null : "2026-11-11T15:00:00Z",
      actor: isDefault ? null : "operator:jacob",
      reason: isDefault ? null : "start the paper trial",
      default: isDefault,
    },
    orchestrator: {
      health: "RUNNING",
      detail: "Heartbeat current; recorded state RUNNING.",
      worker_enabled_in_api_env: true,
      stale_after_seconds: 90,
      worker_id: "orchestrator:host-1",
      state: "RUNNING",
      trading_mode_seen: mode,
      heartbeat_at: "2026-11-11T15:05:00Z",
      heartbeat_age_seconds: 4,
      started_at: "2026-11-11T14:00:00Z",
      stopped_at: null,
      last_tick_at: "2026-11-11T15:04:50Z",
      last_tick_ms: 412,
      counters: { ticks: 120, intents_taken: 2 },
      last_error: null,
      last_error_at: null,
    },
    intents: {
      RECEIVED: 0,
      ORDER_CREATED: 1,
      RISK_REJECTED: 1,
      EXECUTION_REFUSED: 0,
      NOT_TRADED: 0,
      INVALID: 0,
      DEAD: 0,
    },
    jobs: { RUNNING: 0, SUCCEEDED: 5, FAILED: 0, DEAD: 0, SUPERSEDED: 0 },
    outbox: { counts: { PENDING: 0, DELIVERED: 4, DEAD: 0 }, oldest_pending_at: null },
    notes: [],
  };
}

const INTENTS: TradeIntent[] = [
  {
    id: "int-1",
    account_id: "acct-1",
    ai_decision_id: "dec-1",
    decision_run_id: "run-1",
    asset_id: "asset-1",
    symbol: "ORCA",
    direction: "LONG",
    status: "ORDER_CREATED",
    trading_mode: "ENABLED",
    reservation_id: "rsv-1",
    reservation_decision: "APPROVED",
    order_id: "ord-12345678-aaaa",
    execution_request_id: "req-1",
    reasons: [],
    error: null,
    decision_expires_at: "2026-11-11T21:00:00Z",
    received_at: "2026-11-11T15:00:00Z",
    decided_at: "2026-11-11T15:00:01Z",
  },
  {
    id: "int-2",
    account_id: "acct-1",
    ai_decision_id: "dec-2",
    decision_run_id: "run-1",
    asset_id: "asset-2",
    symbol: "ORCB",
    direction: "LONG",
    status: "RISK_REJECTED",
    trading_mode: "ENABLED",
    reservation_id: "rsv-2",
    reservation_decision: "REJECTED",
    order_id: null,
    execution_request_id: null,
    reasons: [{ code: "MAX_OPEN_POSITIONS", message: "8 positions open" }],
    error: null,
    decision_expires_at: "2026-11-11T21:00:00Z",
    received_at: "2026-11-11T15:00:00Z",
    decided_at: "2026-11-11T15:00:01Z",
  },
];

const JOBS: OrchestrationJob[] = [
  {
    id: 1,
    kind: "ACCOUNTING_RECONCILE",
    slot: "2026-11-11T15:00:00Z",
    status: "SUCCEEDED",
    attempts: 1,
    reclaimed: 0,
    lease_owner: null,
    lease_expires_at: null,
    next_attempt_at: null,
    started_at: "2026-11-11T15:00:00Z",
    finished_at: "2026-11-11T15:00:02Z",
    result: { passed: true },
    error: null,
  },
];

const HISTORY: ControlEvent[] = [
  {
    sequence: 1,
    action: "ENABLE",
    from_mode: "DISABLED",
    to_mode: "ENABLED",
    actor: "operator:jacob",
    reason: "start the paper trial",
    occurred_at: "2026-11-11T14:00:00Z",
    details: {},
  },
];

function protection(open: boolean): Protection {
  return {
    at: "2026-11-11T15:05:00Z",
    gate_open: open,
    reasons: open
      ? []
      : [{ code: "POSITION_MONITOR_DOWN", message: "the position monitor is not running (no heartbeat for 300 s)" }],
    position_monitor: { alive: open, detail: open ? "heartbeat 3 s ago (RUNNING)" : "no heartbeat for 300 s" },
    paper_execution: { alive: true, state: "RUNNING", detail: "heartbeat 1 s ago" },
    open_positions: 2,
    working_orders: 1,
    pending_execution_events: 0,
  };
}

const ALERTS: OpsAlert[] = [
  {
    id: 7,
    component: "orchestration",
    event_type: "POSITION_MONITOR_DOWN",
    severity: "CRITICAL",
    summary: "Position monitor not running: no heartbeat for 300 s; 2 open position(s)",
    subject_kind: "worker",
    subject_id: "position-monitor-worker",
    occurred_at: "2026-11-11T15:05:00Z",
    action: "Start the position monitor (python -m app.workers.position_monitor).",
    acknowledged: false,
    acknowledgements: [],
  },
];

function routes(mode: TradingMode, isDefault = false, extra: Record<string, unknown> = {}) {
  return {
    ...LAYOUT_ROUTES,
    "/api/v1/orchestration/status": { body: status(mode, isDefault) },
    "/api/v1/orchestration/intents": { body: INTENTS },
    "/api/v1/orchestration/jobs": { body: JOBS },
    "/api/v1/orchestration/controls/history": { body: isDefault ? [] : HISTORY },
    "/api/v1/orchestration/protection": { body: protection(true) },
    "/api/v1/orchestration/alerts": { body: [] },
    ...extra,
  };
}

function renderAutonomy() {
  return render(
    <DisplayZoneProvider>
      <MemoryRouter initialEntries={["/autonomy"]}>
        <App pollIntervalMs={60_000} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

beforeEach(() => {
  installFakeLive();
});

describe("Autonomous Trading", () => {
  it("shows the default disabled control and offers no way to enable trading", async () => {
    stubApi(routes("DISABLED", true) as never);
    renderAutonomy();
    expect(await screen.findByRole("heading", { name: "Autonomous Trading" })).toBeInTheDocument();
    expect(await screen.findByText(/has never been enabled for this account/)).toBeInTheDocument();
    expect(screen.getByText(/Paper trading only/)).toBeInTheDocument();
    for (const word of [/^enable/i, /resume/i, /clear/i, /buy/i, /submit order/i]) {
      expect(screen.queryByRole("button", { name: word })).toBeNull();
    }
    expect(screen.getAllByText(/app.orchestration.cli enable/).length).toBeGreaterThan(0);
    expect(await screen.findByText("No operator has changed the trading control")).toBeInTheDocument();
  });

  it("lists intents with their outcome, jobs and the control history", async () => {
    stubApi(routes("ENABLED") as never);
    renderAutonomy();
    const intents = await screen.findByRole("table", { name: "Trade intents" });
    expect(within(intents).getByText("ORCA")).toBeInTheDocument();
    expect(within(intents).getByText("Order created")).toBeInTheDocument();
    expect(within(intents).getByText("MAX_OPEN_POSITIONS")).toBeInTheDocument();
    const jobs = await screen.findByRole("table", { name: "Scheduled jobs" });
    expect(within(jobs).getByText("passed")).toBeInTheDocument();
    const history = await screen.findByRole("table", { name: "Control history" });
    expect(within(history).getByText("operator:jacob")).toBeInTheDocument();
    expect(screen.getByText(/412 ms/)).toBeInTheDocument();
  });

  it("pauses with a reason and the operator token", async () => {
    let sent: { body: unknown; token: string | null } | null = null;
    stubApi(
      routes("ENABLED", false, {
        "/api/v1/orchestration/controls/pause": (_url: URL, init?: RequestInit) => {
          const headers = new Headers(init?.headers);
          sent = { body: JSON.parse(String(init?.body)), token: headers.get("X-Atlas-Operator-Token") };
          return {
            body: {
              account_id: "acct-1",
              action: "PAUSE",
              changed: true,
              control: { ...status("PAUSED").control },
            },
          };
        },
      }) as never,
    );
    renderAutonomy();
    const form = await screen.findByRole("form", { name: "Trading controls" });
    const pause = within(form).getByRole("button", { name: /Pause/ });
    expect(pause).toBeDisabled(); // no reason yet
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "market looks broken" } });
    fireEvent.change(within(form).getByLabelText("Operator token"), { target: { value: "tok" } });
    fireEvent.click(pause);
    expect(await within(form).findByText("Trading is now paused.")).toBeInTheDocument();
    expect(sent).toEqual({ body: { reason: "market looks broken" }, token: "tok" });
  });

  it("engages the emergency stop and reports cancelled entries", async () => {
    stubApi(
      routes("ENABLED", false, {
        "/api/v1/orchestration/controls/emergency-stop": {
          body: {
            account_id: "acct-1",
            action: "EMERGENCY_STOP",
            changed: true,
            control: { ...status("EMERGENCY_STOP").control },
            kill_switch_engaged_now: true,
            cancel_requested: ["ord-1", "ord-2"],
            cancel_failed: [],
            clear: "python -m app.orchestration.cli clear-emergency",
          },
        },
      }) as never,
    );
    renderAutonomy();
    const form = await screen.findByRole("form", { name: "Trading controls" });
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "stop everything" } });
    fireEvent.click(within(form).getByRole("button", { name: /Emergency stop/ }));
    expect(await within(form).findByText(/Emergency stop engaged: 2 entry order\(s\) cancelled/)).toBeInTheDocument();
  });

  it("shows a closed entry gate and the open alerts with their action", async () => {
    stubApi(
      routes("ENABLED", false, {
        "/api/v1/orchestration/protection": { body: protection(false) },
        "/api/v1/orchestration/alerts": { body: ALERTS },
      }) as never,
    );
    renderAutonomy();
    expect(await screen.findByText("Entry gate closed")).toBeInTheDocument();
    expect(screen.getByText(/New entries are refused: the position monitor is not running/)).toBeInTheDocument();
    const alerts = await screen.findByRole("table", { name: "Open alerts" });
    expect(within(alerts).getByText("Critical")).toBeInTheDocument();
    expect(within(alerts).getByText(/Start the position monitor/)).toBeInTheDocument();
  });

  it("shows an open entry gate and no alerts", async () => {
    stubApi(routes("ENABLED") as never);
    renderAutonomy();
    expect(await screen.findByText("Entry gate open")).toBeInTheDocument();
    expect(await screen.findByText("No open alerts")).toBeInTheDocument();
  });

  it("shows an engaged emergency stop and a refused control", async () => {
    stubApi(
      routes("EMERGENCY_STOP", false, {
        "/api/v1/orchestration/controls/emergency-stop": {
          status: 401,
          body: { error: { code: "unauthorized", message: "missing or invalid X-Atlas-Operator-Token" } },
        },
      }) as never,
    );
    renderAutonomy();
    expect(await screen.findByText(/Emergency stop is engaged/)).toBeInTheDocument();
    const form = await screen.findByRole("form", { name: "Trading controls" });
    expect(within(form).getByRole("button", { name: /Pause/ })).toBeDisabled();
    fireEvent.change(within(form).getByLabelText("Reason"), { target: { value: "again" } });
    fireEvent.click(within(form).getByRole("button", { name: /Emergency stop/ }));
    expect(await within(form).findByText(/HTTP 401/)).toBeInTheDocument();
  });
});
