import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import type {
  AIOverview,
  DecisionDetail,
  DecisionSummary,
  EvaluationView,
  Rankings,
  RunSummary,
  UsageReport,
} from "../types/ai";

// TEST FIXTURES: payloads shaped like /api/v1/ai responses. Not real decisions.
const RUN: RunSummary = {
  id: "run-1",
  policy_version_id: "pv-1",
  policy_code: "atlas-decision-policy",
  policy_version: 1,
  mode: "HISTORICAL",
  basis: "RECORDED",
  trigger: "cli",
  decision_at: "2026-10-30T21:00:00Z",
  knowledge_at: "2026-10-30T21:00:00Z",
  status: "COMPLETED",
  outcome: "OPPORTUNITIES_PRIORITIZED",
  proposals_considered: 3,
  candidates_count: 2,
  prioritized_count: 1,
  outcome_counts: { PRIORITIZE_FOR_REVIEW: 1, REJECT: 1 },
  notes: ["no usable LLM news analysis for these assets at the decision time"],
  error: null,
  started_at: "2026-11-01T00:00:00Z",
  finished_at: "2026-11-01T00:00:01Z",
};

const PRIORITIZED: DecisionSummary = {
  id: "d-1",
  run_id: "run-1",
  outcome: "PRIORITIZE_FOR_REVIEW",
  rank: 1,
  asset_id: "a-1",
  symbol: "FXA",
  direction: "LONG",
  timeframes: ["1Day"],
  strategies: ["momentum", "breakout"],
  proposal_count: 2,
  heuristic_score: 0.812,
  ranking_score: 0.77,
  component_coverage: 0.83,
  calibrated_probability: null,
  expected_value_status: "NOT_SUPPORTED",
  reward_risk_net: 2.41,
  uncertainty_index: 0.15,
  reasons: [{ code: "PRIORITIZED", message: "ranking score 0.770 >= 0.55; a candidate for future risk review, not an approved trade", outcome: "PRIORITIZE_FOR_REVIEW" }],
  conflict_types: ["DUPLICATE_PROPOSALS"],
  decision_at: RUN.decision_at,
  data_cutoff: "2026-10-30T20:00:00Z",
  expires_at: "2026-10-31T21:00:00Z",
};

const REJECTED: DecisionSummary = {
  ...PRIORITIZED,
  id: "d-2",
  outcome: "REJECT",
  rank: null,
  asset_id: "a-2",
  symbol: "FXB",
  direction: "SHORT",
  strategies: ["mean_reversion"],
  reasons: [{ code: "POOR_REWARD_RISK", message: "net reward/risk 0.62 after assumed costs is below 1.00", outcome: "REJECT" }],
};

const DETAIL: DecisionDetail = {
  ...PRIORITIZED,
  policy_version_id: "pv-1",
  proposal_ids: ["p-1", "p-2"],
  strategy_versions: [
    { strategy_id: "momentum", version: 1, logic_version: "momentum-1" },
    { strategy_id: "breakout", version: 1, logic_version: "breakout-1" },
  ],
  model_versions: [],
  regime_snapshot: { market: { TREND: { label: "BULLISH", confidence: 0.7, as_of: RUN.decision_at } } },
  news_refs: [],
  llm_refs: [],
  feature_timestamps: {},
  scores: { note: "heuristic_score and ranking_score are weighted heuristics that order opportunities; they are not probabilities of profit" },
  expected: { potential_reward_net: 0.099, potential_adverse_net: 0.041, cost_basis: "ASSUMPTION: 10 bps", expected_value: { status: "NOT_SUPPORTED" } },
  uncertainty: { index: 0.15, kind: "HEURISTIC_UNCERTAINTY_INDEX (not a confidence interval)" },
  conflicts: [{ type: "DUPLICATE_PROPOSALS", description: "2 proposals with entries within 1.0%", handling: "merged into one opportunity" }],
  explanation: "PRIORITIZE_FOR_REVIEW: FXA LONG (1Day). Analytical prioritization only: the risk engine has not reviewed it.",
  content_sha256: "a".repeat(64),
  created_at: RUN.finished_at ?? "",
  components: [
    { name: "signal_quality", kind: "HEURISTIC", status: "AVAILABLE", score: 0.8, raw_value: 0.8, weight: 0.15, contribution: 0.14, reason: "mean uncalibrated signal strength", inputs: {} },
    { name: "ml_support", kind: "HEURISTIC", status: "UNAVAILABLE", score: null, raw_value: null, weight: 0.15, contribution: null, reason: "no APPROVED model prediction", inputs: {} },
  ],
  evidence: [{ id: 1, kind: "STRATEGY_PROPOSAL", stance: "SUPPORTS", summary: "momentum v1 LONG 1Day: strength 0.800 (uncalibrated)", input_id: 1, data: {} }],
  proposals: [
    { id: "p-1", strategy_id: "momentum", direction: "LONG", timeframe: "1Day", signal_at: RUN.decision_at, data_cutoff: "2026-10-30T20:00:00Z", reference_entry: "100", invalidation_level: "96", target_reference: "110", signal_strength: "0.8000", strength_kind: "UNCALIBRATED_SCORE", expires_at: "2026-11-02T21:00:00Z", reasoning: "fixture" },
  ],
  llm_analyses: [],
};

const OVERVIEW: AIOverview = {
  runs: { COMPLETED: 1 },
  decisions: { PRIORITIZE_FOR_REVIEW: 1, REJECT: 1 },
  llm_analyses: {},
  latest_run: RUN,
  llm: { provider: "none", model: null, configured: false, priced: false },
  current_policy_sha256: "b".repeat(64),
  disclaimer: "PRIORITIZE_FOR_REVIEW means a candidate for the future deterministic risk engine (Phase 6); it is not an approved trade.",
  llm_note: "LLM outputs are unverified estimates.",
};

