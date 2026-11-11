/**
 * Phases 7A-7B simulated paper orders, positions and execution analytics (shapes of
 * /api/v1/paper-orders, /paper-broker, /paper-positions and /paper-execution). Every order,
 * fill and position is simulated by the isolated Atlas paper broker; none is a brokerage one.
 */
export type Decimal = string;

export type OrderStatus =
  | "PROPOSED"
  | "APPROVED"
  | "OPEN"
  | "PARTIALLY_FILLED"
  | "FILLED"
  | "CANCEL_PENDING"
  | "CANCELLED"
  | "EXPIRED"
  | "REJECTED"
  | "FAILED";

export type OrderType = "MARKET" | "LIMIT" | "STOP" | "STOP_LIMIT" | "TAKE_PROFIT" | "TRAILING_STOP";
export type OrderGroup =
  | "open"
  | "closed"
  | "filled"
  | "partially_filled"
  | "cancelled"
  | "rejected"
  | "expired"
  | "failed";

export interface PaperOrder {
  id: string;
  simulated: true;
  account_id: string;
  asset_id: string;
  symbol: string;
  asset_class: string;
  currency: string;
  purpose: "ENTRY" | "EXIT";
  side: "BUY" | "SELL" | "SELL_SHORT" | "BUY_TO_COVER";
  direction: "LONG" | "SHORT";
  order_type: OrderType;
  status: OrderStatus;
  quantity: Decimal;
  filled_quantity: Decimal;
  remaining_quantity: Decimal;
  average_fill_price: Decimal | null;
  limit_price: Decimal | null;
  stop_price: Decimal | null;
  trail_amount: Decimal | null;
  trail_percent: Decimal | null;
  protection_price: Decimal | null;
  quantity_increment: Decimal;
  price_increment: Decimal | null;
  reservation_id: string;
  risk_assessment_id: string;
  parent_order_id: string | null;
  strategy_proposal_id: string | null;
  ai_decision_id: string | null;
  broker_order_id: string | null;
  status_reason: string | null;
  exit_reason: string | null;
  requested_by: string;
  submitted_at: string;
  expires_at: string;
  closed_at: string | null;
  updated_at: string;
}

export interface OrderEvent {
  id: number;
  order_id: string;
  symbol: string | null;
  sequence: number;
  event_type: string;
  from_status: OrderStatus | null;
  to_status: OrderStatus;
  quantity: Decimal;
  price: Decimal | null;
  source: string;
  actor: string;
  reason: string;
  broker_event_id: string | null;
  broker_sequence: number | null;
  occurred_at: string;
  details: Record<string, unknown>;
}

export interface OrderFill {
  id: string;
  sequence: number;
  quantity: Decimal;
  price: Decimal;
  commission: Decimal;
  fees: Decimal;
  currency: string;
  source: string;
  journal_entry_id: string;
  reservation_event_id: number | null;
  filled_at: string;
}

export interface ReservationLink {
  reservation_id: string;
  role: "CONSUMED_BY_FILLS" | "PARENT_LINK_ONLY";
  status: string;
  execution_link: string;
  order_ref: string | null;
  approved_quantity: Decimal;
  consumed_quantity: Decimal;
  released_quantity: Decimal;
  remaining_quantity: Decimal;
  remaining_risk: Decimal;
  remaining_exposure: Decimal;
  expires_at: string;
}

export interface PaperOrderDetail {
  notice: string;
  order: PaperOrder;
  events: OrderEvent[];
  fills: OrderFill[];
  reservation: ReservationLink;
  execution_request: {
    id: string;
    idempotency_key: string;
    decision: string;
    requested_by: string;
    requested_at: string;
  } | null;
  paper_broker: {
    simulated: true;
    book: {
      broker_order_id: string;
      status: string;
      quantity: Decimal;
      filled_quantity: Decimal;
      last_event_sequence: number;
      received_at: string;
    } | null;
    events: {
      id: string;
      sequence: number;
      event_type: string;
      source: string;
      quantity: Decimal;
      price: Decimal | null;
      reason: string;
      occurred_at: string;
      applied: boolean;
    }[];
  };
  outbox: {
    id: number;
    topic: string;
    status: string;
    attempts: number;
    last_error: string | null;
    created_at: string;
    delivered_at: string | null;
  }[];
  exit_order_ids: string[];
  execution: OrderExecution;
}

export interface FillDetail {
  basis: "QUOTE" | "TRADE_THROUGH" | "BAR_THROUGH" | "BAR_FALLBACK";
  execution_model_id: number;
  reference_price: Decimal | null;
  touch_price: Decimal | null;
  arrival_price: Decimal | null;
  spread: Decimal | null;
  slippage_bps: Decimal | null;
  impact_bps: Decimal | null;
  shortfall_bps: Decimal | null;
  liquidity: Decimal | null;
  participation: Decimal | null;
  commission: Decimal;
  regulatory_fees: Decimal;
  exchange_fees: Decimal;
  gap: boolean;
  observations: Record<string, unknown>;
  assumptions: string[];
}

export interface ExecutionAttempt {
  id: number;
  evaluated_at: string;
  outcome: "FILLED" | "PARTIALLY_FILLED" | "TRIGGERED" | "NO_FILL" | "DEFERRED";
  code: string;
  message: string;
  triggered: boolean;
  fill_event_id: string | null;
  market: Record<string, unknown>;
}

