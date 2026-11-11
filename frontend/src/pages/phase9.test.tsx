import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { equityPoints } from "../components/overview";
import { latestByStrategy } from "../components/strategyEvidence";
import { DisplayZoneProvider } from "../lib/time";
import { NAV_GROUPS, NAV_SECTIONS } from "../navigation";
import {
  CONFIGURATION,
  installFakeLive,
  LAYOUT_ROUTES,
  METRICS,
  PIPELINE_HEALTH,
  snapshots,
  stubApi,
} from "../test/marketFixtures";
import { PHASE9_ROUTES, WORKERS_FIXTURE } from "../test/systemFixtures";
import type { PortfolioSnapshot } from "../types/portfolio";
import type { ValidationRun } from "../types/validation";

// lightweight-charts needs a canvas, which jsdom lacks.
vi.mock("lightweight-charts", () => ({
  AreaSeries: "Area",
  LineSeries: "Line",
  CandlestickSeries: "Candlestick",
  HistogramSeries: "Histogram",
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Dotted: 1 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  createChart: () => ({
    addSeries: () => ({ setData: () => undefined, update: () => undefined, priceScale: () => ({ applyOptions: () => undefined }) }),
    priceScale: () => ({ applyOptions: () => undefined }),
    applyOptions: () => undefined,
    subscribeCrosshairMove: () => undefined,
    timeScale: () => ({ fitContent: () => undefined }),
    remove: () => undefined,
  }),
  createSeriesMarkers: () => ({ setMarkers: () => undefined }),
}));

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
  ...PHASE9_ROUTES,
  "/api/v1/market-data/snapshots": { body: snapshots() },
  "/api/v1/market-data/pipeline-health": { body: PIPELINE_HEALTH },
  "/api/v1/market-data/metrics": { body: METRICS },
  "/api/v1/market-data/configuration": { body: CONFIGURATION },
  "/api/v1/market-data/providers": { body: [] },
  "/api/v1/market-data/quality-events": { body: [] },
  "/api/v1/market-data/ingestion-jobs": { body: [] },
};

// ---- TEST FIXTURES: a simulated paper account (not real data)
const LIVE = {
  as_of: "2026-03-10T15:00:00Z",
  recorded: false,
  mark_status: "COMPLETE",
  mark_issues: [],
  functional_currency: "USD",
  equity: "101250.000000",
  contributed_capital: "100000.00",
  cash: "60000.50",
  available_capital: "59000.25",
  long_exposure: "41249.50",
  short_exposure: "0",
  gross_exposure: "41249.50",
  net_exposure: "41249.50",
  margin_used: "0",
  open_positions: 3,
  daily_loss: "125.50",
  weekly_loss: "-300.00",
  drawdown: "500.00",
  peak_equity: "101750.00",
  of_equity: { gross_exposure: "0.4074" },
};

const RISK_STATE = {
  account: { id: "acct-1", name: "primary", purpose: "PRIMARY", base_currency: "USD" },
  trading_halted: false,
  active_breakers: [],
  kill_switch: "CLEAR",
  breakers: [{ breaker: "DAILY_LOSS" }],
  limits: { configured: false, active: null },
  live: LIVE,
  latest_snapshot: null,
  assessment_counts: { PASS_FOR_RESERVATION: 4, REJECTED: 2, INSUFFICIENT_DATA: 1, RISK_HALTED: 0 },
  reserved_exposure: { reserved: { active_count: 1, gross_exposure: "5000.00" } },
  disclaimer: "fixture",
};

const LEDGER = {
  account: { id: "acct-1" },
  figures: {
    functional_currency: "USD",
    equity: "101000.00",
    contributed_capital: "100000.00",
    cash: "60000.50",
    restricted_cash: "0",
    realized_pnl: "750.25",
    unrealized_pnl: "-120.10",
    available_capital: "59000.25",
    gross_exposure: "41000.00",
    net_exposure: "41000.00",
  },
  positions: { long: 3, short: 0 },
};

beforeEach(() => {
  installFakeLive();
  window.localStorage.clear();
});

