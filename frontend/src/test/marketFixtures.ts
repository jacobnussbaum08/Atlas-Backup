// MOCK API responses for tests only, shaped like the backend's market-data schemas.
// Values are invented test data, not market data.
import { setLiveClient, LiveUpdateClient } from "../services/liveUpdates";
import type {
  AssetSnapshot,
  Candle,
  CandlesResponse,
  CompletenessResponse,
  IngestionJob,
  MarketDataConfiguration,
  MarketStatus,
  PipelineHealth,
  SnapshotsResponse,
  StreamMetrics,
  StreamStatus,
} from "../types/marketData";
import { HEALTH_FIXTURE, READY_FIXTURE } from "./fixtures";

export const MOCK_NOW = "2026-03-10T15:00:00Z";

export const MARKET_STATUS: MarketStatus = {
  checked_at: MOCK_NOW,
  calendar: "NYSE (mock)",
  calendar_verified: true,
  session: "REGULAR",
  is_regular_session: true,
  trading_day: "2026-03-10",
  closure: null,
  early_close: false,
  regular_open: "2026-03-10T13:30:00Z",
  regular_close: "2026-03-10T20:00:00Z",
  next_regular_open: "2026-03-11T13:30:00Z",
  next_regular_close: "2026-03-10T20:00:00Z",
  feed: "iex",
  feed_open: true,
  feed_window_start: "2026-03-10T12:00:00Z",
  feed_window_end: "2026-03-10T21:00:00Z",
  timezone: "America/New_York",
};

function asset(symbol: string, name: string, cls = "EQUITY") {
  return {
    asset_id: `00000000-0000-0000-0000-${symbol === "AAPL" ? "000000000001" : "000000000002"}`,
    symbol,
    name,
    asset_class: cls,
    exchange_code: symbol === "AAPL" ? "XNAS" : "ARCX",
    quote_currency: "USD",
    status: "ACTIVE",
    provider: "alpaca",
    provider_symbol: symbol,
  };
}

export const AAPL_SNAPSHOT: AssetSnapshot = {
  asset: asset("AAPL", "Apple Inc."),
  provider: "alpaca",
  feed: "iex",
  feed_coverage: "SINGLE_VENUE",
  feed_delay_seconds: 0,
  subscribed: true,
  last_price: { value: "101.50", basis: "TRADE", event_time: "2026-03-10T14:59:30Z", source: "LIVE_STREAM" },
  previous_close: {
    value: "100.00",
    session_date: "2026-03-09",
    bar_event_time: "2026-03-09T04:00:00Z",
    source: "HISTORICAL_REST",
    revision: 0,
    basis: "close of the provider's iex daily bar for 2026-03-09 (single venue)",
  },
  previous_close_note: "close of the provider's iex daily bar for 2026-03-09 (single venue)",
  change: "1.50",
  change_percent: "1.5000",
  latest_trade: {
    price: "101.50",
    size: "5",
    event_time: "2026-03-10T14:59:30Z",
    received_at: "2026-03-10T14:59:30Z",
    source: "LIVE_STREAM",
    quality_status: "VALID",
    exchange: "V",
    conditions: [],
  },
  latest_quote: {
    bid_price: "101.45",
    ask_price: "0",
    bid_size: "1",
    ask_size: "0",
    event_time: "2026-03-10T14:59:45Z",
    received_at: "2026-03-10T14:59:45Z",
    source: "LIVE_STREAM",
    quality_status: "VALID",
  },
  latest_bar: null,
  latest_event_at: "2026-03-10T14:59:45Z",
  freshness: "LIVE",
  freshness_threshold_seconds: 180,
  quality_status: "VALID",
  quality_events_24h: { WARNING: 2, INFO: 5 },
  open_gaps: 1,
};

export const SPY_SNAPSHOT: AssetSnapshot = {
  ...AAPL_SNAPSHOT,
  asset: asset("SPY", "SPDR S&P 500 ETF Trust", "ETF"),
  subscribed: false,
  last_price: null,
  previous_close: null,
  previous_close_note: "no stored trades or 1-minute bars",
  change: null,
  change_percent: null,
  latest_trade: null,
  latest_quote: null,
  latest_event_at: null,
  freshness: "NO_DATA",
  quality_status: null,
  quality_events_24h: {},
  open_gaps: 0,
};

export function snapshots(rows: AssetSnapshot[] = [AAPL_SNAPSHOT, SPY_SNAPSHOT]): SnapshotsResponse {
  return {
    provider: "alpaca",
    feed: "iex",
    feed_coverage: "SINGLE_VENUE",
    market: MARKET_STATUS,
    snapshots: rows,
    checked_at: MOCK_NOW,
    notes: ["Stored data only (mock)."],
  };
}

