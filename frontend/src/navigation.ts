import {
  Activity,
  BookOpenCheck,
  BrainCircuit,
  CandlestickChart,
  FlaskConical,
  Gauge,
  HeartPulse,
  LayoutDashboard,
  type LucideIcon,
  Settings,
  ShieldAlert,
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

export const NAV_SECTIONS: readonly NavSection[] = [
  {
    path: "/",
    title: "Overview",
    icon: LayoutDashboard,
    specSection: "22.1",
    phase: 9,
    summary: "Equity, P/L, exposure, drawdown and system status at a glance.",
  },
  {
    path: "/markets",
    title: "Markets",
    icon: CandlestickChart,
    specSection: "22.2",
    phase: 2,
    summary: "Tracked assets, discovery rankings, charts, regimes and data freshness.",
  },
  {
    path: "/ai-activity",
    title: "AI Activity",
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
    path: "/strategies",
    title: "Strategies",
    icon: Activity,
    specSection: "22.5",
    phase: 4,
    summary: "Strategy status, versions and regime-specific performance.",
  },
  {
    path: "/performance",
    title: "Performance",
    icon: Gauge,
    specSection: "22.6",
    phase: 8,
    summary: "Equity curve, drawdown, benchmark and risk-adjusted performance.",
  },
  {
    path: "/backtesting",
    title: "Backtesting",
    icon: FlaskConical,
    specSection: "22.7",
    phase: 8,
    summary: "Configure, run and compare reproducible historical backtests.",
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
    path: "/accounting",
    title: "Accounting",
    icon: BookOpenCheck,
    specSection: "22.9",
    phase: 6,
    summary: "Cash by currency, journal history, fees and reconciliation.",
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
    phase: 9,
    summary: "Approved, audited configuration. Hard risk limits require explicit authorisation.",
  },
];