describe("Overview (Phase 9)", () => {
  it("says the paper account does not exist instead of showing figures", async () => {
    stubApi(ROUTES);
    renderAt("/");
    expect(await screen.findByText("No paper account yet")).toBeInTheDocument();
    const panel = screen.getByText("Paper account").closest("section")!;
    expect(panel.textContent).not.toMatch(/USD/);
    expect(panel.textContent).toMatch(/accounting\.cli init/);
  });

  it("shows live equity, P/L signs and exposure from the risk engine and ledger", async () => {
    stubApi({ ...ROUTES, "/api/v1/risk/state": { body: RISK_STATE }, "/api/v1/portfolio/overview": { body: LEDGER } });
    renderAt("/");
    const panel = (await screen.findByText("Total equity")).closest("section")!;
    const p = within(panel);
    expect(p.getByText("101,250.00 USD")).toBeInTheDocument();
    // daily_loss 125.50 (a loss) is a negative P/L; weekly_loss −300 is a gain.
    expect(p.getByText("−125.50 USD")).toBeInTheDocument();
    expect(p.getByText("+300.00 USD")).toBeInTheDocument();
    expect(p.getByText("+1.25%")).toBeInTheDocument(); // (101250 − 100000) / 100000
    expect(p.getByText("+1,250.00 USD")).toBeInTheDocument();
    expect(p.getByText("40.74% of equity")).toBeInTheDocument();
    expect(p.getByText("0.49% from peak")).toBeInTheDocument();
    expect(p.getByText("+750.25 USD")).toBeInTheDocument();
    expect(p.getByText("−120.10 USD")).toBeInTheDocument();
    expect(p.getByText("Simulated")).toBeInTheDocument();
    const risk = (await screen.findByText("Not halted")).closest("section")!;
    expect(risk).toHaveTextContent("engine defaults (no stored configuration)");
    expect(risk).toHaveTextContent("4 passed · 2 rejected · 1 insufficient");
  });

  it("warns when marks are stale", async () => {
    const stale = { ...RISK_STATE, live: { ...LIVE, mark_status: "STALE", mark_issues: [{ kind: "STALE_PRICE" }] } };
    stubApi({ ...ROUTES, "/api/v1/risk/state": { body: stale }, "/api/v1/portfolio/overview": { body: LEDGER } });
    renderAt("/");
    expect(await screen.findByText(/Marks are STALE/)).toBeInTheDocument();
  });

  it("summarizes worker health without calling silent workers running", async () => {
    stubApi(ROUTES);
    renderAt("/");
    const panel = (await screen.findByText("1 Stale heartbeat")).closest("section")!;
    expect(within(panel).getByText("1 Running")).toBeInTheDocument();
    expect(within(panel).getByText("1 No heartbeat")).toBeInTheDocument();
    expect(panel).toHaveTextContent("Market-data recovery: stale heartbeat");
  });
});

