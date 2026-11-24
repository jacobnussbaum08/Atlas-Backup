// TEST FIXTURES shaped like the Strategy Laboratory API (Phase 4A, extended in 4B).
// Strategies, proposals, evaluations and conflicts are invented test data (FIXTURE
// strategies, MOCK prices); used only by tests.
import type {
  CatalogResponse,
  ConflictsResponse,
  EvaluationsResponse,
  FeatureRegistryResponse,
  ProposalsResponse,
  StrategyDetail,
  StrategySummary,
} from "../types/strategies";

const IMPLEMENTED_NOTE =
  "Evaluation logic implemented (Phase 4B). Proposals are analytical hypotheses; default parameters are not optimized and no performance is claimed.";
const INTERFACE_NOTE =
  "Interface, parameters and feature dependencies only; evaluation logic arrives in Phase 4B. It is never evaluated and cannot be enabled until then.";

function family(
  strategy_id: string,
  name: string,
  familyCode: StrategySummary["family"],
  triggers: StrategySummary["schedule"]["triggers"] = ["BAR_CLOSE"],
): StrategySummary {
  return {
    strategy_id,
    family: familyCode,
    name,
    description: `${name} description (fixture).`,
    implemented: true,
    implementation_note: IMPLEMENTED_NOTE,
    enabled: false,
    status_changed_at: null,
    current_version: {
      id: `${strategy_id}-v1`,
      version: 1,
      logic_version: `${strategy_id}-1`,
      configuration_id: `${strategy_id}-config`,
      reason: "initial registration with default parameters",
      created_at: "2026-11-03T01:30:00Z",
    },
    version_count: 1,
    supported_asset_classes: ["EQUITY", "ETF"],
    supported_timeframes: ["1Day", "1Week"],
    configured_timeframes: ["1Day"],
    schedule: { triggers, description: "after every completed and settled period" },
    required_features: {
      "1Day": [
        { key: "close.v1()@1Day", optional: false },
        { key: "rsi.v1(length=14)@1Day", optional: false },
        { key: "regime_label.v1(dimension=TREND,max_age_hours=96,scope=MARKET)@1Day", optional: true },
      ],
    },
    evaluations: 0,
    last_evaluation_at: null,
    proposals: 0,
    active_proposals: 0,
    parameters: { timeframes: ["1Day"], directions: ["LONG", "SHORT"], atr_length: 14, signal_ttl_periods: 1 },
    signal_states: {},
  };
}

export const FAMILIES: StrategySummary[] = [
  family("breakout", "Breakout", "BREAKOUT"),
  family("event_driven", "Event driven", "EVENT_DRIVEN", ["BAR_CLOSE", "NEWS_EVENT"]),
  family("mean_reversion", "Mean reversion", "MEAN_REVERSION"),
  family("momentum", "Momentum", "MOMENTUM"),
  family("trend_following", "Trend following", "TREND_FOLLOWING"),
  family("volatility", "Volatility", "VOLATILITY"),
];

/** The Phase 4A state: interfaces without logic. */
export const INTERFACE_ONLY_CATALOG: CatalogResponse = {
  strategies: FAMILIES.map((f) => ({
    ...f,
    implemented: false,
    implementation_note: INTERFACE_NOTE,
    current_version: f.current_version && { ...f.current_version, logic_version: `${f.strategy_id}-interface-1` },
  })),
  feature_registry_version: "feature-registry-v1",
  sync_problems: [],
  as_of: "2026-11-03T02:00:00Z",
  notes: [],
};

export const CATALOG: CatalogResponse = {
  strategies: FAMILIES,
  feature_registry_version: "feature-registry-v1",
  sync_problems: [],
  as_of: "2026-11-03T02:00:00Z",
  notes: [
    "Proposals are hypotheses produced by strategy code from point-in-time features. They are not approved trades, carry no position size and were not sent anywhere; signal strength is an uncalibrated score, not a probability.",
  ],
};

const FIXTURE_STRATEGY: StrategySummary = {
  ...family("fixture_proposer", "Fixture proposer", "MOMENTUM"),
  enabled: true,
  status_changed_at: "2026-11-03T01:31:00Z",
  evaluations: 2,
  last_evaluation_at: "2026-11-03T22:30:00Z",
  proposals: 1,
  active_proposals: 1,
  signal_states: { SIGNAL: 1, MISSING_DATA: 1 },
};

export const CATALOG_WITH_FIXTURE: CatalogResponse = {
  ...CATALOG,
  strategies: [...FAMILIES, FIXTURE_STRATEGY],
};

