import {
  Activity,
  BrainCircuit,
  CandlestickChart,
  DatabaseZap,
  FlaskConical,
  HeartPulse,
  LayoutDashboard,
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

/** Pages backed by real backend data (Phase 1, the Phase 2 market-data engine, Phase 3A). */
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
    path: "/strategies",
    title: "Strategies",
    icon: Activity,
    specSection: "22.5",
    phase: 4,
    summary: "Strategy status, versions and regime-specific performance.",
  },
  {
    path: "/ai",
    title: "AI Intelligence",
    icon: BrainCircuit,
    specSection: "22.3",
    phase: 5,
    summary: "Proposals, decisions, evidence, model predictions and news analysis.",
  },
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
