import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router";

import { Loading } from "./components/ui";
import { DashboardLayout } from "./layouts/DashboardLayout";
import { UPCOMING_SECTIONS } from "./navigation";
import { DataHealthPage } from "./pages/DataHealthPage";
import { DiscoveryAssetPage } from "./pages/DiscoveryAssetPage";
import { DiscoveryPage } from "./pages/DiscoveryPage";
import { IngestionPage } from "./pages/IngestionPage";
import { IntelligencePage } from "./pages/IntelligencePage";
import { MarketsPage } from "./pages/MarketsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SectionPlaceholderPage } from "./pages/SectionPlaceholderPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SystemHealthPage } from "./pages/SystemHealthPage";

// The chart library is only needed on the asset page: load it with that page.
const AssetDetailPage = lazy(() =>
  import("./pages/AssetDetailPage").then((module) => ({ default: module.AssetDetailPage })),
);

export function App({ pollIntervalMs }: { pollIntervalMs?: number }) {
  return (
    <Routes>
      <Route element={<DashboardLayout {...(pollIntervalMs === undefined ? {} : { pollIntervalMs })} />}>
        <Route index element={<OverviewPage />} />
        <Route path="/markets" element={<MarketsPage />} />
        <Route
          path="/markets/:symbol"
          element={
            <Suspense fallback={<Loading label="Loading chart…" />}>
              <AssetDetailPage />
            </Suspense>
          }
        />
        <Route path="/discovery" element={<DiscoveryPage />} />
        <Route path="/discovery/assets/:assetId" element={<DiscoveryAssetPage />} />
        <Route path="/intelligence" element={<IntelligencePage />} />
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
