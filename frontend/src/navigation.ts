import {
  Activity,
  Bot,
  BrainCircuit,
  CandlestickChart,
  ClipboardCheck,
  ClipboardList,
  Gauge,
  Cpu,
  DatabaseZap,
  FlaskConical,
  HeartPulse,
  History,
  LayoutDashboard,
  Layers,
  Newspaper,
  Radar,
  type LucideIcon,
  ServerCog,
  Settings,
  ShieldAlert,
  ShieldCheck,
  Telescope,
  Wallet,
} from "lucide-react";

export type NavGroup = "Monitor" | "Decisions" | "Paper trading" | "Research" | "Operations";

/** Sidebar groups, in display order. */
export const NAV_GROUPS: readonly NavGroup[] = ["Monitor", "Decisions", "Paper trading", "Research", "Operations"];

export interface NavSection {
  path: string;
  title: string;
  icon: LucideIcon;
  group: NavGroup;
  /** Spec section describing this page (ATLAS_MASTER_SPEC_V2 §22). */
  specSection: string;
  /** Roadmap phase that delivers the page's data. */
  phase: number;
  summary: string;
}

/** Pages backed by real backend data (Phase 1, the Phase 2 market-data engine, Phases 3A-3C, 4A-4B, 5A-5B, 6A-6C, 7A-7B, 8A-8B, completed in Phase 9; Phase 10A orchestration). */
export const NAV_SECTIONS: readonly NavSection[] = [
  {
    path: "/",
    group: "Monitor",
    title: "Overview",
    icon: LayoutDashboard,
    specSection: "22.1",
    phase: 9,
    summary: "Paper equity, P/L and exposure, risk and system status, market session, data pipeline and monitored assets at a glance.",
  },
  {
    path: "/markets",
    group: "Monitor",
    title: "Markets",
    icon: CandlestickChart,
    specSection: "22.2",
    phase: 9,
    summary: "Monitored assets, live prices, market regime, the active watchlist, charts and data freshness.",
  },
  {
    path: "/discovery",
    group: "Monitor",
    title: "Asset Discovery",
    icon: Telescope,
    specSection: "8",
    phase: 3,
    summary: "Discovery pool, qualified universe and active watchlist, with reasons and history.",
  },
  {
    path: "/intelligence",
    group: "Monitor",
    title: "Market Intelligence",
    icon: Radar,
    specSection: "9",
    phase: 3,
    summary: "Trend, volatility and stability of the market, universe participation and correlation.",
  },
  {
    path: "/news",
    group: "Monitor",
    title: "News & Events",
    icon: Newspaper,
    specSection: "17",
    phase: 3,
    summary: "Headlines as received, mapped to assets and classified by versioned rules; provider and stream status.",
  },
  {
    path: "/strategies",
    group: "Decisions",
    title: "Strategy Laboratory",
    icon: Activity,
    specSection: "11",
    phase: 4,
    summary: "Strategy catalog, immutable versions, parameters, proposals and evaluation history.",
  },
  {
    path: "/ml",
    group: "Research",
    title: "Machine Learning",
    icon: Cpu,
    specSection: "12.2",
    phase: 5,
    summary: "Point-in-time datasets, walk-forward training runs, calibration evidence, the model registry and predictions.",
  },
  {
    path: "/ai",
    group: "Decisions",
    title: "Central AI Activity",
    icon: BrainCircuit,
    specSection: "22.3",
    phase: 5,
    summary: "Evaluated and ranked opportunities, rejections and their reasons, conflicts, model and news inputs, and LLM usage and cost.",
  },
  {
    path: "/autonomy",
    group: "Paper trading",
    title: "Autonomous Trading",
    icon: Bot,
    specSection: "20, 24.2",
    phase: 10,
    summary: "The trading orchestrator: operator trading control (disabled by default), pause, disable and emergency stop, trade intents from AI decisions, scheduled risk monitoring and reconciliation jobs, and the worker's heartbeat.",
  },
  {
    path: "/portfolio",
    group: "Paper trading",
    title: "Portfolio & Accounting",
    icon: Wallet,
    specSection: "22.4, 22.9",
    phase: 6,
    summary: "The simulated paper account's double-entry books: funding, cash by currency, holdings, exposure, P/L, costs, journal, balances and reconciliation.",
  },
  {
    path: "/risk",
    group: "Paper trading",
    title: "Risk Center",
    icon: ShieldAlert,
    specSection: "22.8",
    phase: 6,
    summary: "Hard limits, live exposure, daily and weekly loss, drawdown, circuit breakers, the kill switch and every risk decision with its reasons.",
  },
  {
    path: "/paper-orders",
    group: "Paper trading",
    title: "Paper Orders",
    icon: ClipboardList,
    specSection: "15",
    phase: 7,
    summary: "Simulated paper orders and the isolated paper broker: order history, state changes, simulated fills and their costs, reservation linkage. No brokerage, no trading controls.",
  },
  {
    path: "/positions",
    group: "Paper trading",
    title: "Open Positions",
    icon: Layers,
    specSection: "13, 15",
    phase: 7,
    summary: "Simulated open positions: entry, current mark, unrealized P/L, stop, target and holding duration, managed by the position monitor.",
  },
  {
    path: "/positions/closed",
    group: "Paper trading",
    title: "Closed Positions",
    icon: History,
    specSection: "13, 15",
    phase: 7,
    summary: "Simulated closed positions: entry and exit, realized and net P/L after costs, exit reason and strategy origin.",
  },
  {
    path: "/execution-analytics",
    group: "Paper trading",
    title: "Execution Analytics",
    icon: Gauge,
    specSection: "15.3",
    phase: 7,
    summary: "Simulated slippage, fill rates, partial fills, estimated costs and the versioned execution-model assumptions.",
  },
  {
    path: "/backtesting",
    group: "Research",
    title: "Backtesting",
    icon: FlaskConical,
    specSection: "22.7",
    phase: 8,
    summary: "Configure, run, inspect and compare reproducible event-driven backtests of stored history.",
  },
  {
    path: "/research",
    group: "Research",
    title: "Research & Validation",
    icon: ShieldCheck,
    specSection: "18.2, 19",
    phase: 8,
    summary: "Walk-forward validation, robustness and overfitting controls, champion/challenger governance, ML model comparison and degradation monitoring.",
  },
  {
    path: "/evidence",
    group: "Research",
    title: "Evidence & Readiness",
    icon: ClipboardCheck,
    specSection: "20, 20.1",
    phase: 11,
    summary: "Readiness scorecards under pre-registered criteria: backtest, out-of-sample and paper-trading evidence kept apart; nothing is promoted automatically.",
  },
  {
    path: "/data-health",
    group: "Operations",
    title: "Data Health",
    icon: DatabaseZap,
    specSection: "22.10",
    phase: 2,
    summary: "Pipeline stages, missing intervals, recovery and data-quality events.",
  },
  {
    path: "/ingestion",
    group: "Operations",
    title: "Ingestion",
    icon: ServerCog,
    specSection: "7",
    phase: 2,
    summary: "Historical retrieval and recovery jobs.",
  },
  {
    path: "/system-health",
    group: "Operations",
    title: "System Health",
    icon: HeartPulse,
    specSection: "22.10",
    phase: 9,
    summary: "API, database, migrations, Redis, background workers, providers, recent errors and recovery activity.",
  },
  {
    path: "/settings",
    group: "Operations",
    title: "Settings",
    icon: Settings,
    specSection: "22.11",
    phase: 9,
    summary: "System information, paper-only safeguards, worker switches, risk and execution configuration, market-data configuration (all read-only) and display preferences.",
  },
];

/**
 * Systems that do not exist yet. Shown disabled in the navigation with their roadmap
 * phase; their URLs render an explicit "not built" page with no numbers.
 */
export const UPCOMING_SECTIONS: readonly NavSection[] = [];