describe("System Health (Phase 9)", () => {
  it("lists every worker with its derived health, and collects recent errors", async () => {
    stubApi({
      ...ROUTES,
      "/api/v1/market-data/quality-events": {
        body: [
          { id: 1, event_type: "OUTLIER", severity: "ERROR", provider: "alpaca", feed: "iex", asset_id: null, provider_symbol: "AAPL", timeframe: null, interval_start: null, interval_end: null, detected_at: "2026-03-10T13:00:00Z", resulting_status: null, message: "price spike (fixture)", details: {} },
          { id: 2, event_type: "NOTE", severity: "INFO", provider: "alpaca", feed: "iex", asset_id: null, provider_symbol: "SPY", timeframe: null, interval_start: null, interval_end: null, detected_at: "2026-03-10T13:30:00Z", resulting_status: null, message: "informational (fixture)", details: {} },
        ],
      },
    });
    renderAt("/system-health");
    const table = await screen.findByRole("table", { name: "Background workers" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Running");
    expect(rows[1]).toHaveTextContent("Stale heartbeat");
    expect(rows[2]).toHaveTextContent("No heartbeat");
    expect(rows[2]).toHaveTextContent("none recorded");

    const errors = await screen.findByRole("table", { name: "Recent errors" });
    expect(errors).toHaveTextContent("provider timeout (fixture)");
    expect(errors).toHaveTextContent("price spike (fixture)");
    expect(errors).not.toHaveTextContent("informational (fixture)");
    expect(screen.getByText("No recovery jobs recorded")).toBeInTheDocument();
  });

  it("shows one alert and hides worker panels when the API is unreachable", async () => {
    stubApi({ ...ROUTES, "/health": "network-error", "/ready": "network-error" });
    renderAt("/system-health");
    expect(await screen.findByRole("alert")).toHaveTextContent("Atlas API is unreachable");
    expect(screen.queryByRole("table", { name: "Background workers" })).toBeNull();
    expect(screen.getByText("Disconnected from the Atlas API.")).toBeInTheDocument();
  });
});

describe("Settings (Phase 9)", () => {
  it("shows system information, safeguards and switches read-only", async () => {
    stubApi(ROUTES);
    renderAt("/settings");
    expect(await screen.findByText("Paper-only safeguards")).toBeInTheDocument();
    expect(await screen.findByText(/No paper account yet:/)).toBeInTheDocument();
    expect(screen.getByText(/No execution-model version has been recorded yet/)).toBeInTheDocument();
    const switches = screen.getByText("Worker switches").closest("section")!;
    expect(within(switches).getByText("Market-data stream")).toBeInTheDocument();
    const main = screen.getByRole("main");
    for (const name of [/save/i, /apply/i, /enable/i, /disable/i, /submit/i]) {
      expect(within(main).queryByRole("button", { name })).toBeNull();
    }
  });
});

describe("Markets (Phase 9)", () => {
  it("shows the active watchlist and an honest unavailable regime", async () => {
    stubApi({
      ...ROUTES,
      "/api/v1/assets/watchlist": {
        body: {
          items: [{ asset_id: "a-1", symbol: "MSFT", name: "Microsoft (fixture)", instrument_type: "STOCK", exchange: "NASDAQ", shortable: true, watchlist_rank: 1, watchlist_score: "0.8123" }],
          total: 1,
          limit: 50,
          offset: 0,
          notes: [],
        },
      },
    });
    renderAt("/markets");
    const table = await screen.findByRole("table", { name: "Active watchlist" });
    expect(within(table).getByText("MSFT")).toBeInTheDocument();
    expect(within(table).getByText("0.812")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Open" })).toHaveAttribute("href", "/markets/MSFT");
    expect(await screen.findByText(/No market-intelligence classification is recorded yet/)).toBeInTheDocument();
  });
});

describe("Navigation and shell (Phase 9)", () => {
  it("groups every section under a labelled group", async () => {
    stubApi(ROUTES);
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Dashboard sections" });
    for (const group of NAV_GROUPS) {
      const list = within(nav).getByRole("list", { name: group });
      const expected = NAV_SECTIONS.filter((s) => s.group === group).map((s) => s.title);
      expect(within(list).getAllByRole("link").map((a) => a.textContent)).toEqual(expected);
    }
  });

  it("shows a disconnected banner on every page when the API stops answering", async () => {
    stubApi({ ...ROUTES, "/health": "network-error", "/ready": "network-error" });
    renderAt("/markets");
    expect(await screen.findByText("Disconnected from the Atlas API.")).toBeInTheDocument();
  });
});

describe("Every route (Phase 9)", () => {
  // Layout endpoints answer; every page endpoint fails with 503. Each page must still render
  // its heading and say the data could not be loaded, never crash or show invented numbers.
  const sections = NAV_SECTIONS.map((s) => [s.title, s.path] as const);
  it.each(sections)("%s renders with its data unavailable", async (_title, path) => {
    const layout = LAYOUT_ROUTES as Record<string, { body: unknown }>;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = new URL(typeof input === "string" ? input : input.toString(), "http://localhost");
      const route = layout[url.pathname];
      const [status, body] = route ? [200, route.body] : [503, { error: { code: "dependency_unavailable", message: "fixture outage" } }];
      return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;
    renderAt(path);
    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
    expect((await screen.findAllByText(/fixture outage|Could not load/)).length).toBeGreaterThan(0);
    expect(screen.getByRole("status", { name: /Paper trading mode/ })).toBeInTheDocument();
  });
});

describe("Phase 9 helpers", () => {
  const snap = (as_of: string, equity: string, valuation_status: PortfolioSnapshot["valuation_status"] = "COMPLETE") =>
    ({ as_of, equity, valuation_status }) as PortfolioSnapshot;

  it("draws only complete recorded checkpoints, ascending and unique", () => {
    const points = equityPoints([
      snap("2026-03-10T15:00:00Z", "101000"),
      snap("2026-03-10T14:00:00Z", "100500", "STALE"),
      snap("2026-03-10T13:00:00Z", "100000"),
    ]);
    expect(points).toEqual([
      [Date.parse("2026-03-10T13:00:00Z") / 1000, 100000],
      [Date.parse("2026-03-10T15:00:00Z") / 1000, 101000],
    ]);
  });

  it("picks the newest completed single-strategy walk-forward run", () => {
    const run = (id: string, finished: string, extra: Partial<ValidationRun> = {}) =>
      ({ id, kind: "WALK_FORWARD", status: "COMPLETED", strategies: ["momentum"], finished_at: finished, created_at: finished, ...extra }) as ValidationRun;
    const latest = latestByStrategy([
      run("old", "2026-10-01T00:00:00Z"),
      run("new", "2026-11-01T00:00:00Z"),
      run("failed", "2026-11-05T00:00:00Z", { status: "FAILED" }),
      run("combined", "2026-11-06T00:00:00Z", { strategies: ["momentum", "breakout"] }),
    ]);
    expect(latest.get("momentum")?.id).toBe("new");
    expect(latest.has("breakout")).toBe(false);
  });
});

it("fixture worker report is internally consistent", () => {
  const counted = WORKERS_FIXTURE.workers.reduce<Record<string, number>>((acc, w) => ({ ...acc, [w.health]: (acc[w.health] ?? 0) + 1 }), {});
  expect(counted).toEqual(WORKERS_FIXTURE.summary);
});