export const EMPTY_PROPOSALS: ProposalsResponse = { proposals: [], as_of: CATALOG.as_of, notes: CATALOG.notes };
export const EMPTY_EVALUATIONS: EvaluationsResponse = { evaluations: [], as_of: CATALOG.as_of };
export const EMPTY_CONFLICTS: ConflictsResponse = { conflicts: [], as_of: CATALOG.as_of, notes: [] };

export const PROPOSALS: ProposalsResponse = {
  as_of: "2026-11-03T22:31:00Z",
  notes: CATALOG.notes,
  proposals: [
    {
      id: "p-1",
      evaluation_id: "e-1",
      strategy_id: "fixture_proposer",
      strategy_version_id: "fixture_proposer-v1",
      version: 1,
      asset_id: "a-1",
      symbol: "FXA",
      market: "EQUITY",
      direction: "LONG",
      timeframe: "1Day",
      mode: "LIVE",
      basis: "RECORDED",
      signal_at: "2026-11-03T22:30:00Z",
      data_cutoff: "2026-11-03T22:30:00Z",
      feature_snapshot_ids: ["s-1"],
      reference_entry: "119.80000000",
      invalidation_level: "113.81000000",
      target_reference: "125.79000000",
      holding_horizon_periods: 5,
      signal_strength: "0.5000",
      strength_kind: "UNCALIBRATED_SCORE",
      evidence: [
        {
          feature_key: "close.v1()@1Day",
          role: "trigger",
          note: "newest completed close",
          value: "119.8",
          available_at: "2026-11-03T21:30:00Z",
        },
      ],
      reasoning: "TEST FIXTURE: close compared with its SMA.",
      expires_at: "2026-11-04T21:00:00Z",
      created_at: "2026-11-03T22:30:00Z",
      status: "ACTIVE",
      evaluation_event: "BAR_CLOSE:1Day:2026-11-03",
      levels: {
        entry_method: "newest completed close",
        invalidation_method: "entry -/+ 2.0 x ATR14",
        target_method: "entry +/- 3.0 x ATR14",
        atr: "2.995",
        risk_per_unit: "5.99",
        reward_per_unit: "5.99",
        reward_risk: "1.00",
        holding_horizon_periods: 5,
        setup: "MOMENTUM",
      },
      conflict_count: 1,
    },
  ],
};

export const CONFLICTS: ConflictsResponse = {
  as_of: "2026-11-03T22:31:00Z",
  notes: [],
  conflicts: [
    {
      id: 7,
      kind: "OPPOSING_DIRECTION",
      asset_id: "a-1",
      symbol: "FXA",
      mode: "LIVE",
      basis: "RECORDED",
      overlap_start: "2026-11-03T22:30:00Z",
      overlap_end: "2026-11-04T21:00:00Z",
      detected_by: "p-2",
      detected_at: "2026-11-03T22:30:05Z",
      same_strategy: false,
      proposals: [
        {
          id: "p-1",
          strategy_id: "fixture_proposer",
          direction: "LONG",
          timeframe: "1Day",
          signal_at: "2026-11-03T22:30:00Z",
          expires_at: "2026-11-04T21:00:00Z",
          reference_entry: "119.80000000",
          signal_strength: "0.5000",
          status: "ACTIVE",
        },
        {
          id: "p-2",
          strategy_id: "fixture_short",
          direction: "SHORT",
          timeframe: "1Day",
          signal_at: "2026-11-03T22:30:05Z",
          expires_at: "2026-11-04T21:00:00Z",
          reference_entry: "119.80000000",
          signal_strength: "0.5000",
          status: "ACTIVE",
        },
      ],
    },
  ],
};

