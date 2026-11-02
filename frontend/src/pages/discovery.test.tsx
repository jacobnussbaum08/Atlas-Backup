import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import {
  asset,
  CHEAP,
  CHEAP_DETAIL,
  CHEAP_ID,
  CONFIG,
  HISTORY,
  job,
  LIQ_DETAIL,
  LIQ_ID,
  page,
  QUALIFICATION,
  SUMMARY,
} from "../test/discoveryFixtures";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider initial="UTC">
      <MemoryRouter initialEntries={[path]}>
        <App pollIntervalMs={0} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

const ROUTES = {
  ...LAYOUT_ROUTES,
  "/api/v1/discovery/summary": { body: SUMMARY },
  "/api/v1/discovery/jobs": { body: page([job(), job({ id: "j2", kind: "QUALIFICATION", status: "FAILED", error: "no completed qualification job to use", counters: {} })]) },
  "/api/v1/discovery/config": { body: CONFIG },
  "/api/v1/assets/discovery-pool": { body: page([asset(), CHEAP], 120) },
  "/api/v1/assets/qualified": { body: page([asset()]) },
  "/api/v1/assets/watchlist": { body: page([asset()]) },
  "/api/v1/assets": { body: page([asset(), CHEAP]) },
};

beforeEach(() => {
  installFakeLive();
});

