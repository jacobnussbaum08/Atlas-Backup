import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type { AccountView } from "../types/portfolio";
import type {
  ExecutionRequest,
  OrderEvent,
  OrdersSummary,
  PaperBrokerStatus,
  PaperOrder,
  PaperOrderDetail,
} from "../types/orders";

// TEST FIXTURES: payloads shaped like /api/v1/paper-orders responses. Simulated orders only.
const ACCOUNT: AccountView = {
  id: "acct-1",
  name: "Atlas paper account",
  purpose: "PRIMARY",
  base_currency: "USD",
  status: "ACTIVE",
  created_at: "2026-11-01T00:00:00Z",
  entries: 3,
  opening_funding: "100000.00",
  opening_funding_posted_at: "2026-11-01T00:00:00Z",
};

function order(extra: Partial<PaperOrder> = {}): PaperOrder {
  return {
    id: "ord-1",
    simulated: true,
    account_id: "acct-1",
    asset_id: "asset-1",
    symbol: "PXA",
    asset_class: "EQUITY",
    currency: "USD",
    purpose: "ENTRY",
    side: "BUY",
    direction: "LONG",
    order_type: "LIMIT",
    status: "PARTIALLY_FILLED",
    quantity: "92",
    filled_quantity: "40",
    remaining_quantity: "52",
    average_fill_price: "99.5",
    limit_price: "99.6",
    stop_price: null,
    trail_amount: null,
    trail_percent: null,
    protection_price: "99.7",
    quantity_increment: "1",
    price_increment: "0.01",
    reservation_id: "res-1",
    risk_assessment_id: "asm-1",
    parent_order_id: null,
    strategy_proposal_id: null,
    ai_decision_id: null,
    broker_order_id: "bk-1",
    status_reason: "40 filled at 99.5 (CONTROLLED_TEST)",
    exit_reason: null,
    requested_by: "operator:jacob",
    submitted_at: "2026-11-04T21:00:00Z",
    expires_at: "2026-11-05T21:00:00Z",
    closed_at: null,
    updated_at: "2026-11-04T21:05:00Z",
    ...extra,
  };
}

const EVENTS: OrderEvent[] = [
  ["CREATED", null, "PROPOSED", "OPERATOR", "BUY 92 PXA LIMIT requested"],
  ["APPROVED", "PROPOSED", "APPROVED", "EXECUTION_ENGINE", "authorized and revalidated; reservation attached"],
  ["ACCEPTED", "APPROVED", "OPEN", "PAPER_BROKER", "accepted by the simulated paper broker"],
  ["FILL", "OPEN", "PARTIALLY_FILLED", "PAPER_BROKER", "40 filled at 99.5 (CONTROLLED_TEST)"],
].map(([event_type, from_status, to_status, source, reason], i) => ({
  id: i + 1,
  order_id: "ord-1",
  symbol: "PXA",
  sequence: i + 1,
  event_type: event_type as string,
  from_status: from_status as PaperOrder["status"] | null,
  to_status: to_status as PaperOrder["status"],
  quantity: event_type === "FILL" ? "40" : "0",
  price: event_type === "FILL" ? "99.5" : null,
  source: source as string,
  actor: "system:atlas-paper-broker",
  reason: reason as string,
  broker_event_id: null,
  broker_sequence: null,
  occurred_at: `2026-11-04T21:0${i}:00Z`,
  details: {},
}));

const DETAIL: PaperOrderDetail = {
  notice: "Simulated paper order handled by the isolated Atlas paper broker. No brokerage is connected and no real order exists.",
  order: order(),
  events: EVENTS,
  fills: [
    {
      id: "fill-1",
      sequence: 1,
      quantity: "40",
      price: "99.5",
      commission: "0",
      fees: "0",
      currency: "USD",
      source: "CONTROLLED_TEST",
      journal_entry_id: "je-12345678",
      reservation_event_id: 7,
      filled_at: "2026-11-04T21:03:00Z",
    },
  ],
  reservation: {
    reservation_id: "res-1",
    role: "CONSUMED_BY_FILLS",
    status: "PARTIALLY_CONSUMED",
    execution_link: "ORDER_ATTACHED",
    order_ref: "ord-1",
    approved_quantity: "92",
    consumed_quantity: "40",
    released_quantity: "0",
    remaining_quantity: "52",
    remaining_risk: "282.3",
    remaining_exposure: "5193.48",
    expires_at: "2026-11-04T21:05:00Z",
  },
  execution_request: null,
  paper_broker: {
    simulated: true,
    book: {
      broker_order_id: "bk-1",
      status: "PARTIALLY_FILLED",
      quantity: "92",
      filled_quantity: "40",
      last_event_sequence: 2,
      received_at: "2026-11-04T21:02:00Z",
    },
    events: [],
  },
  outbox: [],
  exit_order_ids: [],
  execution: {
    execution_model_id: 1,
    arrival_price: "99.525",
    triggered_at: null,
    trigger_price: null,
    trailing_stop_price: null,
    trail_reference_price: null,
    evaluations: 3,
    last_evaluated_at: "2026-11-04T21:05:00Z",
    last_outcome: "NO_FILL",
    last_code: "LIMIT_NOT_THROUGH",
    attempts: [
      {
        id: 2,
        evaluated_at: "2026-11-04T21:05:00Z",
        outcome: "NO_FILL",
        code: "LIMIT_NOT_THROUGH",
        message: "no trade strictly through the limit 99.6 (3 trades observed)",
        triggered: false,
        fill_event_id: null,
        market: {},
      },
    ],
    fill_details: [],
  },
};

