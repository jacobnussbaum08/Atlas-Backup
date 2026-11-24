// Mirrors the backend market-data schemas (backend/app/api/v1/market_data*.py,
// backend/app/data_engine/market_overview.py and live_updates.py).
// Decimal values arrive as strings and stay strings until they are displayed or charted.

export type Decimal = string;
export type IsoTime = string;

export type DataSource = "HISTORICAL_REST" | "LIVE_STREAM";
export type DataQualityStatus =
  | "UNCHECKED"
  | "VALID"
  | "STALE"
  | "INCOMPLETE"
  | "MISSING"
  | "INVALID"
  | "QUARANTINED"
  | "RECOVERED"
  | "UNAVAILABLE";
export type BarObservationKind = "HISTORICAL" | "LIVE_INITIAL" | "LIVE_UPDATED" | "LIVE_RUNNING";
export type FeedCoverage = "SINGLE_VENUE" | "CONSOLIDATED" | (string & {});
export type MarketSession = "PRE_MARKET" | "REGULAR" | "AFTER_HOURS" | "CLOSED";
export type Freshness =
  | "LIVE"
  | "DELAYED"
  | "HISTORICAL"
  | "STALE"
  | "MARKET_CLOSED"
  | "NO_DATA";
export type CredentialStatus = "NOT_CONFIGURED" | "CONFIGURED" | "VERIFIED" | "REJECTED";
export type Severity = "INFO" | "WARNING" | "ERROR" | "CRITICAL";

export interface MarketAsset {
  asset_id: string;
  symbol: string;
  name: string | null;
  asset_class: string;
  exchange_code: string | null;
  quote_currency: string;
  status: string;
  provider: string | null;
  provider_symbol: string | null;
}

// ------------------------------------------------------------------ market status
export interface MarketStatus {
  checked_at: IsoTime;
  calendar: string;
  calendar_verified: boolean;
  session: MarketSession;
  is_regular_session: boolean;
  trading_day: string | null;
  closure: string | null;
  early_close: boolean;
  regular_open: IsoTime | null;
  regular_close: IsoTime | null;
  next_regular_open: IsoTime;
  next_regular_close: IsoTime;
  feed: string;
  feed_open: boolean;
  feed_window_start: IsoTime | null;
  feed_window_end: IsoTime | null;
  timezone: string;
}

// ------------------------------------------------------------------ snapshots
export interface SnapshotTrade {
  price: Decimal;
  size: Decimal | null;
  event_time: IsoTime;
  received_at: IsoTime;
  source: DataSource;
  quality_status: DataQualityStatus;
  exchange: string | null;
  conditions: string[];
}

export interface SnapshotQuote {
  bid_price: Decimal | null;
  ask_price: Decimal | null;
  bid_size: Decimal | null;
  ask_size: Decimal | null;
  event_time: IsoTime;
  received_at: IsoTime;
  source: DataSource;
  quality_status: DataQualityStatus;
}

export interface SnapshotBar {
  timeframe: string;
  event_time: IsoTime;
  open: Decimal;
  high: Decimal;
  low: Decimal;
  close: Decimal;
  volume: Decimal | null;
  is_complete: boolean;
  observation_kind: BarObservationKind;
  source: DataSource;
  revision: number;
  quality_status: DataQualityStatus;
}

export interface LastPrice {
  value: Decimal;
  basis: "TRADE" | "BAR_CLOSE";
  event_time: IsoTime;
  source: DataSource;
}

export interface PreviousClose {
  value: Decimal;
  session_date: string;
  bar_event_time: IsoTime;
  source: DataSource;
  revision: number;
  basis: string;
}

export interface AssetSnapshot {
  asset: MarketAsset;
  provider: string;
  feed: string;
  feed_coverage: FeedCoverage;
  feed_delay_seconds: number;
  subscribed: boolean;
  last_price: LastPrice | null;
  previous_close: PreviousClose | null;
  previous_close_note: string | null;
  change: Decimal | null;
  change_percent: Decimal | null;
  latest_trade: SnapshotTrade | null;
  latest_quote: SnapshotQuote | null;
  latest_bar: SnapshotBar | null;
  latest_event_at: IsoTime | null;
  freshness: Freshness;
  freshness_threshold_seconds: number;
  quality_status: DataQualityStatus | null;
  quality_events_24h: Record<string, number>;
  open_gaps: number;
}

