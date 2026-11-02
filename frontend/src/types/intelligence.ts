/**
 * Phase 3B market intelligence API types (backend: app/api/v1/intelligence.py).
 * Numbers arrive as decimal strings, as stored; probabilities are normalized evidence
 * weights, not calibrated likelihoods.
 */
export type IsoTime = string;
export type Decimal = string;

export type Timeframe = "1Day" | "1Week";
export type Dimension = "TREND" | "VOLATILITY" | "STABILITY";
export type KnowledgeBasis = "RECORDED" | "RECONSTRUCTED";
export type RunKind = "LIVE" | "RECONSTRUCTION";
export type RunStatus = "RUNNING" | "COMPLETED" | "PARTIALLY_COMPLETED" | "FAILED";
export type ClassificationQuality = "OK" | "PARTIAL" | "INSUFFICIENT_DATA";

export const TREND_LABELS = ["BULLISH", "BEARISH", "SIDEWAYS", "TRANSITIONING", "UNCERTAIN"] as const;
export const VOLATILITY_LABELS = ["LOW", "NORMAL", "ELEVATED", "EXTREME", "UNCERTAIN"] as const;
export const STABILITY_LABELS = ["STABLE", "UNSETTLED", "STRESSED", "UNCERTAIN"] as const;

export interface IntelligenceRun {
  id: string;
  kind: RunKind;
  status: RunStatus;
  basis: KnowledgeBasis;
  provider: string;
  feed: string;
  adjustment: string;
  as_of_start: IsoTime;
  as_of_end: IsoTime;
  requested_by: string;
  params: Record<string, unknown>;
  summary: Record<string, unknown>;
  error: string | null;
  started_at: IsoTime;
  heartbeat_at: IsoTime;
  completed_at: IsoTime | null;
}

export interface Classification {
  dimension: Dimension;
  timeframe: Timeframe;
  label: string;
  confidence: Decimal;
  probabilities: Record<string, Decimal | null>;
  quality: ClassificationQuality;
  algorithm: string;
  as_of: IsoTime;
  basis: KnowledgeBasis;
  available_at: IsoTime;
  inputs_available_at: IsoTime | null;
  evidence: Record<string, unknown>;
  reasons: string[];
}

export interface Participation {
  status: "OK" | "INSUFFICIENT_DATA";
  members_total: number;
  members_evaluated: number;
  metrics: Record<string, Decimal | number | string | null>;
  cross_asset: Record<string, unknown>;
  limitations: string[];
}

export interface MarketConditions {
  available: boolean;
  run: IntelligenceRun | null;
  as_of: IsoTime | null;
  benchmark_symbol: string | null;
  classifications: Classification[];
  participation: Participation | null;
  notes: string[];
}

export interface RegimePoint {
  as_of: IsoTime;
  basis: KnowledgeBasis;
  trend: Classification | null;
  volatility: Classification | null;
  stability: Classification | null;
}

export interface RegimeHistory {
  timeframe: Timeframe;
  points: RegimePoint[];
  notes: string[];
}

export interface AssetIntelligence {
  asset_id: string;
  symbol: string;
  timeframe: Timeframe;
  quality: "OK" | "PARTIAL" | "INSUFFICIENT_DATA";
  as_of: IsoTime;
  last_event_at: IsoTime | null;
  bars_used: number;
  expected_bars: number;
  features: Record<string, Decimal | null>;
  trend: Classification | null;
  volatility: Classification | null;
  notes: string[];
}

export type AssetSort = "relative_strength" | "roc" | "realized_volatility" | "adx" | "symbol";

export interface AssetList {
  run: IntelligenceRun | null;
  timeframe: Timeframe;
  sort: AssetSort;
  items: AssetIntelligence[];
  notes: string[];
}

export interface CorrelationCell {
  a: string;
  b: string;
  status: "OK" | "INSUFFICIENT_OVERLAP";
  observations: number;
  correlation: Decimal | null;
  ci_low: Decimal | null;
  ci_high: Decimal | null;
  significant: boolean | null;
  prior_correlation: Decimal | null;
  change: Decimal | null;
}

export interface Correlations {
  run: IntelligenceRun | null;
  as_of: IsoTime | null;
  window: number | null;
  window_start: IsoTime | null;
  window_end: IsoTime | null;
  symbols: string[];
  cells: CorrelationCell[];
  benchmark: {
    symbol: string;
    status: string;
    observations: number;
    correlation: Decimal | null;
    beta: Decimal | null;
    relative_log_return: Decimal | null;
  }[];
  summary: Record<string, unknown>;
  notes: string[];
}
