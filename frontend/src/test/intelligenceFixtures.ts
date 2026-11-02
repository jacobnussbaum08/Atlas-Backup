/**
 * TEST FIXTURES for the Phase 3B market intelligence page: API responses shaped like the
 * backend's, with made-up values. Not market data.
 */
import type {
  AssetIntelligence,
  AssetList,
  Classification,
  Correlations,
  IntelligenceRun,
  MarketConditions,
  RegimeHistory,
  RegimePoint,
} from "../types/intelligence";

const AS_OF = "2026-11-03T22:00:00+00:00";

export const RUN: IntelligenceRun = {
  id: "11111111-2222-3333-4444-555555555555",
  kind: "LIVE",
  status: "COMPLETED",
  basis: "RECORDED",
  provider: "alpaca",
  feed: "iex",
  adjustment: "SPLIT",
  as_of_start: AS_OF,
  as_of_end: AS_OF,
  requested_by: "api",
  params: {},
  summary: {},
  error: null,
  started_at: AS_OF,
  heartbeat_at: AS_OF,
  completed_at: "2026-11-03T22:00:30+00:00",
};

export function classification(over: Partial<Classification> = {}): Classification {
  return {
    dimension: "TREND",
    timeframe: "1Day",
    label: "BULLISH",
    confidence: "0.620000",
    probabilities: { BULLISH: "0.62", BEARISH: "0.08", SIDEWAYS: "0.2", TRANSITIONING: "0.1", UNCERTAIN: "0" },
    quality: "OK",
    algorithm: "trend-evidence-v1",
    as_of: AS_OF,
    basis: "RECORDED",
    available_at: AS_OF,
    inputs_available_at: "2026-11-03T21:30:00+00:00",
    evidence: { direction_score: "0.55", adx: "27.4", trend_strength: "0.86" },
    reasons: [],
    ...over,
  };
}

const VOLATILITY = classification({
  dimension: "VOLATILITY",
  label: "NORMAL",
  confidence: "0.900000",
  algorithm: "volatility-percentile-v1",
  probabilities: { LOW: "0.05", NORMAL: "0.9", ELEVATED: "0.05", EXTREME: "0", UNCERTAIN: "0" },
  evidence: { realized_volatility: "0.142", volatility_percentile: "0.48", range_state: "STEADY" },
});

const STABILITY = classification({
  dimension: "STABILITY",
  label: "STABLE",
  confidence: "0.700000",
  algorithm: "stability-stress-v1",
  probabilities: { STABLE: "0.7", UNSETTLED: "0.2", STRESSED: "0.1", UNCERTAIN: "0" },
  evidence: { stress_score: "0.21", missing_inputs: [] },
});

export const MARKET: MarketConditions = {
  available: true,
  run: RUN,
  as_of: AS_OF,
  benchmark_symbol: "SPY",
  classifications: [
    classification(),
    VOLATILITY,
    STABILITY,
    classification({ timeframe: "1Week", label: "SIDEWAYS", probabilities: { BULLISH: "0.2", BEARISH: "0.1", SIDEWAYS: "0.6", TRANSITIONING: "0.1", UNCERTAIN: "0" } }),
    { ...VOLATILITY, timeframe: "1Week", label: "UNCERTAIN", quality: "INSUFFICIENT_DATA", reasons: ["30 of 39 expected bars; at least 36 needed"] },
  ],
  participation: {
    status: "OK",
    members_total: 82,
    members_evaluated: 79,
    metrics: {
      above_slow_ma: "0.64",
      above_slow_ma_change: "-0.05",
      above_fast_ma: "0.58",
      positive_momentum: "0.61",
      advancers: 47,
      decliners: 30,
      stale_excluded: 2,
      bullish_share: "0.41",
      bearish_share: "0.18",
      new_highs: 6,
      new_lows: 2,
      concentration_top_share: "0.33",
      concentration_top_count: 8,
    },
    cross_asset: {},
    limitations: [
      "Universe participation over 79 of 82 qualified-universe members with usable daily history; not market-wide breadth.",
      "Feed 'iex' is a single venue: prices and volumes are not consolidated US market activity.",
    ],
  },
  notes: ["Market intelligence describes measured conditions. nothing here is a trading signal."],
};

export const EMPTY_MARKET: MarketConditions = {
  available: false,
  run: null,
  as_of: null,
  benchmark_symbol: "SPY",
  classifications: [],
  participation: null,
  notes: [],
};

function point(day: string, trend: string, basis: RegimePoint["basis"]): RegimePoint {
  const at = `${day}T22:00:00+00:00`;
  return {
    as_of: at,
    basis,
    trend: classification({ label: trend, as_of: at, basis }),
    volatility: { ...VOLATILITY, as_of: at, basis },
    stability: { ...STABILITY, as_of: at, basis },
  };
}

export const HISTORY: RegimeHistory = {
  timeframe: "1Day",
  points: [
    point("2026-10-29", "SIDEWAYS", "RECONSTRUCTED"),
    point("2026-10-30", "SIDEWAYS", "RECONSTRUCTED"),
    point("2026-11-02", "BULLISH", "RECONSTRUCTED"),
    point("2026-11-03", "BULLISH", "RECORDED"),
  ],
  notes: [],
};

export function assetItem(over: Partial<AssetIntelligence> = {}): AssetIntelligence {
  return {
    asset_id: "a1",
    symbol: "AAA",
    timeframe: "1Day",
    quality: "OK",
    as_of: AS_OF,
    last_event_at: "2026-11-03T05:00:00+00:00",
    bars_used: 140,
    expected_bars: 140,
    features: { relative_strength: "0.0412", roc: "0.051", realized_volatility: "0.231", adx: "31.2" },
    trend: classification(),
    volatility: VOLATILITY,
    notes: [],
    ...over,
  };
}

export const ASSETS: AssetList = {
  run: RUN,
  timeframe: "1Day",
  sort: "relative_strength",
  items: [
    assetItem(),
    assetItem({
      asset_id: "a2",
      symbol: "NEWX",
      quality: "INSUFFICIENT_DATA",
      features: {},
      trend: classification({ label: "UNCERTAIN", quality: "INSUFFICIENT_DATA" }),
      volatility: null,
      notes: ["60 of 140 expected bars; at least 126 needed"],
    }),
  ],
  notes: ["Relative strength is against the benchmark over the same bars."],
};

export const CORRELATIONS: Correlations = {
  run: RUN,
  as_of: AS_OF,
  window: 60,
  window_start: "2026-08-06T04:00:00+00:00",
  window_end: "2026-11-03T05:00:00+00:00",
  symbols: ["AAA", "BBB", "CCC"],
  cells: [
    { a: "AAA", b: "BBB", status: "OK", observations: 59, correlation: "0.73000000", ci_low: "0.58", ci_high: "0.83", significant: true, prior_correlation: "0.6", change: "0.13" },
    { a: "AAA", b: "CCC", status: "INSUFFICIENT_OVERLAP", observations: 20, correlation: null, ci_low: null, ci_high: null, significant: null, prior_correlation: null, change: null },
    { a: "BBB", b: "CCC", status: "OK", observations: 59, correlation: "-0.21000000", ci_low: "-0.44", ci_high: "0.05", significant: false, prior_correlation: null, change: null },
  ],
  benchmark: [],
  summary: { average_correlation: "0.26", absorption_ratio: "0.41", absorption_assets: 3, pairs_computed: 2, pairs_insufficient: 1 },
  notes: ["Correlation of daily log returns describes co-movement in the window only."],
};
