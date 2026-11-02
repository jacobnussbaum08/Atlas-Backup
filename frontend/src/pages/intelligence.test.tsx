import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { ASSETS, CORRELATIONS, EMPTY_MARKET, HISTORY, MARKET, RUN } from "../test/intelligenceFixtures";
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
  "/api/v1/intelligence/market": { body: MARKET },
  "/api/v1/intelligence/regimes/history": { body: HISTORY },
  "/api/v1/intelligence/assets": { body: ASSETS },
  "/api/v1/intelligence/correlations": { body: CORRELATIONS },
  "/api/v1/intelligence/runs": { body: [RUN] },
};

beforeEach(() => {
  installFakeLive();
});

describe("Market Intelligence", () => {
  it("shows broad-market classifications with their probabilities and participation", async () => {
    stubApi(ROUTES);
    renderAt("/intelligence");
    const market = await screen.findByLabelText("Broad market conditions");
    expect(within(market).getByText("Bullish 62%")).toBeInTheDocument();
    expect(within(market).getByRole("img", { name: /Trend probabilities: Bullish 62%/ })).toBeInTheDocument();
    expect(within(market).getByText("Normal 90%")).toBeInTheDocument();
    expect(within(market).getByText("Stable 70%")).toBeInTheDocument();
    expect(within(market).getByText("27.4")).toBeInTheDocument(); // ADX from the evidence
    expect(screen.getByText("79 of 82 qualified members")).toBeInTheDocument();
    expect(screen.getByText("47 / 30")).toBeInTheDocument();
    expect(screen.getByText(/not market-wide breadth/)).toBeInTheDocument();
    expect(screen.getByText(/single venue/)).toBeInTheDocument();
    expect(screen.getAllByText(/trading signal/).length).toBeGreaterThan(0);
  });

  it("switches to the weekly timeframe and shows insufficient data as uncertain", async () => {
    stubApi(ROUTES);
    renderAt("/intelligence");
    await screen.findByLabelText("Broad market conditions");
    fireEvent.click(screen.getAllByRole("radio", { name: "Weekly" })[0]!);
    const market = screen.getByLabelText("Broad market conditions");
    expect(within(market).getByText("Sideways 60%")).toBeInTheDocument();
    expect(within(market).getByText(/Insufficient data: 30 of 39 expected bars/)).toBeInTheDocument();
    expect(within(market).getByText(/daily data only/)).toBeInTheDocument();
  });

  it("says plainly when nothing has been computed", async () => {
    stubApi({ ...ROUTES, "/api/v1/intelligence/market": { body: EMPTY_MARKET } });
    renderAt("/intelligence");
    expect(await screen.findByText("No market intelligence yet")).toBeInTheDocument();
  });

  it("ranks instruments and shows missing values as missing", async () => {
    const stub = stubApi(ROUTES);
    renderAt("/intelligence");
    const table = await screen.findByRole("table", { name: "Instrument conditions" });
    const strong = within(table).getByRole("row", { name: /AAA/ });
    expect(strong).toHaveTextContent("+4.12%");
    expect(strong).toHaveTextContent("Bullish");
    const fresh = within(table).getByRole("row", { name: /NEWX/ });
    expect(fresh).toHaveTextContent("—");
    expect(fresh).not.toHaveTextContent("0.00%");
    expect(fresh).toHaveTextContent("Insufficient data");
    fireEvent.change(screen.getByLabelText("Sort by"), { target: { value: "adx" } });
    await waitFor(() =>
      expect(stub.calls.some((c) => c.url.pathname === "/api/v1/intelligence/assets" && c.url.searchParams.get("sort") === "adx")).toBe(true),
    );
  });

  it("draws the correlation matrix without inventing values for thin pairs", async () => {
    stubApi(ROUTES);
    renderAt("/intelligence");
    const matrix = await screen.findByRole("table", { name: "Correlation matrix" });
    expect(within(matrix).getAllByText(".73")).toHaveLength(2); // symmetric
    expect(within(matrix).getAllByText("-.21")).toHaveLength(2);
    expect(within(matrix).getAllByText("n/a")).toHaveLength(2);
    expect(within(matrix).getAllByTitle(/too few shared observations \(20\)/)).toHaveLength(2);
    expect(screen.getByText("1 without enough overlap")).toBeInTheDocument();
  });

  it("lists regime changes and marks reconstructed history", async () => {
    stubApi(ROUTES);
    renderAt("/intelligence");
    const changes = await screen.findByRole("table", { name: "Regime changes" });
    const row = within(changes).getByRole("row", { name: /Trend/ });
    expect(row).toHaveTextContent("Sideways");
    expect(row).toHaveTextContent("Bullish");
    expect(row).toHaveTextContent("Reconstructed");
    expect(screen.getByText(/4 decision times · hatched = reconstructed/)).toBeInTheDocument();
  });

  it("starts a reconstruction with the operator token", async () => {
    const stub = stubApi({
      ...ROUTES,
      "/api/v1/intelligence/runs": (_url, init) =>
        init?.method === "POST"
          ? { status: 202, body: { ...RUN, kind: "RECONSTRUCTION", status: "RUNNING" } }
          : { body: [RUN] },
    });
    renderAt("/intelligence");
    await screen.findByLabelText("Broad market conditions");
    fireEvent.click(screen.getByRole("radio", { name: "Reconstruct" }));
    fireEvent.change(screen.getByLabelText("Past sessions"), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText("Operator token"), { target: { value: "op-secret" } });
    fireEvent.click(screen.getByRole("button", { name: /Start run/ }));
    expect(await screen.findByText(/Reconstruction run started/)).toBeInTheDocument();
    const post = stub.calls.find((c) => c.init?.method === "POST");
    expect(JSON.parse(String(post?.init?.body))).toEqual({ kind: "RECONSTRUCTION", sessions: 30 });
    expect(new Headers(post?.init?.headers).get("X-Atlas-Operator-Token")).toBe("op-secret");
  });

  it("is in the navigation", async () => {
    stubApi(ROUTES);
    renderAt("/intelligence");
    expect(await screen.findByRole("link", { name: /Market Intelligence/ })).toBeInTheDocument();
  });
});
