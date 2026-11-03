import {
  Activity,
  BrainCircuit,
  CandlestickChart,
  Cpu,
  DatabaseZap,
  FlaskConical,
  HeartPulse,
  LayoutDashboard,
  Newspaper,
  Radar,
  type LucideIcon,
  ServerCog,
  Settings,
  ShieldAlert,
  Telescope,
  Wallet,
} from "lucide-react";

export interface NavSection {
  path: string;
  title: string;
  icon: LucideIcon;
  /** Spec section describing this page (ATLAS_MASTER_SPEC_V2 §22). */
  specSection: string;
  /** Roadmap phase that delivers the page's data. */
  phase: number;
  summary: string;
}

/** Pages backed by real backend data (Phase 1, the Phase 2 market-data engine, Phases 3A-3C, 4A-4B, 5A-5B). */
export const NAV_SECTIONS: readonly NavSection[] = [
  {
    path: "/",
    title: "Overview",
    icon: LayoutDashboard,
    specSection: "22.1",
    phase: 2,
    summary: "Market session, data pipeline and monitored assets at a glance.",
  },
  {
    path: "/markets",
    title: "Markets",
    icon: CandlestickChart,
    specSection: "22.2",
    phase: 2,
    summary: "Monitored assets, prices, charts and data freshness.",
  },
  {
    path: "/discovery",
    title: "Asset Discovery",
    icon: Telescope,
    specSection: "8",
    phase: 3,
    summary: "Discovery pool, qualified universe and active watchlist, with reasons and history.",
  },
  {
    path: "/intelligence",
    title: "Market Intelligence",
    icon: Radar,
    specSection: "9",
    phase: 3,
    summary: "Trend, volatility and stability of the market, universe participation and correlation.",
  },
  {
    path: "/news",
    title: "News & Events",
    icon: Newspaper,
    specSection: "17",
    phase: 3,
    summary: "Headlines as received, mapped to assets and classified by versioned rules; provider and stream status.",
  },
  {
    path: "/strategies",
    title: "Strategy Laboratory",
    icon: Activity,
    specSection: "11",
    phase: 4,
    summary: "Strategy catalog, immutable versions, parameters, proposals and evaluation history.",
  },
  {
    path: "/ml",
    title: "Machine Learning",
    icon: Cpu,
    specSection: "12.2",
    phase: 5,
    summary: "Point-in-time datasets, walk-forward training runs, calibration evidence, the model registry and predictions.",
  },
  {
    path: "/ai",
    title: "Central AI Activity",
    icon: BrainCircuit,
    specSection: "22.3",
    phase: 5,
    summary: "Evaluated and ranked opportunities, rejections and their reasons, conflicts, model and news inputs, and LLM usage and cost.",
  },
  {
    path: "/data-health",
    title: "Data Health",
    icon: DatabaseZap,
    specSection: "22.10",
    phase: 2,
    summary: "Pipeline stages, missing intervals, recovery and data-quality events.",
  },
  {
    path: "/ingestion",
    title: "Ingestion",
    icon: ServerCog,
    specSection: "7",
    phase: 2,
    summary: "Historical retrieval and recovery jobs.",
  },
  {
    path: "/system-health",
    title: "System Health",
    icon: HeartPulse,
    specSection: "22.10",
    phase: 1,
    summary: "API, database, migrations and Redis status.",
  },
  {
    path: "/settings",
    title: "Settings",
    icon: Settings,
    specSection: "22.11",
    phase: 2,
    summary: "Market-data configuration (read-only) and display preferences.",
  },
];

/**
 * Systems that do not exist yet. Shown disabled in the navigation with their roadmap
 * phase; their URLs render an explicit "not built" page with no numbers.
 */
export const UPCOMING_SECTIONS: readonly NavSection[] = [
  {
    path: "/portfolio",
    title: "Portfolio",
    icon: Wallet,
    specSection: "22.4",
    phase: 6,
    summary: "Open simulated positions, prices, stops, targets and P/L.",
  },
  {
    path: "/risk",
    title: "Risk Center",
    icon: ShieldAlert,
    specSection: "22.8",
    phase: 6,
    summary: "Hard limits, reservations, circuit breakers and the kill switch.",
  },
  {
    path: "/backtesting",
    title: "Backtesting",
    icon: FlaskConical,
    specSection: "22.7",
    phase: 8,
    summary: "Configure, run and compare reproducible historical backtests.",
  },
];