export interface SnapshotsResponse {
  provider: string;
  feed: string;
  feed_coverage: FeedCoverage;
  market: MarketStatus;
  snapshots: AssetSnapshot[];
  checked_at: IsoTime;
  notes: string[];
}

// ------------------------------------------------------------------ candles
export interface Candle {
  event_time: IsoTime;
  open: Decimal;
  high: Decimal;
  low: Decimal;
  close: Decimal;
  volume: Decimal | null;
  vwap: Decimal | null;
  trade_count: number | null;
  is_complete: boolean;
  quality_status: DataQualityStatus;
  revision: number;
  provider_symbol: string;
  source: DataSource;
  observation_kind: BarObservationKind;
  received_at: IsoTime;
  available_at: IsoTime;
  original_available_at: IsoTime | null;
  processed_at: IsoTime;
  ingested_at: IsoTime;
  ingestion_job_id: string | null;
  raw_event_id: number | null;
}

export interface CandlesResponse {
  asset: MarketAsset;
  timeframe: string;
  provider: string;
  feed: string;
  feed_coverage: FeedCoverage;
  adjustment: string;
  quality: DataQualityStatus[];
  known_at: IsoTime | null;
  candles: Candle[];
  next_after: IsoTime | null;
  notes: string[];
}

// ------------------------------------------------------------------ quotes and trades
interface LiveRow {
  id: number;
  provider_symbol: string;
  event_time: IsoTime;
  event_time_ns: number;
  conditions: string[];
  tape: string | null;
  source: DataSource;
  quality_status: DataQualityStatus;
  received_at: IsoTime;
  available_at: IsoTime;
}

export interface Quote extends LiveRow {
  bid_price: Decimal | null;
  ask_price: Decimal | null;
  bid_size: Decimal | null;
  ask_size: Decimal | null;
  bid_exchange: string | null;
  ask_exchange: string | null;
}

export interface Trade extends LiveRow {
  price: Decimal;
  size: Decimal | null;
  provider_trade_id: string | null;
  exchange: string | null;
}

export interface TradesResponse {
  asset: MarketAsset;
  provider: string;
  feed: string;
  feed_coverage: FeedCoverage;
  trades: Trade[];
  next_after_id: number | null;
  notes: string[];
}

export interface QuotesResponse {
  asset: MarketAsset;
  provider: string;
  feed: string;
  feed_coverage: FeedCoverage;
  quotes: Quote[];
  next_after_id: number | null;
  notes: string[];
}

// ------------------------------------------------------------------ stream and health
export interface StreamStatus {
  provider: string;
  feed: string;
  feed_coverage: FeedCoverage;
  enabled: boolean;
  health: string;
  reasons: string[];
  market_session: MarketSession;
  calendar: string;
  connection_state: string | null;
  auth_state: string | null;
  subscription_state: string | null;
  subscribed_symbols: string[];
  active_channels: string[];
  worker_id: string | null;
  worker_started_at: IsoTime | null;
  worker_stopped_at: IsoTime | null;
  heartbeat_at: IsoTime | null;
  last_authenticated_at: IsoTime | null;
  last_subscribed_at: IsoTime | null;
  last_message_at: IsoTime | null;
  last_persisted_at: IsoTime | null;
  last_error: string | null;
  last_error_at: IsoTime | null;
  recent_errors: Record<string, unknown>[];
  reconnect_count: number;
  queue_depth: number;
  queue_capacity: number;
  counters: Record<string, number>;
  open_gaps: number;
  checked_at: IsoTime;
  notes: string[];
}

export type DimensionState = "OK" | "WARNING" | "FAILING" | "INACTIVE" | "UNKNOWN";
export type PipelineHealthState = "HEALTHY" | "DEGRADED" | "FAILING" | "INACTIVE";

export interface Dimension {
  name: string;
  state: DimensionState;
  detail: string;
  essential: boolean;
  last_success_at: IsoTime | null;
  updated_at: IsoTime | null;
}

