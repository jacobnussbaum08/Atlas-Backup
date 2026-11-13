/** Phase 11A evidence program: shapes of /api/v1/evidence responses. */

export type Classification =
  | "REJECTED"
  | "INSUFFICIENT_EVIDENCE"
  | "NOT_DEMONSTRATED"
  | "CONTINUE_PAPER"
  | "CANDIDATE_FOR_REVIEW";

export type CriterionStatus = "PASS" | "FAIL" | "INSUFFICIENT" | "NOT_AVAILABLE";
export type Section = "BACKTEST" | "OUT_OF_SAMPLE" | "PAPER";

export interface Criterion {
  code: string;
  section: Section;
  status: CriterionStatus;
  value: unknown;
  threshold: unknown;
  message: string;
  blocking: boolean;
}

export interface SectionStats {
  trades: number;
  sessions: number;
  net_pnl: number;
  costs: number;
  profit_factor: number | null;
  expectancy: number | null;
  win_rate: number | null;
  max_drawdown: number | null;
  net_return: number | null;
  sharpe_annualized: number | null;
  by_direction: Record<string, { trades: number; net_pnl: number; expectancy?: number | null }>;
  by_regime: Record<string, { trades: number; net_pnl: number }>;
}

export interface OutOfSampleSection extends SectionStats {
  deflated_sharpe: number | null;
  pbo: number | null;
  benchmark_sharpe_annualized: number | null;
  cost_stress_net_pnl: number | null;
  holdout: { access_number: number; trades: number; net_pnl: number; first: string; last: string } | null;
  baselines: {
    benchmark: string | null;
    benchmark_return: number | null;
    equal_weight_universe_return: number | null;
    cash_return: number;
  } | null;
  run: { run_id: string; period: [string, string]; verdict_8b: string | null };
}

export interface BacktestSection {
  source: string;
  first: string | null;
  last: string | null;
  sessions: number | null;
  net_return: number | null;
  max_drawdown: number | null;
  sharpe_annualized: number | null;
  trade_statistics: { trades: number; net_pnl: number; profit_factor: number | null } | null;
  note: string;
}

export interface PaperSection extends SectionStats {
  entry_shortfall_vs_decision_bps: number | null;
  assumed_cost_bps: number | null;
  entry_fills: number;
}

export interface StrategyScore {
  classification: Classification;
  reasons: string[];
  rejections: string[];
  criteria: Criterion[];
  backtest: BacktestSection | null;
  out_of_sample: OutOfSampleSection | null;
  paper: PaperSection | null;
}

export interface ScorecardResult {
  statement: string;
  protocol: { id: string; name: string; version: number; criteria_sha256: string; registered_at: string };
  as_of: string;
  verdict: "NOT_READY" | "READY_FOR_REVIEW";
  strategies: Record<string, StrategyScore>;
  portfolio: {
    classification: Classification;
    criteria: Criterion[];
    oos_trades: number;
    oos_source: string;
    data_audit: { id: string; status: string; failures: string[]; warnings: string[] } | null;
  };
  paper_observation: {
    account_name: string;
    observed_sessions: number;
    closed_trades: number;
    first_observed_session: string | null;
    last_observed_session: string | null;
  } | null;
}

export interface Scorecard {
  id: string;
  verdict: "NOT_READY" | "READY_FOR_REVIEW";
  as_of: string;
  result_sha256: string;
  entries: { subject: string; classification: Classification; reasons: string[] }[];
  result: ScorecardResult;
}

export interface Protocol {
  id: string;
  name: string;
  version: number;
  criteria_sha256: string;
  rationale: string;
  registered_by: string;
  registered_at: string;
}
