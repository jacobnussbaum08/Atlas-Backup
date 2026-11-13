import { type ComponentType, lazy, Suspense } from "react";
import { Route, Routes } from "react-router";

import { Loading } from "./components/ui";
import { DashboardLayout } from "./layouts/DashboardLayout";
import { UPCOMING_SECTIONS } from "./navigation";
import { MarketsPage } from "./pages/MarketsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SectionPlaceholderPage } from "./pages/SectionPlaceholderPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SystemHealthPage } from "./pages/SystemHealthPage";

/**
 * Sections other than the landing pages load on first visit, so the first screen does not
 * download every page (and the chart library) up front. Created once at module scope, so a
 * re-render never remounts a page.
 */
function lazyPage<K extends string>(load: () => Promise<Record<K, ComponentType>>, name: K): ComponentType {
  const Component = lazy<ComponentType>(() => load().then((module) => ({ default: module[name] })));
  return function LazyPage() {
    return (
      <Suspense fallback={<Loading label="Loading page…" />}>
        <Component />
      </Suspense>
    );
  };
}

const AssetDetailPage = lazyPage(() => import("./pages/AssetDetailPage"), "AssetDetailPage");
const DiscoveryPage = lazyPage(() => import("./pages/DiscoveryPage"), "DiscoveryPage");
const DiscoveryAssetPage = lazyPage(() => import("./pages/DiscoveryAssetPage"), "DiscoveryAssetPage");
const IntelligencePage = lazyPage(() => import("./pages/IntelligencePage"), "IntelligencePage");
const NewsPage = lazyPage(() => import("./pages/NewsPage"), "NewsPage");
const NewsArticlePage = lazyPage(() => import("./pages/NewsArticlePage"), "NewsArticlePage");
const StrategiesPage = lazyPage(() => import("./pages/StrategiesPage"), "StrategiesPage");
const StrategyDetailPage = lazyPage(() => import("./pages/StrategyDetailPage"), "StrategyDetailPage");
const MLPage = lazyPage(() => import("./pages/MLPage"), "MLPage");
const AIActivityPage = lazyPage(() => import("./pages/AIActivityPage"), "AIActivityPage");
const AutonomyPage = lazyPage(() => import("./pages/AutonomyPage"), "AutonomyPage");
const PortfolioPage = lazyPage(() => import("./pages/PortfolioPage"), "PortfolioPage");
const RiskCenterPage = lazyPage(() => import("./pages/RiskCenterPage"), "RiskCenterPage");
const PaperOrdersPage = lazyPage(() => import("./pages/PaperOrdersPage"), "PaperOrdersPage");
const OpenPositionsPage = lazyPage(() => import("./pages/PaperPositionsPage"), "OpenPositionsPage");
const ClosedPositionsPage = lazyPage(() => import("./pages/PaperPositionsPage"), "ClosedPositionsPage");
const ExecutionAnalyticsPage = lazyPage(() => import("./pages/ExecutionAnalyticsPage"), "ExecutionAnalyticsPage");
const BacktestingPage = lazyPage(() => import("./pages/BacktestingPage"), "BacktestingPage");
const BacktestDetailPage = lazyPage(() => import("./pages/BacktestDetailPage"), "BacktestDetailPage");
const ValidationPage = lazyPage(() => import("./pages/ValidationPage"), "ValidationPage");
const EvidencePage = lazyPage(() => import("./pages/EvidencePage"), "EvidencePage");
const PerformancePage = lazyPage(() => import("./pages/PerformancePage"), "PerformancePage");
const NotificationsPage = lazyPage(() => import("./pages/NotificationsPage"), "NotificationsPage");
const ValidationRunPage = lazyPage(() => import("./pages/ValidationRunPage"), "ValidationRunPage");
const DataHealthPage = lazyPage(() => import("./pages/DataHealthPage"), "DataHealthPage");
const IngestionPage = lazyPage(() => import("./pages/IngestionPage"), "IngestionPage");

export function App({ pollIntervalMs }: { pollIntervalMs?: number }) {
  return (
    <Routes>
      <Route element={<DashboardLayout {...(pollIntervalMs === undefined ? {} : { pollIntervalMs })} />}>
        <Route index element={<OverviewPage />} />
        <Route path="/markets" element={<MarketsPage />} />
        <Route path="/markets/:symbol" element={<AssetDetailPage />} />
        <Route path="/discovery" element={<DiscoveryPage />} />
        <Route
          path="/discovery/assets/:assetId"
          element={<DiscoveryAssetPage />}
        />
        <Route path="/intelligence" element={<IntelligencePage />} />
        <Route path="/news" element={<NewsPage />} />
        <Route path="/news/articles/:articleId" element={<NewsArticlePage />} />
        <Route path="/strategies" element={<StrategiesPage />} />
        <Route
          path="/strategies/:strategyId"
          element={<StrategyDetailPage />}
        />
        <Route path="/ml" element={<MLPage />} />
        <Route path="/ai" element={<AIActivityPage />} />
        <Route path="/autonomy" element={<AutonomyPage />} />
        <Route path="/portfolio" element={<PortfolioPage />} />
        <Route path="/risk" element={<RiskCenterPage />} />
        <Route path="/paper-orders" element={<PaperOrdersPage />} />
        <Route path="/positions" element={<OpenPositionsPage />} />
        <Route path="/positions/closed" element={<ClosedPositionsPage />} />
        <Route
          path="/execution-analytics"
          element={<ExecutionAnalyticsPage />}
        />
        <Route path="/backtesting" element={<BacktestingPage />} />
        <Route path="/backtesting/:runId" element={<BacktestDetailPage />} />
        <Route path="/research" element={<ValidationPage />} />
        <Route path="/evidence" element={<EvidencePage />} />
        <Route path="/performance" element={<PerformancePage />} />
        <Route path="/notifications" element={<NotificationsPage />} />
        <Route path="/research/runs/:runId" element={<ValidationRunPage />} />
        <Route path="/data-health" element={<DataHealthPage />} />
        <Route path="/ingestion" element={<IngestionPage />} />
        <Route path="/system-health" element={<SystemHealthPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        {/* Not built yet: reachable by URL only, and say so explicitly. */}
        {UPCOMING_SECTIONS.map((section) => (
          <Route key={section.path} path={section.path} element={<SectionPlaceholderPage section={section} />} />
        ))}
        <Route path="*" element={<p className="text-ink-muted text-sm">This page does not exist.</p>} />
      </Route>
    </Routes>
  );
}