export interface PipelineHealth {
  provider: string;
  feed: string;
  pipeline_health: PipelineHealthState;
  stream_health: string;
  dimensions: Dimension[];
  last_message_at: IsoTime | null;
  last_persisted_at: IsoTime | null;
  last_successful_backfill_at: IsoTime | null;
  outstanding_gaps: Record<string, number>;
  pending_recovery_jobs: number;
  failed_recovery_jobs: number;
  recent_provider_errors: Record<string, unknown>[];
  checked_at: IsoTime;
}

export interface StreamGap {
  id: number;
  kind: string;
  gap_start: IsoTime;
  gap_end: IsoTime | null;
  last_message_before: IsoTime | null;
  reason: string;
  symbols: string[];
  channels: string[];
  frames_dropped: number;
  recovery_status: string;
  detected_at: IsoTime;
  closed_at: IsoTime | null;
}

export type GapStatus = "OPEN" | "RECOVERING" | "RECOVERED" | "PARTIALLY_RECOVERED" | "UNAVAILABLE";

export interface DataGap {
  id: number;
  asset_id: string;
  provider_symbol: string;
  provider: string;
  feed: string;
  data_type: "BAR" | "TRADE" | "QUOTE";
  timeframe: string | null;
  gap_start: IsoTime;
  gap_end: IsoTime;
  cause: string;
  status: GapStatus;
  stream_gap_id: number | null;
  parent_gap_id: number | null;
  detected_at: IsoTime;
  resolved_at: IsoTime | null;
  attempts: number;
  records_retrieved: number;
  records_recovered: number;
  records_duplicate: number;
  records_revised: number;
  records_rejected: number;
  resolution: string | null;
  details: Record<string, unknown>;
}

export type RecoveryJobStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "PARTIALLY_COMPLETED"
  | "FAILED";

export interface RecoveryJob {
  id: string;
  kind: "RECOVERY" | "RECONCILIATION";
  gap_id: number | null;
  provider_symbol: string;
  feed: string;
  data_type: "BAR" | "TRADE" | "QUOTE";
  timeframe: string | null;
  missing_start: IsoTime;
  missing_end: IsoTime;
  requested_start: IsoTime;
  requested_end: IsoTime;
  status: RecoveryJobStatus;
  attempts: number;
  max_attempts: number;
  next_attempt_at: IsoTime;
  ingestion_job_ids: string[];
  records_retrieved: number;
  records_recovered: number;
  records_duplicate: number;
  records_revised: number;
  records_rejected: number;
  remaining: Record<string, unknown>[];
  error: string | null;
  created_at: IsoTime;
  started_at: IsoTime | null;
  completed_at: IsoTime | null;
}

export interface IntervalOut {
  start: IsoTime;
  end: IsoTime;
}

export interface CompletenessResponse {
  symbol: string;
  data_type: string;
  timeframe: string | null;
  start: IsoTime;
  end: IsoTime;
  status: "COMPLETE" | "INCOMPLETE" | "MISSING" | "NOT_EXPECTED";
  expected: IntervalOut[];
  covered: IntervalOut[];
  missing: IntervalOut[];
  unverified: IntervalOut[];
  known_gaps: DataGap[];
  notes: string[];
}

export interface QualityEvent {
  id: number;
  event_type: string;
  severity: Severity;
  provider: string;
  feed: string | null;
  asset_id: string | null;
  provider_symbol: string | null;
  timeframe: string | null;
  interval_start: IsoTime | null;
  interval_end: IsoTime | null;
  detected_at: IsoTime;
  resulting_status: DataQualityStatus | null;
  message: string;
  details: Record<string, unknown>;
}

export interface AuditEvent {
  id: number;
  occurred_at: IsoTime;
  actor_type: string;
  actor_id: string;
  action: string;
  priority: "NORMAL" | "HIGH" | null;
  entity_type: string | null;
  entity_id: string | null;
  reason: string | null;
  details: Record<string, unknown>;
}

export interface StreamMetrics {
  provider: string;
  feed: string;
  collected_at: IsoTime;
  ingestion: {
    persisted: Record<string, number>;
    rejected: number;
    quarantined: number;
    stale: number;
    duplicates: Record<string, number>;
    out_of_order: number;
    sequence_gaps: number;
    note: string;
  };
  reliability: Record<string, number>;
  completeness: {
    gaps: Record<string, Record<string, number>>;
    recovery_jobs: Record<string, number>;
    records_recovered: Record<string, number>;
    ingestion_jobs: Record<string, number>;
  };
}