describe("Asset Discovery", () => {
  it("shows backend counts, jobs and the discovery pool", async () => {
    stubApi(ROUTES);
    renderAt("/discovery");
    const summary = await screen.findByLabelText("Discovery summary");
    expect(within(summary).getByText("33,567")).toBeInTheDocument();
    expect(within(summary).getByText("14,407")).toBeInTheDocument();
    expect(within(summary).getAllByText("21")).toHaveLength(2);
    expect(within(summary).getByText("19,160")).toBeInTheDocument(); // inactive + delisted
    const jobs = await screen.findByRole("table", { name: "Discovery jobs" });
    expect(within(jobs).getByText("no completed qualification job to use")).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Instruments" });
    const cheap = within(table).getByRole("row", { name: "CHEAP" });
    expect(cheap).toHaveTextContent("Disqualified");
    expect(cheap).toHaveTextContent("Failed: Min price");
    // A flag the provider did not report is shown as unknown, never as "No".
    const liq = within(table).getByRole("row", { name: "LIQ" });
    expect(within(liq).getByTitle("Easy to borrow: not reported by the provider")).toBeInTheDocument();
    expect(screen.getByText("1–50 of 120")).toBeInTheDocument();
    expect(await screen.findByText("us-equity-iex v1")).toBeInTheDocument();
  });

  it("queries the chosen layer with search, filters, sort and pagination", async () => {
    const stub = stubApi(ROUTES);
    renderAt("/discovery");
    await screen.findByRole("table", { name: "Instruments" });
    fireEvent.click(screen.getByRole("radio", { name: "Watchlist" }));
    await waitFor(() => expect(stub.calls.some((c) => c.url.pathname === "/api/v1/assets/watchlist")).toBe(true));
    const watch = stub.calls.find((c) => c.url.pathname === "/api/v1/assets/watchlist");
    expect(watch?.url.searchParams.get("sort")).toBe("rank");
    expect(await screen.findByRole("columnheader", { name: "Rank" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "Discovery pool" }));
    fireEvent.change(screen.getByLabelText("Search symbol or name"), { target: { value: "liq" } });
    fireEvent.change(screen.getByLabelText("Exchange"), { target: { value: "XNAS" } });
    fireEvent.change(screen.getByLabelText("Qualification"), { target: { value: "DISQUALIFIED" } });
    await waitFor(() =>
      expect(
        stub.calls.some(
          (c) =>
            c.url.pathname === "/api/v1/assets/discovery-pool" &&
            c.url.searchParams.get("search") === "liq" &&
            c.url.searchParams.get("exchange") === "XNAS" &&
            c.url.searchParams.get("outcome") === "DISQUALIFIED",
        ),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(stub.calls.some((c) => c.url.searchParams.get("offset") === "50")).toBe(true));
  });

  it("starts discovery through the operator-gated endpoint and reports an active job", async () => {
    const stub = stubApi({
      ...ROUTES,
      "/api/v1/discovery/run": (_url, init) =>
        init?.method === "POST" ? { body: { job: job({ status: "RUNNING" }), created: false } } : { status: 405, body: {} },
    });
    renderAt("/discovery");
    fireEvent.change(await screen.findByLabelText("Operator token"), { target: { value: "op-secret" } });
    fireEvent.click(screen.getByRole("button", { name: /Start discovery/ }));
    expect(await screen.findByText(/already running/)).toBeInTheDocument();
    const post = stub.calls.find((c) => c.url.pathname === "/api/v1/discovery/run");
    expect(JSON.parse(String(post?.init?.body))).toEqual({ mode: "INCREMENTAL", pipeline: true });
    expect(new Headers(post?.init?.headers).get("X-Atlas-Operator-Token")).toBe("op-secret");
  });

  it("offers no buy, sell or order controls", async () => {
    stubApi(ROUTES);
    renderAt("/discovery");
    await screen.findByRole("table", { name: "Instruments" });
    for (const button of screen.getAllByRole("button")) {
      expect(button.textContent ?? "").not.toMatch(/\b(buy|sell|order|trade|short it)\b/i);
    }
  });

  it("shows a load error with retry", async () => {
    stubApi({ ...ROUTES, "/api/v1/discovery/summary": { status: 503, body: { error: { code: "dependency_unavailable", message: "The database is unavailable." } } } });
    renderAt("/discovery");
    expect(await screen.findByText("The database is unavailable.")).toBeInTheDocument();
  });
});

describe("Discovered instrument", () => {
  it("explains why an instrument was disqualified, with measurements and history", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      [`/api/v1/assets/${CHEAP_ID}`]: { body: CHEAP_DETAIL },
      [`/api/v1/assets/${CHEAP_ID}/qualification`]: { body: QUALIFICATION },
      [`/api/v1/assets/${CHEAP_ID}/history`]: { body: HISTORY },
    });
    renderAt(`/discovery/assets/${CHEAP_ID}`);
    expect(await screen.findByRole("heading", { name: "CHEAP" })).toBeInTheDocument();
    const rules = await screen.findByRole("table", { name: "Qualification rules" });
    expect(within(rules).getByRole("row", { name: "min_price" })).toHaveTextContent("last valid close 2 USD (fails >= 5 USD)");
    expect(within(rules).getByRole("row", { name: "max_spread" })).toHaveTextContent("optional");
    expect(await screen.findByRole("table", { name: "Measurements" })).toHaveTextContent("900000.00");
    expect(await screen.findByRole("table", { name: "Lifecycle events" })).toHaveTextContent("not a listing date");
    expect(screen.getByText("not reported")).toBeInTheDocument(); // country
    expect(screen.getByText(/borrow must be revalidated/)).toBeInTheDocument();
    expect(screen.getByText(/assumed to cover the provider's history/)).toBeInTheDocument();
  });

  it("shows ranking factors with the not-a-signal explanation", async () => {
    stubApi({
      ...LAYOUT_ROUTES,
      [`/api/v1/assets/${LIQ_ID}`]: { body: LIQ_DETAIL },
      [`/api/v1/assets/${LIQ_ID}/qualification`]: { body: { ...QUALIFICATION, metrics: [] } },
      [`/api/v1/assets/${LIQ_ID}/history`]: { body: HISTORY },
    });
    renderAt(`/discovery/assets/${LIQ_ID}`);
    const factors = await screen.findByRole("table", { name: "Ranking factors" });
    expect(within(factors).getByRole("row", { name: "liquidity" })).toHaveTextContent("100.0");
    expect(within(factors).getByRole("row", { name: "spread_quality" })).toHaveTextContent("not scored");
    expect(screen.getByText(/not a trading signal/)).toBeInTheDocument();
    expect(screen.getByText("rank 1 · score 82.1 / 100")).toBeInTheDocument();
  });
});
