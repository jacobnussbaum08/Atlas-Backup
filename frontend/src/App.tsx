import { Route, Routes } from "react-router";

import { DashboardLayout } from "./layouts/DashboardLayout";
import { NAV_SECTIONS } from "./navigation";
import { SectionPlaceholderPage } from "./pages/SectionPlaceholderPage";
import { SystemHealthPage } from "./pages/SystemHealthPage";

export function App({ pollIntervalMs }: { pollIntervalMs?: number }) {
  return (
    <Routes>
      <Route element={<DashboardLayout {...(pollIntervalMs === undefined ? {} : { pollIntervalMs })} />}>
        {NAV_SECTIONS.map((section) =>
          section.path === "/system-health" ? (
            <Route key={section.path} path={section.path} element={<SystemHealthPage />} />
          ) : (
            <Route
              key={section.path}
              path={section.path}
              element={<SectionPlaceholderPage section={section} />}
            />
          ),
        )}
        <Route
          path="*"
          element={<p className="text-ink-muted text-sm">This page does not exist.</p>}
        />
      </Route>
    </Routes>
  );
}
