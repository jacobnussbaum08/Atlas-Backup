import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "./App";
import { DisplayZoneProvider } from "./lib/time";
import { NAV_SECTIONS, UPCOMING_SECTIONS } from "./navigation";
import { HEALTH_FIXTURE, NOT_READY_FIXTURE, READY_FIXTURE } from "./test/fixtures";
import {
  CONFIGURATION,
  LAYOUT_ROUTES,
  METRICS,
  PIPELINE_HEALTH,
  STREAM_STATUS,
  installFakeLive,
  snapshots,
  stubApi,
} from "./test/marketFixtures";

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider initial="UTC">
      <MemoryRouter initialEntries={[path]}>
        <App pollIntervalMs={0} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

const PAGE_ROUTES = {
  ...LAYOUT_ROUTES,
  "/api/v1/market-data/snapshots": { body: snapshots() },
  "/api/v1/market-data/pipeline-health": { body: PIPELINE_HEALTH },
  "/api/v1/market-data/metrics": { body: METRICS },
  "/api/v1/market-data/configuration": { body: CONFIGURATION },
  "/api/v1/market-data/providers": { body: [] },
  "/api/v1/market-data/gaps": { body: [] },
  "/api/v1/market-data/stream/gaps": { body: [] },
  "/api/v1/market-data/quality-events": { body: [] },
  "/api/v1/market-data/audit-events": { body: [] },
  "/api/v1/market-data/assets": { body: [] },
  "/api/v1/market-data/ingestion-jobs": { body: [] },
  "/api/v1/market-data/recovery-jobs": { body: [] },
};

beforeEach(() => {
  installFakeLive();
});

describe("dashboard shell", () => {
  it("links every built section and shows later phases disabled, not as pages", () => {
    stubApi(PAGE_ROUTES);
    renderAt("/");

    const nav = screen.getByRole("navigation", { name: "Dashboard sections" });
    for (const section of NAV_SECTIONS) {
      expect(within(nav).getByRole("link", { name: section.title })).toBeInTheDocument();
    }
    for (const section of UPCOMING_SECTIONS) {
      expect(within(nav).queryByRole("link", { name: new RegExp(section.title) })).toBeNull();
      const item = within(nav).getByText(section.title).closest("[aria-disabled]");
      expect(item).toHaveAttribute("aria-disabled", "true");
      expect(item).toHaveAttribute("title", `Not built yet: roadmap Phase ${section.phase}`);
    }
  });

  it("navigates between pages through the sidebar", async () => {
    stubApi(PAGE_ROUTES);
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Dashboard sections" });

    fireEvent.click(within(nav).getByRole("link", { name: "Markets" }));
    expect(await screen.findByRole("heading", { name: "Markets" })).toBeInTheDocument();
    fireEvent.click(within(nav).getByRole("link", { name: "Data Health" }));
    expect(await screen.findByRole("heading", { name: "Data Health" })).toBeInTheDocument();
    fireEvent.click(within(nav).getByRole("link", { name: "Ingestion" }));
    expect(await screen.findByRole("heading", { name: "Ingestion" })).toBeInTheDocument();
    fireEvent.click(within(nav).getByRole("link", { name: "Settings" }));
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    fireEvent.click(within(nav).getByRole("link", { name: "Overview" }));
    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });

  it("always shows the paper-trading, monitoring-only label", () => {
    stubApi({ "/health": "network-error", "/ready": "network-error", "/api/v1/market-data/stream": "network-error", "/api/v1/market-data/market-status": "network-error" });
    renderAt("/risk");

    const label = screen.getByRole("status", { name: /paper trading mode/i });
    expect(label).toHaveTextContent(/paper trading/i);
    expect(label).toHaveTextContent(/monitoring only/i);
    expect(screen.getByText("No order execution")).toBeInTheDocument();
  });

  it("shows unbuilt sections explicitly as not built, without numbers", () => {
    stubApi(PAGE_ROUTES);
    renderAt("/risk");

    expect(screen.getByRole("heading", { name: "Risk Center" })).toBeInTheDocument();
    expect(screen.getByText("No data yet")).toBeInTheDocument();
    expect(screen.getByRole("main").textContent).not.toMatch(/\$|\d+\.\d+%/);
  });

  it("shows provider, feed and stream health from the backend in the header", async () => {
    stubApi({ ...PAGE_ROUTES, "/api/v1/market-data/stream": { body: { ...STREAM_STATUS, health: "NOT_RUNNING" } } });
    renderAt("/");
    const header = screen.getByRole("banner");

    expect(await within(header).findByText("Alpaca")).toBeInTheDocument();
    expect(within(header).getByText("IEX")).toBeInTheDocument();
    expect(within(header).getByText("Not running")).toBeInTheDocument();
    expect(await within(header).findByText("Regular")).toBeInTheDocument();
    expect(within(header).getByText(/UTC/)).toBeInTheDocument();
  });

  it("reports readiness in the header", async () => {
    stubApi(PAGE_ROUTES);
    renderAt("/");

    expect(await screen.findByText("All systems ready")).toBeInTheDocument();
    expect(screen.getByText("v0.1.0")).toBeInTheDocument();
  });

  it("reports a degraded system when readiness fails", async () => {
    stubApi({ ...PAGE_ROUTES, "/ready": { status: 503, body: NOT_READY_FIXTURE } });
    renderAt("/");

    expect(await screen.findByText("Degraded")).toBeInTheDocument();
  });

  it("reports an unreachable API", async () => {
    stubApi({ ...PAGE_ROUTES, "/health": "network-error", "/ready": "network-error" });
    renderAt("/");

    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });

  it("keeps the system health page working", async () => {
    stubApi({ ...PAGE_ROUTES, "/health": { body: HEALTH_FIXTURE }, "/ready": { body: READY_FIXTURE } });
    renderAt("/system-health");

    expect(await screen.findByText("PostgreSQL")).toBeInTheDocument();
  });
});
