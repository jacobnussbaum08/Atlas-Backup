import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { App } from "../App";
import { HEALTH_FIXTURE, NOT_READY_FIXTURE, READY_FIXTURE, stubFetch } from "../test/fixtures";

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/system-health"]}>
      <App pollIntervalMs={0} />
    </MemoryRouter>,
  );
}

describe("System Health page", () => {
  it("lists each dependency with its status and latency", async () => {
    stubFetch({ "/health": { status: 200, body: HEALTH_FIXTURE }, "/ready": { status: 200, body: READY_FIXTURE } });
    renderPage();

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[0]?.textContent)).toEqual([
      "PostgreSQL",
      "Database migrations",
      "Redis",
    ]);
    expect(within(table).getAllByText("OK")).toHaveLength(3);
    expect(within(table).getByText("1.2 ms")).toBeInTheDocument();
    expect(screen.getByText("PAPER")).toBeInTheDocument();
  });

  it("shows failing components with their detail", async () => {
    stubFetch({ "/health": { status: 200, body: HEALTH_FIXTURE }, "/ready": { status: 503, body: NOT_READY_FIXTURE } });
    renderPage();

    const table = await screen.findByRole("table");
    expect(within(table).getByText("Error")).toBeInTheDocument();
    expect(within(table).getByText("ConnectionError: refused")).toBeInTheDocument();
  });

  it("shows an alert and no stale data when the API is unreachable", async () => {
    stubFetch({ "/health": "network-error", "/ready": "network-error" });
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("Atlas API is unreachable");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("treats unexpected HTTP errors as failures", async () => {
    stubFetch({ "/health": { status: 500, body: {} }, "/ready": { status: 200, body: READY_FIXTURE } });
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 500");
  });
});
