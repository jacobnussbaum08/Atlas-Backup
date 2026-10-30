import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { NAV_SECTIONS } from "./navigation";
import {
  HEALTH_FIXTURE,
  NOT_READY_FIXTURE,
  READY_FIXTURE,
  stubFetch,
} from "./test/fixtures";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App pollIntervalMs={0} />
    </MemoryRouter>,
  );
}

describe("dashboard shell", () => {
  it("renders every spec section in the navigation", () => {
    stubFetch({ "/health": { status: 200, body: HEALTH_FIXTURE }, "/ready": { status: 200, body: READY_FIXTURE } });
    renderAt("/");

    const nav = screen.getByRole("navigation", { name: "Dashboard sections" });
    for (const section of NAV_SECTIONS) {
      expect(within(nav).getByRole("link", { name: section.title })).toBeInTheDocument();
    }
  });

  it("always shows the paper-trading label", () => {
    stubFetch({ "/health": "network-error", "/ready": "network-error" });
    renderAt("/portfolio");

    expect(screen.getByRole("status", { name: /paper trading mode/i })).toHaveTextContent(
      /paper trading/i,
    );
  });

  it("shows placeholders without fabricated numbers for unbuilt sections", () => {
    stubFetch({ "/health": { status: 200, body: HEALTH_FIXTURE }, "/ready": { status: 200, body: READY_FIXTURE } });
    renderAt("/portfolio");

    expect(screen.getByRole("heading", { name: "Portfolio" })).toBeInTheDocument();
    expect(screen.getByText("No data yet")).toBeInTheDocument();
    expect(screen.getByRole("main").textContent).not.toMatch(/\$|\d+\.\d+%/);
  });

  it("reports readiness in the header", async () => {
    stubFetch({ "/health": { status: 200, body: HEALTH_FIXTURE }, "/ready": { status: 200, body: READY_FIXTURE } });
    renderAt("/");

    expect(await screen.findByText("All systems ready")).toBeInTheDocument();
    expect(screen.getByText("v0.1.0")).toBeInTheDocument();
  });

  it("reports a degraded system when readiness fails", async () => {
    stubFetch({ "/health": { status: 200, body: HEALTH_FIXTURE }, "/ready": { status: 503, body: NOT_READY_FIXTURE } });
    renderAt("/");

    expect(await screen.findByText("Degraded")).toBeInTheDocument();
  });

  it("reports an unreachable API", async () => {
    stubFetch({ "/health": "network-error", "/ready": "network-error" });
    renderAt("/");

    expect(await screen.findByText("API unreachable")).toBeInTheDocument();
  });
});