export const STREAM_STATUS: StreamStatus = {
  provider: "alpaca",
  feed: "iex",
  feed_coverage: "SINGLE_VENUE",
  enabled: true,
  health: "HEALTHY",
  reasons: [],
  market_session: "REGULAR",
  calendar: "NYSE",
  connection_state: "SUBSCRIBED",
  auth_state: "AUTHENTICATED",
  subscription_state: "ACTIVE",
  subscribed_symbols: ["AAPL", "SPY"],
  active_channels: ["bars", "trades", "quotes"],
  worker_id: "w",
  worker_started_at: MOCK_NOW,
  worker_stopped_at: null,
  heartbeat_at: MOCK_NOW,
  last_authenticated_at: MOCK_NOW,
  last_subscribed_at: MOCK_NOW,
  last_message_at: MOCK_NOW,
  last_persisted_at: MOCK_NOW,
  last_error: null,
  last_error_at: null,
  recent_errors: [],
  reconnect_count: 0,
  queue_depth: 0,
  queue_capacity: 10000,
  counters: {},
  open_gaps: 0,
  checked_at: MOCK_NOW,
  notes: [],
};

const dim = (name: string, state: PipelineHealth["dimensions"][number]["state"], detail = "ok", essential = true) => ({
  name,
  state,
  detail,
  essential,
  last_success_at: MOCK_NOW,
  updated_at: MOCK_NOW,
});

export const PIPELINE_HEALTH: PipelineHealth = {
  provider: "alpaca",
  feed: "iex",
  pipeline_health: "FAILING",
  stream_health: "DEGRADED",
  dimensions: [
    dim("provider_connection", "OK", "connected (DEGRADED)"),
    dim("authentication", "OK", "auth state AUTHENTICATED"),
    dim("subscription", "OK"),
    dim("message_reception", "OK"),
    dim("message_processing", "OK"),
    dim("data_validation", "OK"),
    dim("database_persistence", "FAILING", "writes failing since 2026-03-10T14:58:00Z"),
    dim("historical_completeness", "WARNING", "4 outstanding gap(s)", false),
    dim("backfill_activity", "INACTIVE", "recovery worker disabled or never ran", false),
  ],
  last_message_at: MOCK_NOW,
  last_persisted_at: "2026-03-10T14:58:00Z",
  last_successful_backfill_at: null,
  outstanding_gaps: { BAR: 1, QUOTE: 3 },
  pending_recovery_jobs: 2,
  failed_recovery_jobs: 0,
  recent_provider_errors: [],
  checked_at: MOCK_NOW,
};

export const METRICS: StreamMetrics = {
  provider: "alpaca",
  feed: "iex",
  collected_at: MOCK_NOW,
  ingestion: {
    persisted: { bars: 10, quotes: 100, trades: 50 },
    rejected: 1,
    quarantined: 2,
    stale: 0,
    duplicates: { bars: 1, quotes: 2, trades: 3, replayed_frames_suppressed: 1 },
    out_of_order: 0,
    sequence_gaps: 0,
    note: "mock",
  },
  reliability: {
    disconnections: 1,
    worker_not_running_intervals: 2,
    database_interruptions: 1,
    data_not_persisted_intervals: 0,
  },
  completeness: {
    gaps: {},
    recovery_jobs: {},
    records_recovered: {},
    ingestion_jobs: { COMPLETED: 3, FAILED: 1 },
  },
};

export function candle(eventTime: string, close: string, extra: Partial<Candle> = {}): Candle {
  return {
    event_time: eventTime,
    open: "100.00",
    high: "102.00",
    low: "99.00",
    close,
    volume: "1000",
    vwap: null,
    trade_count: 10,
    is_complete: true,
    quality_status: "VALID",
    revision: 0,
    provider_symbol: "AAPL",
    source: "HISTORICAL_REST",
    observation_kind: "HISTORICAL",
    received_at: eventTime,
    available_at: eventTime,
    original_available_at: null,
    processed_at: eventTime,
    ingested_at: eventTime,
    ingestion_job_id: null,
    raw_event_id: null,
    ...extra,
  };
}

export function candlesResponse(candles: Candle[], timeframe = "1Min"): CandlesResponse {
  return {
    asset: AAPL_SNAPSHOT.asset,
    timeframe,
    provider: "alpaca",
    feed: "iex",
    feed_coverage: "SINGLE_VENUE",
    adjustment: "RAW",
    quality: ["VALID", "INCOMPLETE", "QUARANTINED"],
    known_at: null,
    candles,
    next_after: null,
    notes: ["mock candles"],
  };
}

export const COMPLETENESS: CompletenessResponse = {
  symbol: "AAPL",
  data_type: "BAR",
  timeframe: "1Min",
  start: "2026-03-06T15:00:00Z",
  end: MOCK_NOW,
  status: "INCOMPLETE",
  expected: [],
  covered: [],
  missing: [{ start: "2026-03-10T14:01:00Z", end: "2026-03-10T14:03:00Z" }],
  unverified: [],
  known_gaps: [],
  notes: [],
};