export interface OrderExecution {
  execution_model_id: number | null;
  arrival_price: Decimal | null;
  triggered_at: string | null;
  trigger_price: Decimal | null;
  trailing_stop_price: Decimal | null;
  trail_reference_price: Decimal | null;
  evaluations: number;
  last_evaluated_at: string | null;
  last_outcome: string | null;
  last_code: string | null;
  attempts: ExecutionAttempt[];
  fill_details: (FillDetail & { broker_event_id: string; quantity: Decimal; price: Decimal })[];
}

export interface PaperPosition {
  id: string;
  simulated: true;
  account_id: string;
  asset_id: string;
  symbol: string;
  asset_class: string;
  currency: string;
  direction: "LONG" | "SHORT";
  status: "OPEN" | "CLOSED";
  entry_order_id: string;
  reservation_id: string;
  strategy_proposal_id: string | null;
  ai_decision_id: string | null;
  strategy_id: string | null;
  origin: string;
  quantity_opened: Decimal;
  quantity_closed: Decimal;
  open_quantity: Decimal;
  average_entry_price: Decimal;
  average_exit_price: Decimal | null;
  current_price: Decimal | null;
  current_price_at: string | null;
  current_price_source: string | null;
  unrealized_pnl: Decimal | null;
  realized_pnl: Decimal;
  commissions: Decimal;
  fees: Decimal;
  borrow_accrued: Decimal;
  borrow_paid: Decimal;
  net_pnl: Decimal;
  net_return: Decimal | null;
  stop_price: Decimal;
  initial_stop_price: Decimal;
  trailing_stop_price: Decimal | null;
  trail_amount: Decimal | null;
  trail_percent: Decimal | null;
  target_price: Decimal | null;
  invalidation_price: Decimal | null;
  expires_at: string | null;
  trigger_reason: string | null;
  triggered_at: string | null;
  exit_reason: string | null;
  monitor_status: string;
  opened_at: string;
  closed_at: string | null;
  holding_seconds: number | null;
  updated_at: string;
}

export interface PositionEvent {
  sequence: number;
  event_type: string;
  occurred_at: string;
  actor: string;
  reason: string;
  quantity: Decimal;
  price: Decimal | null;
  open_quantity_after: Decimal;
  order_id: string | null;
  journal_entry_id: string | null;
  details: Record<string, unknown>;
}

export interface PositionFill {
  id: string;
  order_id: string;
  side: string;
  quantity: Decimal;
  price: Decimal;
  commission: Decimal;
  fees: Decimal;
  source: string;
  journal_entry_id: string;
  filled_at: string;
  detail: FillDetail | null;
}

export interface PaperPositionDetail {
  notice: string;
  position: PaperPosition;
  events: PositionEvent[];
  orders: PaperOrder[];
  fills: PositionFill[];
  close_requests: {
    id: string;
    requested_by: string;
    reason: string;
    status: string;
    requested_at: string;
    resolved_at: string | null;
    outcome: string | null;
  }[];
}

export interface PositionsSummary {
  simulated: true;
  open: number;
  closed: number;
  long: number;
  short: number;
  unrealized_pnl: Decimal;
  realized_pnl: Decimal;
  net_pnl_closed: Decimal;
  winners: number;
  losers: number;
  exit_reasons: Record<string, number>;
  stale: number;
}

export interface Stats {
  count: string;
  mean: Decimal | null;
  median: Decimal | null;
  max: Decimal | null;
  min: Decimal | null;
}

export interface ExecutionAnalytics {
  notice: string;
  simulated: true;
  fills: number;
  orders_with_simulated_fills: number;
  multi_fill_orders: number;
  by_basis: Record<string, number>;
  slippage_bps: Stats;
  slippage_bps_by_side: Record<string, Stats>;
  impact_bps: Stats;
  shortfall_bps: Stats;
  participation: Stats;
  gaps: number;
  fill_rates: Record<
    string,
    { final: number; filled: number; partial: number; unfilled: number; fill_rate: Decimal | null }
  >;
  costs: {
    notional: Decimal;
    commission: Decimal;
    regulatory_fees: Decimal;
    exchange_fees: Decimal;
    borrow_accrued: Decimal;
    total: Decimal;
    cost_bps_of_notional: Decimal | null;
  };
  evaluations: { outcome: string; code: string; count: number }[];
  execution_model_ids: number[];
  current_model: {
    code: string;
    version: number;
    sha256: string;
    description: string;
    rules: string[];
    classes: Record<string, Record<string, unknown>>;
  };
  limitations: string[];
}

export interface OrdersSummary {
  notice: string;
  simulated: true;
  account_id: string;
  counts: Record<Exclude<OrderStatus, "PROPOSED">, number>;
  open: number;
  refused_requests: number;
}

export interface ExecutionRequest {
  id: string;
  idempotency_key: string;
  purpose: string;
  decision: "ORDER_CREATED" | "REFUSED";
  reservation_id: string | null;
  reasons: { code: string; message: string }[];
  requested_by: string;
  requested_at: string;
}

export interface PaperBrokerStatus {
  notice: string;
  broker_id: string;
  simulated: true;
  status: "RUNNING" | "PAUSED" | "STOPPED";
  accepting_orders: boolean;
  fill_model: string;
  working_orders: number;
  last_heartbeat_at: string | null;
  last_recovery_at: string | null;
  outbox: { counts: Record<string, number>; oldest_pending_at: string | null };
  recent_events: {
    id: string;
    client_order_id: string;
    sequence: number;
    event_type: string;
    source: string;
    occurred_at: string;
  }[];
}

/** One execution-model version (GET /paper-execution/models). */
export interface ExecutionModelVersion {
  id: number;
  code: string;
  version: number;
  simulated: boolean;
  sha256: string;
  description: string;
  definition: Record<string, unknown>;
  created_at: string;
  current: boolean;
}