const USAGE: UsageReport = {
  window_days: 30,
  configuration: { provider: "none", model: null, priced: false, input_usd_per_mtok: null, output_usd_per_mtok: null, pricing_source: null, max_concurrency: 2, max_retries: 2, batch_size: 5, news_analysis_enabled: false },
  budget: { spent_today_usd: "0", spent_month_usd: "0", reserved_usd: "0", requests_today: 0, daily_budget_usd: "0.50", monthly_budget_usd: "5.00", max_requests_per_day: 200 },
  by_day: [],
  by_task: [],
  request_statuses: {},
  note: "Costs are estimates.",
};

const EVALUATION: EvaluationView = {
  id: "e-1",
  known_at: "2026-11-03T00:00:00Z",
  decision_start: RUN.decision_at,
  decision_end: RUN.decision_at,
  runs_evaluated: 1,
  spec: {},
  results: { coordinator: { observations: 1, mean: 0.049 }, take_all: { observations: 2, mean: 0.01 }, strongest_signal: { observations: 1, mean: 0.049 }, declined: { observations: 1, mean: -0.03 } },
  verdict: "INSUFFICIENT_EVIDENCE",
  notes: ["1 matured coordinator outcome(s); the verdict needs at least 30"],
  created_at: "2026-11-03T00:00:00Z",
};

function routes(overrides: Record<string, unknown> = {}) {
  const rankings: Rankings = { run: RUN, decisions: [PRIORITIZED, REJECTED], disclaimer: OVERVIEW.disclaimer };
  return {
    ...LAYOUT_ROUTES,
    "/api/v1/ai/overview": { body: OVERVIEW },
    "/api/v1/ai/rankings": { body: rankings },
    "/api/v1/ai/runs": { body: [RUN] },
    "/api/v1/ai/decisions/d-1": { body: DETAIL },
    "/api/v1/ai/llm/analyses": { body: [] },
    "/api/v1/ai/llm/usage": { body: USAGE },
    "/api/v1/ai/evaluations": { body: [EVALUATION] },
    ...overrides,
  } as Parameters<typeof stubApi>[0];
}

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider initial="UTC">
      <MemoryRouter initialEntries={[path]}>
        <App pollIntervalMs={0} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

beforeEach(() => {
  installFakeLive();
});

describe("Central AI Activity page", () => {
  it("separates prioritization from risk approval and offers no trading controls", async () => {
    stubApi(routes());
    renderAt("/ai");
    expect(await screen.findByText("Analytical prioritization is not risk approval.")).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Ranked opportunities" });
    expect(within(table).getByText("Prioritized for review")).toBeInTheDocument();
    expect(within(table).getByText("Reject")).toBeInTheDocument();
    expect(within(table).getByText(/Poor reward risk: net reward\/risk 0.62/)).toBeInTheDocument();
    expect(within(table).getAllByText("Not claimed")).toHaveLength(2);
    for (const word of [/buy/i, /sell/i, /execute/i, /submit/i, /approve/i]) {
      expect(screen.queryByRole("button", { name: word })).not.toBeInTheDocument();
    }
  });

  it("explains a decision with components, evidence and conflicts", async () => {
    stubApi(routes());
    renderAt("/ai");
    const table = await screen.findByRole("table", { name: "Ranked opportunities" });
    fireEvent.click(within(table).getByRole("button", { name: "Details of FXA LONG" }));
    const components = await screen.findByRole("table", { name: "Scoring components" });
    expect(within(components).getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/the risk engine has not reviewed it/)).toBeInTheDocument();
    expect(screen.getByText(/not claimed: no validated probability/)).toBeInTheDocument();
    expect(screen.getByText("none (no approved, calibrated model)")).toBeInTheDocument();
    expect(screen.getByText(/merged into one opportunity/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Strategy proposals considered" })).toBeInTheDocument();
  });

  it("states when no LLM is configured and shows baselines without inventing numbers", async () => {
    stubApi(routes());
    renderAt("/ai");
    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    expect(await screen.findByText("No LLM analysis has been stored.")).toBeInTheDocument();
    expect(await screen.findByText("No LLM tokens have been used.")).toBeInTheDocument();
    expect(screen.getByText("not configured: every call is refused")).toBeInTheDocument();
    const baselines = await screen.findByRole("table", { name: "Baseline comparison" });
    expect(within(baselines).getByText("Baseline: take every opportunity")).toBeInTheDocument();
    expect(screen.getByText("Insufficient evidence")).toBeInTheDocument();
  });

  it("says so when no run has completed", async () => {
    stubApi(
      routes({
        "/api/v1/ai/overview": { body: { ...OVERVIEW, runs: {}, decisions: {}, latest_run: null } },
        "/api/v1/ai/rankings": { body: { run: null, decisions: [], disclaimer: "" } },
        "/api/v1/ai/runs": { body: [] },
        "/api/v1/ai/evaluations": { body: [] },
      }),
    );
    renderAt("/ai");
    expect(await screen.findByText("No decision run has completed yet.")).toBeInTheDocument();
    expect(await screen.findByText("No decision run yet.")).toBeInTheDocument();
    expect(await screen.findByText("No baseline comparison has been run.")).toBeInTheDocument();
  });

  it("is in the navigation", async () => {
    stubApi(routes());
    renderAt("/ai");
    const link = await screen.findByRole("link", { name: /Central AI Activity/ });
    expect(link).toHaveAttribute("href", "/ai");
  });
});