export const CONFIGURATION: MarketDataConfiguration = {
  environment: "development",
  trading_mode: "PAPER",
  provider: "alpaca",
  provider_display_name: "Alpaca Markets",
  feed: "iex",
  feed_display_name: "IEX",
  feed_description: "Investors Exchange only.",
  feed_coverage: "SINGLE_VENUE",
  feed_delay_seconds: 0,
  adjustment: "RAW",
  credentials: "CONFIGURED",
  operator_token_configured: false,
  configured_symbols: ["AAPL", "SPY"],
  dashboard_timeframes: ["1Min", "5Min", "15Min", "1Hour", "1Day"],
  historical_timeframes: { Min: [1, 5, 15], Hour: [1], Day: [1] },
  stream_bar_timeframes: ["1Day", "1Min"],
  stale_observation_seconds: 60,
  stream: {
    enabled_in_api: true,
    enabled_in_worker: true,
    channels: ["bars", "trades", "quotes"],
    stale_after_seconds: 180,
    heartbeat_timeout_seconds: 30,
    queue_size: 10000,
  },
  recovery: {
    enabled: true,
    interval_seconds: 60,
    timeframes: ["1Min", "1Day"],
    data_types: ["BAR", "TRADE"],
    lookback_days: 5,
    settle_seconds: 120,
    max_attempts: 5,
    trade_reconciliation_minutes: 30,
  },
  calendar_verified_years: [2025, 2026, 2027],
  limitations: ["Automatic recovery is not enabled for: quotes"],
};

export function ingestionJob(extra: Partial<IngestionJob> = {}): IngestionJob {
  return {
    job_id: "11111111-2222-3333-4444-555555555555",
    provider: "alpaca",
    feed: "iex",
    adjustment: "RAW",
    data_type: "BAR",
    provider_symbols: ["AAPL"],
    timeframe: "1Min",
    range_start: "2026-03-09T13:30:00Z",
    range_end: "2026-03-09T20:00:00Z",
    status: "COMPLETED",
    counters: {
      pages_retrieved: 2,
      records_retrieved: 390,
      records_validated: 390,
      records_stored: 385,
      records_duplicate: 5,
      records_revised: 0,
      records_rejected: 0,
      records_quarantined: 0,
    },
    symbols: [],
    error: null,
    requested_by: "api",
    created_at: "2026-03-10T14:00:00Z",
    started_at: "2026-03-10T14:00:01Z",
    completed_at: "2026-03-10T14:00:05Z",
    ...extra,
  };
}

type Reply = { status?: number; body: unknown } | "network-error" | "pending";
export type Handler = (url: URL, init?: RequestInit) => Reply;

export interface ApiStub {
  calls: { url: URL; init?: RequestInit }[];
}

/** Fetch stub routed by pathname; unknown paths fail the test loudly. */
export function stubApi(handlers: Record<string, Handler | Reply>): ApiStub {
  const stub: ApiStub = { calls: [] };
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === "string" ? input : input.toString(), "http://localhost");
    stub.calls.push(init ? { url, init } : { url });
    const handler = handlers[url.pathname];
    if (handler === undefined) throw new Error(`Unexpected fetch: ${url.pathname}`);
    const reply = typeof handler === "function" ? handler(url, init) : handler;
    if (reply === "network-error") throw new TypeError("Failed to fetch");
    if (reply === "pending") {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    }
    return new Response(JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  return stub;
}

/** Default answers for the layout's own requests. */
export const LAYOUT_ROUTES: Record<string, Reply> = {
  "/health": { body: HEALTH_FIXTURE },
  "/ready": { body: READY_FIXTURE },
  "/api/v1/market-data/stream": { body: STREAM_STATUS },
  "/api/v1/market-data/market-status": { body: MARKET_STATUS },
};

/** An EventSource stand-in: tests push server events with ``emit``. */
export class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  closed = false;
  private listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  emit(type: string, data?: unknown): void {
    const event = new MessageEvent<string>(type, data === undefined ? {} : { data: JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  /** A transport failure (no data), like a dropped connection. */
  fail(): void {
    for (const listener of this.listeners.get("error") ?? []) listener(new MessageEvent<string>("error"));
  }

  close(): void {
    this.closed = true;
  }

  static open(): FakeEventSource[] {
    return FakeEventSource.instances.filter((s) => !s.closed);
  }
}

/** Install a live client backed by FakeEventSource. */
export function installFakeLive(): void {
  FakeEventSource.instances = [];
  setLiveClient(new LiveUpdateClient((url) => new FakeEventSource(url) as unknown as EventSource));
}