export const EVALUATIONS: EvaluationsResponse = {
  as_of: "2026-11-03T22:31:00Z",
  evaluations: [
    {
      id: "e-2",
      strategy_id: "fixture_proposer",
      strategy_version_id: "fixture_proposer-v1",
      version: 1,
      asset_id: "a-2",
      symbol: "FXM",
      timeframe: "1Day",
      mode: "LIVE",
      basis: "RECORDED",
      trigger: "BAR_CLOSE",
      trigger_ref: "1Day:2026-11-03",
      attempt: 1,
      status: "SKIPPED",
      data_cutoff: "2026-11-03T22:30:00Z",
      known_at: "2026-11-03T22:30:00Z",
      started_at: "2026-11-03T22:30:01Z",
      finished_at: "2026-11-03T22:30:01Z",
      duration_ms: 42,
      proposals_count: 0,
      skip_reasons: [{ feature: "sma.v1(length=5)@1Day", status: "MISSING_INPUTS", reason: "1 of 5 periods" }],
      notes: [],
      worker_id: "fixture-worker",
      logic_version: "fixture-proposer-1",
      configuration_id: "fixture_proposer-config",
      signal_state: "MISSING_DATA",
      signal_reason: "sma.v1(length=5)@1Day: MISSING_INPUTS",
      checks: [],
      eligibility: null,
    },
    {
      id: "e-1",
      strategy_id: "fixture_proposer",
      strategy_version_id: "fixture_proposer-v1",
      version: 1,
      asset_id: "a-1",
      symbol: "FXA",
      timeframe: "1Day",
      mode: "HISTORICAL",
      basis: "RECONSTRUCTED",
      trigger: "MANUAL",
      trigger_ref: "cli",
      attempt: 1,
      status: "PROPOSED",
      data_cutoff: "2026-11-02T22:30:00Z",
      known_at: "2026-11-03T22:30:00Z",
      started_at: "2026-11-03T22:30:00Z",
      finished_at: "2026-11-03T22:30:00Z",
      duration_ms: 35,
      proposals_count: 1,
      skip_reasons: [],
      notes: [],
      worker_id: "fixture-worker",
      logic_version: "fixture-proposer-1",
      configuration_id: "fixture_proposer-config",
      signal_state: "SIGNAL",
      signal_reason: null,
      checks: [
        { name: "ema_confirmation", passed: true, detail: "close 119.8, EMA20 117", direction: "LONG", features: [] },
        { name: "relative_volume", passed: false, detail: "volume ratio 0.9 (threshold 1.2)", direction: "SHORT", features: [] },
        { name: "market_regime", passed: null, detail: "market trend regime unavailable", direction: "LONG", features: [] },
      ],
      eligibility: {
        known: true,
        provider: "alpaca",
        provider_status: "ACTIVE",
        tradable: true,
        shortable: false,
        easy_to_borrow: false,
        reference_version: 3,
        observed_at: "2026-10-01T00:00:00Z",
        long_eligible: true,
        short_eligible: false,
      },
    },
  ],
};

export const FEATURES: FeatureRegistryResponse = {
  registry_version: "feature-registry-v1",
  notes: [],
  definitions: [
    {
      name: "rsi",
      version: "v1",
      source: "BARS",
      description: "Relative strength index (0-100).",
      method: "Wilder (1978): 100 - 100 / (1 + avg_gain / avg_loss)",
      parameters: [{ name: "length", type: "int", default: 14, description: "periods" }],
      timeframes: ["1Day", "1Week"],
      required_data: ["bars.close"],
      availability: "the newest availability among the completed bars of its window",
      value_kind: "NUMBER",
      status: "ACTIVE",
      window_rule: "5 * length + 1",
      definition_sha256: "a".repeat(64),
      registered_at: "2026-11-03T01:30:00Z",
    },
    {
      name: "news_event_count",
      version: "v1",
      source: "NEWS",
      description: "Number of news articles about the asset in the look-back window.",
      method: "articles DIRECTLY related to the asset; UNAVAILABLE unless retrieval covered the window",
      parameters: [],
      timeframes: ["1Day", "1Week"],
      required_data: ["news_events"],
      availability: "the newest available_at among the events used",
      value_kind: "NUMBER",
      status: "ACTIVE",
      window_rule: null,
      definition_sha256: "b".repeat(64),
      registered_at: "2026-11-03T01:30:00Z",
    },
  ],
};

export const MOMENTUM_DETAIL: StrategyDetail = {
  strategy: FAMILIES[3]!,
  parameters: {
    timeframes: ["1Day"],
    directions: ["LONG", "SHORT"],
    lookback_periods: 20,
    min_volume_ratio: "1.2",
    signal_ttl_periods: 1,
  },
  parameters_schema: {
    properties: { lookback_periods: { description: "momentum return periods", minimum: 2, maximum: 260 } },
  },
  versions: [
    {
      id: "momentum-v1",
      strategy_id: "momentum",
      version: 1,
      logic_version: "momentum-1",
      configuration_id: "momentum-config",
      parameters: { lookback_periods: 20 },
      parameters_sha256: "c".repeat(64),
      name: "Momentum",
      description: "Momentum description (fixture).",
      implemented: true,
      supported_asset_classes: ["EQUITY", "ETF"],
      supported_timeframes: ["1Day", "1Week"],
      schedule: { triggers: ["BAR_CLOSE"], description: "after every completed and settled period" },
      required_features: {},
      feature_registry_version: "feature-registry-v1",
      reason: "initial registration with default parameters",
      created_by: "atlas:strategy-catalog",
      created_at: "2026-11-03T01:30:00Z",
    },
  ],
  status_history: [],
};
