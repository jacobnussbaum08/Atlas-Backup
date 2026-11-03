import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import {
  CATALOG,
  CATALOG_WITH_FIXTURE,
  EMPTY_EVALUATIONS,
  EMPTY_PROPOSALS,
  EVALUATIONS,
  FEATURES,
  MOMENTUM_DETAIL,
  PROPOSALS,
} from "../test/strategyFixtures";

function renderAt(path: string) {
  return render(
    <DisplayZoneProvider initial="UTC">
      <MemoryRouter initialEntries={[path]}>
        <App pollIntervalMs={0} />
      </MemoryRouter>
    </DisplayZoneProvider>,
  );
}

const EMPTY_ROUTES = {
  ...LAYOUT_ROUTES,
  "/api/v1/strategies": { body: CATALOG },
  "/api/v1/strategies/proposals": { body: EMPTY_PROPOSALS },
  "/api/v1/strategies/evaluations": { body: EMPTY_EVALUATIONS },
  "/api/v1/strategies/features": { body: FEATURES },
};

beforeEach(() => {
  installFakeLive();
});

describe("Strategy Laboratory", () => {
  it("lists the six families with version, markets, timeframes and status", async () => {
    stubApi(EMPTY_ROUTES);
    renderAt("/strategies");
    const catalog = await screen.findByRole("list", { name: "Strategy catalog" });
    const cards = within(catalog).getAllByRole("listitem");
    expect(cards).toHaveLength(6);
    const momentum = within(cards[3]!);
    expect(momentum.getByRole("link", { name: /Momentum/ })).toHaveAttribute("href", "/strategies/momentum");
    expect(momentum.getByText("v1 · momentum-interface-1")).toBeInTheDocument();
    expect(momentum.getByText("Equity, Etf")).toBeInTheDocument();
    expect(momentum.getByText("Disabled")).toBeInTheDocument();
    expect(momentum.getByText("Interface only")).toBeInTheDocument();
    expect(momentum.getByText("2 required, 1 optional")).toBeInTheDocument();
    expect(within(cards[1]!).getByText("Bar close + News event")).toBeInTheDocument();
    expect(screen.getByText(/no evaluation logic yet \(Phase 4B\)/)).toBeInTheDocument();
  });

  it("shows honest empty states and no performance figures or execution controls", async () => {
    stubApi(EMPTY_ROUTES);
    renderAt("/strategies");
    expect(await screen.findByText("No proposals")).toBeInTheDocument();
    expect(await screen.findByText("No evaluations")).toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(main.textContent).not.toMatch(/win rate|profit factor|sharpe|drawdown|P\/L|expectancy/i);
    expect(main.textContent).toMatch(/No performance statistics are shown/);
    for (const name of [/buy/i, /sell/i, /submit/i, /execute/i, /enable/i]) {
      expect(within(main).queryByRole("button", { name })).toBeNull();
    }
  });

  it("shows proposals as hypotheses with uncalibrated strength, and evaluation outcomes", async () => {
    stubApi({
      ...EMPTY_ROUTES,
      "/api/v1/strategies": { body: CATALOG_WITH_FIXTURE },
      "/api/v1/strategies/proposals": { body: PROPOSALS },
      "/api/v1/strategies/evaluations": { body: EVALUATIONS },
    });
    renderAt("/strategies");
    const proposals = await screen.findByRole("table", { name: "Strategy proposals" });
    const [, row] = within(proposals).getAllByRole("row");
    const cells = within(row!);
    expect(cells.getByText("FXA")).toBeInTheDocument();
    expect(cells.getByText("Long")).toBeInTheDocument();
    expect(cells.getByText("119.8000")).toBeInTheDocument();
    expect(cells.getByText("0.5000")).toBeInTheDocument();
    expect(cells.getByText("Active")).toBeInTheDocument();
    expect(within(proposals).getByText("Strength")).toHaveAttribute(
      "title",
      "Uncalibrated score in [0, 1]; not a probability",
    );
    const evaluations = screen.getByRole("table", { name: "Evaluation history" });
    expect(within(evaluations).getByText("Skipped")).toBeInTheDocument();
    expect(within(evaluations).getByText("sma.v1(length=5)@1Day: missing inputs")).toBeInTheDocument();
    expect(within(evaluations).getByText("Historical · Reconstructed")).toBeInTheDocument();
    expect(screen.getByText("Logic implemented")).toBeInTheDocument();
  });

  it("filters proposals by status", async () => {
    const stub = stubApi({ ...EMPTY_ROUTES, "/api/v1/strategies/proposals": { body: PROPOSALS } });
    renderAt("/strategies");
    await screen.findByRole("table", { name: "Strategy proposals" });
    fireEvent.click(screen.getByRole("radio", { name: "Expired" }));
    await waitFor(() =>
      expect(
        stub.calls.some(
          (c) => c.url.pathname === "/api/v1/strategies/proposals" && c.url.searchParams.get("status") === "EXPIRED",
        ),
      ).toBe(true),
    );
  });

  it("shows the feature registry on request", async () => {
    stubApi(EMPTY_ROUTES);
    renderAt("/strategies");
    fireEvent.click(await screen.findByRole("button", { name: "Show definitions" }));
    const table = await screen.findByRole("table", { name: "Feature definitions" });
    expect(within(table).getByText("5 * length + 1")).toBeInTheDocument();
    expect(within(table).getByText("News")).toBeInTheDocument();
  });

  it("shows a strategy's parameters, required features, versions and empty status history", async () => {
    stubApi({
      ...EMPTY_ROUTES,
      "/api/v1/strategies/momentum": { body: MOMENTUM_DETAIL },
    });
    renderAt("/strategies/momentum");
    expect(await screen.findByRole("heading", { name: "Momentum" })).toBeInTheDocument();
    expect(screen.getByText("lookback_periods")).toBeInTheDocument();
    expect(screen.getByText("LONG, SHORT")).toBeInTheDocument();
    const features = screen.getByRole("list", { name: "Features on 1Day" });
    expect(within(features).getByText("rsi.v1(length=14)@1Day")).toBeInTheDocument();
    const versions = screen.getByRole("table", { name: "Strategy versions" });
    expect(within(versions).getByText("initial registration with default parameters")).toBeInTheDocument();
    expect(screen.getByText(/the strategy is disabled/)).toBeInTheDocument();
    expect(screen.getByText(/Changing a parameter creates a new immutable version/)).toBeInTheDocument();
  });

  it("is in the main navigation", async () => {
    stubApi(EMPTY_ROUTES);
    renderAt("/strategies");
    const nav = screen.getByRole("navigation");
    expect(within(nav).getByRole("link", { name: /Strategy Laboratory/ })).toHaveAttribute("href", "/strategies");
  });
});