const SUMMARY: OrdersSummary = {
  notice: "Simulated",
  simulated: true,
  account_id: "acct-1",
  counts: {
    APPROVED: 0,
    OPEN: 0,
    PARTIALLY_FILLED: 1,
    FILLED: 2,
    CANCEL_PENDING: 0,
    CANCELLED: 1,
    EXPIRED: 0,
    REJECTED: 1,
    FAILED: 0,
  },
  open: 1,
  refused_requests: 1,
};

const BROKER: PaperBrokerStatus = {
  notice: "Simulated paper broker.",
  broker_id: "atlas-paper-broker",
  simulated: true,
  status: "RUNNING",
  accepting_orders: true,
  fill_model: "SIMULATED from stored market data (versions: execution_models)",
  working_orders: 1,
  last_heartbeat_at: "2026-11-04T21:05:00Z",
  last_recovery_at: "2026-11-04T20:00:00Z",
  outbox: { counts: { PENDING: 0, DELIVERED: 9, DEAD: 0 }, oldest_pending_at: null },
  recent_events: [],
};

const REFUSED: ExecutionRequest[] = [
  {
    id: "req-9",
    idempotency_key: "llm-attempt-0001",
    purpose: "ENTRY",
    decision: "REFUSED",
    reservation_id: "res-2",
    reasons: [{ code: "UNAUTHORIZED_SUBMITTER", message: "strategy:momentum may not submit paper orders" }],
    requested_by: "strategy:momentum",
    requested_at: "2026-11-04T21:04:00Z",
  },
];

function routes(extra: Record<string, unknown> = {}) {
  return {
    ...LAYOUT_ROUTES,
    "/api/v1/portfolio/accounts": { body: [ACCOUNT] },
    "/api/v1/paper-orders/summary": { body: SUMMARY },
    "/api/v1/paper-orders": (url: URL) => ({
      body: url.searchParams.get("group") === "cancelled" ? [order({ id: "ord-2", status: "CANCELLED" })] : [order()],
    }),
    "/api/v1/paper-orders/ord-1": { body: DETAIL },
    "/api/v1/paper-orders/events": { body: EVENTS },
    "/api/v1/paper-orders/requests": { body: REFUSED },
    "/api/v1/paper-broker/status": { body: BROKER },
    ...extra,
  };
}

function renderOrders() {
  return render(
    <DisplayZoneProvider>
      <MemoryRouter initialEntries={["/paper-orders"]}>
        <App pollIntervalMs={60_000} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

beforeEach(() => {
  installFakeLive();
});

describe("Paper Orders", () => {
  it("states that every order is simulated and offers no trading control", async () => {
    const stub = stubApi(routes() as never);
    renderOrders();
    expect(await screen.findByText(/Simulated orders only\./)).toBeInTheDocument();
    expect(screen.getByText(/No brokerage is connected/)).toBeInTheDocument();
    await screen.findByRole("table", { name: "Paper orders" });
    for (const name of [/submit/i, /cancel order/i, /buy/i, /sell/i, /live/i, /execute/i]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(stub.calls.every((c) => (c.init?.method ?? "GET") === "GET")).toBe(true);
  });

  it("shows the paper broker, counts, open orders and their fills", async () => {
    stubApi(routes() as never);
    renderOrders();
    const table = await screen.findByRole("table", { name: "Paper orders" });
    expect(within(table).getByText("PXA")).toBeInTheDocument();
    expect(within(table).getByText("Partially filled")).toBeInTheDocument();
    expect(within(table).getByText("40 / 52")).toBeInTheDocument();
    expect(within(table).getByText("limit 99.60")).toBeInTheDocument();
    expect(screen.getByText("atlas-paper-broker")).toBeInTheDocument();
    expect(screen.getByText("SIMULATED from stored market data (versions: execution_models)")).toBeInTheDocument();
    expect(screen.getByText("Refused requests")).toBeInTheDocument();

    fireEvent.click(within(table).getByText("PXA"));
    const events = await screen.findByRole("table", { name: "Order events" });
    expect(within(events).getByText("accepted by the simulated paper broker")).toBeInTheDocument();
    expect(within(events).getByText("Approved → Open")).toBeInTheDocument();
    const fills = screen.getByRole("table", { name: "Fills" });
    expect(within(fills).getByText("Controlled test")).toBeInTheDocument();
    expect(screen.getByText("92 / 40 / 0")).toBeInTheDocument();
    expect(screen.getByText("consumed by this order's fills")).toBeInTheDocument();
    const evaluations = screen.getByRole("table", { name: "Simulator evaluations" });
    expect(within(evaluations).getByText(/no trade strictly through the limit/)).toBeInTheDocument();
  });

  it("filters cancelled orders and lists refused submissions", async () => {
    stubApi(routes() as never);
    renderOrders();
    await screen.findByRole("table", { name: "Paper orders" });
    fireEvent.click(screen.getByRole("radio", { name: "Cancelled" }));
    const table = await screen.findByRole("table", { name: "Paper orders" });
    expect(await within(table).findByText("Cancelled")).toBeInTheDocument();
    const refused = await screen.findByRole("table", { name: "Refused submissions" });
    expect(within(refused).getByText("strategy:momentum")).toBeInTheDocument();
    expect(within(refused).getByText("Unauthorized submitter")).toBeInTheDocument();
  });

  it("explains a paper account that does not exist yet", async () => {
    stubApi(routes({ "/api/v1/portfolio/accounts": { body: [] } }) as never);
    renderOrders();
    expect(await screen.findByText("The paper account has not been created yet")).toBeInTheDocument();
  });
});