// ------------------------------------------------------------------ ingestion
export type IngestionJobStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "PARTIALLY_COMPLETED"
  | "FAILED"
  | "CANCELLED";

export interface IngestionCounters {
  pages_retrieved: number;
  records_retrieved: number;
  records_validated: number;
  records_stored: number;
  records_duplicate: number;
  records_revised: number;
  records_rejected: number;
  records_quarantined: number;
}

export interface IngestionSymbolResult {
  provider_symbol: string;
  status: string;
  counters: IngestionCounters;
  error: string | null;
}

export interface IngestionJob {
  job_id: string;
  provider: string;
  feed: string;
  adjustment: string;
  data_type: "BAR" | "TRADE" | "QUOTE";
  provider_symbols: string[];
  timeframe: string | null;
  range_start: IsoTime;
  range_end: IsoTime;
  status: IngestionJobStatus;
  counters: IngestionCounters;
  symbols: IngestionSymbolResult[];
  error: string | null;
  requested_by: string | null;
  created_at: IsoTime;
  started_at: IsoTime | null;
  completed_at: IsoTime | null;
}

export interface HistoricalFetchRequest {
  symbols: string[];
  timeframe: string;
  start: IsoTime;
  end: IsoTime;
}

// ------------------------------------------------------------------ provider and settings
export interface ProviderInfo {
  provider: string;
  feed: string;
  feed_coverage: FeedCoverage;
  adjustment: string;
  credentials: CredentialStatus;
  configuration: {
    provider: string;
    feed: string;
    credentials: CredentialStatus;
    stream_enabled: boolean;
    issues: { code?: string; severity: Severity; message: string }[];
  };
  capabilities: {
    display_name: string;
    verified_on: string;
    max_page_size: number | null;
    documentation: string[];
  };
}

export interface MarketDataConfiguration {
  environment: string;
  trading_mode: string;
  provider: string;
  provider_display_name: string;
  feed: string;
  feed_display_name: string;
  feed_description: string;
  feed_coverage: FeedCoverage;
  feed_delay_seconds: number;
  adjustment: string;
  credentials: CredentialStatus;
  operator_token_configured: boolean;
  configured_symbols: string[];
  dashboard_timeframes: string[];
  historical_timeframes: Record<string, number[]>;
  stream_bar_timeframes: string[];
  stale_observation_seconds: number;
  stream: {
    enabled_in_api: boolean;
    enabled_in_worker: boolean | null;
    channels: string[];
    stale_after_seconds: number;
    heartbeat_timeout_seconds: number;
    queue_size: number;
  };
  recovery: {
    enabled: boolean;
    interval_seconds: number;
    timeframes: string[];
    data_types: string[];
    lookback_days: number;
    settle_seconds: number;
    max_attempts: number;
    trade_reconciliation_minutes: number;
  };
  calendar_verified_years: number[];
  limitations: string[];
}

// ------------------------------------------------------------------ live updates (SSE)
export interface LiveBar {
  id: number;
  symbol: string;
  timeframe: string;
  event_time: IsoTime;
  open: Decimal;
  high: Decimal;
  low: Decimal;
  close: Decimal;
  volume: Decimal | null;
  vwap: Decimal | null;
  trade_count: number | null;
  is_complete: boolean;
  quality_status: DataQualityStatus;
  revision: number;
  source: DataSource;
  observation_kind: BarObservationKind;
  ingested_at: IsoTime;
}

export interface LiveTrade {
  id: number;
  symbol: string;
  price: Decimal;
  size: Decimal | null;
  event_time: IsoTime;
  received_at: IsoTime;
  source: DataSource;
  quality_status: DataQualityStatus;
  arrived: number;
}

export interface LiveQuote {
  id: number;
  symbol: string;
  bid_price: Decimal | null;
  ask_price: Decimal | null;
  bid_size: Decimal | null;
  ask_size: Decimal | null;
  event_time: IsoTime;
  received_at: IsoTime;
  source: DataSource;
  quality_status: DataQualityStatus;
  arrived: number;
}

export interface LiveHello {
  provider: string;
  feed: string;
  symbols: string[];
  server_time: IsoTime;
  poll_interval_seconds: number;
}
