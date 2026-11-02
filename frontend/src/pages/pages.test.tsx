import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import {
  AAPL_SNAPSHOT,
  COMPLETENESS,
  CONFIGURATION,
  FakeEventSource,
  LAYOUT_ROUTES,
  METRICS,
  PIPELINE_HEALTH,
  candle,
  candlesResponse,
  ingestionJob,
  installFakeLive,
  snapshots,
  stubApi,
} from "../test/marketFixtures";
import type { DataGap } from "../types/marketData";

// lightweight-charts draws on canvas, which jsdom lacks: record what the app asks of it.
const chartMock = vi.hoisted(() => {
  const state = {
    charts: 0,
    removed: 0,
    series: [] as { kind: string; setData: unknown[][]; updates: unknown[] }[],
    markers: [] as unknown[][],
  };
  const makeSeries = (kind: string) => {
    const record = { kind, setData: [] as unknown[][], updates: [] as unknown[] };
    state.series.push(record);
    return {
      setData: (data: unknown[]) => record.setData.push(data),
      update: (bar: unknown) => record.updates.push(bar),
      priceScale: () => ({ applyOptions: () => undefined }),
      createPriceLine: () => ({ applyOptions: () => undefined }),
      removePriceLine: () => undefined,
    };
  };
  return { state, makeSeries };
});

vi.mock("lightweight-charts", () => ({
  CandlestickSeries: "Candlestick",
  HistogramSeries: "Histogram",
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Dotted: 1 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  createChart: () => {
    chartMock.state.charts += 1;
    return {
      addSeries: (kind: string) => chartMock.makeSeries(kind),
      priceScale: () => ({ applyOptions: () => undefined }),
      applyOptions: () => undefined,
      subscribeCrosshairMove: () => undefined,
      timeScale: () => ({ fitContent: () => undefined }),
      remove: () => {
        chartMock.state.removed += 1;
      },
    };
  },
  createSeriesMarkers: () => ({ setMarkers: (m: unknown[]) => chartMock.state.markers.push(m) }),
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

const BASE_ROUTES = {
  ...LAYOUT_ROUTES,
  "/api/v1/market-data/snapshots": { body: snapshots() },
  "/api/v1/market-data/pipeline-health": { body: PIPELINE_HEALTH },
  "/api/v1/market-data/metrics": { body: METRICS },
  "/api/v1/market-data/configuration": { body: CONFIGURATION },
  "/api/v1/market-data/providers": { body: [] },
  "/api/v1/market-data/assets": { body: [AAPL_SNAPSHOT.asset] },
  "/api/v1/market-data/quality-events": { body: [] },
  "/api/v1/market-data/trades": { body: { trades: [], next_after_id: null, notes: [] } },
  "/api/v1/market-data/completeness": { body: COMPLETENESS },
  "/api/v1/market-data/gaps": { body: [] },
  "/api/v1/market-data/stream/gaps": { body: [] },
  "/api/v1/market-data/audit-events": { body: [] },
  "/api/v1/market-data/ingestion-jobs": { body: [] },
  "/api/v1/market-data/recovery-jobs": { body: [] },
};

beforeEach(() => {
  installFakeLive();
  chartMock.state.charts = 0;
  chartMock.state.removed = 0;
  chartMock.state.series = [];
  chartMock.state.markers = [];
  window.localStorage.clear();
});

// ------------------------------------------------------------------ overview
describe("Overview", () => {
  it("renders backend status and monitored assets without inventing values", async () => {
    stubApi(BASE_ROUTES);
    renderAt("/");

    const table = await screen.findByRole("table", { name: "Monitored assets" });
    const aapl = within(table).getByRole("row", { name: "Open AAPL" });
    expect(aapl).toHaveTextContent("101.50");
    expect(aapl).toHaveTextContent("+1.50");
    expect(aapl).toHaveTextContent("+1.50%");
    expect(aapl).toHaveTextContent("100.00");
    expect(aapl).toHaveTextContent("IEX · single venue");
    const spy = within(table).getByRole("row", { name: "Open SPY" });
    expect(spy).toHaveTextContent("No data");
    expect(spy.textContent).not.toMatch(/\d+\.\d{2}/); // no price for SPY at all
    expect(screen.getByText("Pipeline FAILING")).toBeInTheDocument();
    expect(screen.getByText("NYSE open")).toBeInTheDocument();
    expect(await screen.findByText("Failed ingestion")).toBeInTheDocument();
  });

  it("shows loading, then an error with retry", async () => {
    let fail = true;
    const stub = stubApi({
      ...BASE_ROUTES,
      "/api/v1/market-data/snapshots": () => (fail ? { status: 500, body: { error: { code: "internal_error", message: "An internal error occurred." } } } : { body: snapshots() }),
    });
    renderAt("/");
    expect(screen.getByText("Loading assets…")).toBeInTheDocument();
    expect(await screen.findByText("An internal error occurred.")).toBeInTheDocument();
    fail = false;
    fireEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("row", { name: "Open AAPL" })).toBeInTheDocument();
    expect(stub.calls.filter((c) => c.url.pathname.endsWith("/snapshots")).length).toBeGreaterThanOrEqual(2);
  });

  it("shows an empty state when no assets are monitored", async () => {
    stubApi({ ...BASE_ROUTES, "/api/v1/market-data/snapshots": { body: snapshots([]) } });
    renderAt("/");
    expect(await screen.findByText("No monitored assets")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ markets
describe("Markets", () => {
  it("searches, sorts, hides columns and opens an asset", async () => {
    stubApi(BASE_ROUTES);
    renderAt("/markets");
    const table = await screen.findByRole("table", { name: "Monitored assets" });

    fireEvent.change(screen.getByRole("searchbox", { name: "Search by symbol or name" }), { target: { value: "spdr" } });
    expect(within(table).queryByRole("row", { name: "Open AAPL" })).toBeNull();
    expect(within(table).getByRole("row", { name: "Open SPY" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search by symbol or name" }), { target: { value: "ZZZ" } });
    expect(screen.getByText('No asset matches "ZZZ"')).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Search by symbol or name" }), { target: { value: "" } });

    const table2 = screen.getByRole("table", { name: "Monitored assets" });
    const lastHeader = within(table2).getByRole("columnheader", { name: /Last/ });
    fireEvent.click(within(lastHeader).getByRole("button"));
    expect(lastHeader).toHaveAttribute("aria-sort", "ascending");
    // Missing prices sort last in both directions.
    expect(within(table2).getAllByRole("row").slice(1).map((r) => r.getAttribute("aria-label"))).toEqual(["Open AAPL", "Open SPY"]);
    fireEvent.click(within(lastHeader).getByRole("button"));
    expect(lastHeader).toHaveAttribute("aria-sort", "descending");
    expect(within(table2).getAllByRole("row").slice(1).map((r) => r.getAttribute("aria-label"))).toEqual(["Open AAPL", "Open SPY"]);

    fireEvent.click(screen.getByRole("button", { name: "Columns" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Name" }));
    expect(within(table2).queryByRole("columnheader", { name: /Name/ })).toBeNull();

    stubApi({ ...BASE_ROUTES, "/api/v1/market-data/candles": { body: candlesResponse([]) } });
    fireEvent.click(within(table2).getByRole("row", { name: "Open AAPL" }));
    expect(await screen.findByRole("heading", { name: "AAPL" })).toBeInTheDocument();
  });

  it("flags data-quality warnings and open gaps per asset", async () => {
    stubApi(BASE_ROUTES);
    renderAt("/markets");
    const row = await screen.findByRole("row", { name: "Open AAPL" });
    expect(row).toHaveTextContent("2 evt");
  });
});

// ------------------------------------------------------------------ asset detail
// Chart ranges are measured back from the real clock ("last 4 days"): recent minutes.
const MINUTE_BASE = Math.floor(Date.now() / 60_000) * 60_000 - 30 * 60_000;
const minute = (m: number) => new Date(MINUTE_BASE + m * 60_000).toISOString().replace(".000Z", "Z");

function candleSeries() {
  return chartMock.state.series.filter((s) => s.kind === "Candlestick");
}

describe("Asset detail", () => {
  it("draws stored candles ordered and de-duplicated, with source and feed shown", async () => {
    stubApi({
      ...BASE_ROUTES,
      "/api/v1/market-data/candles": {
        body: candlesResponse([
          candle(minute(2), "101.00"),
          candle(minute(1), "100.50"),
          candle(minute(2), "101.20", { revision: 1, source: "LIVE_STREAM", observation_kind: "LIVE_UPDATED" }),
        ]),
      },
    });
    renderAt("/markets/AAPL");

    expect(await screen.findByRole("heading", { name: "AAPL" })).toBeInTheDocument();
    await waitFor(() => expect(candleSeries()[0]?.setData.length).toBeGreaterThan(0));
    const drawn = candleSeries()[0]?.setData.at(-1) as { time: number; close: number }[];
    expect(drawn.map((d) => [d.time, d.close])).toEqual([
      [Date.parse(minute(1)) / 1000, 100.5],
      [Date.parse(minute(2)) / 1000, 101.2],
    ]);
    expect(chartMock.state.charts).toBe(1);
    expect(screen.getByText("2 candles")).toBeInTheDocument();
    expect(screen.getByText("1 revised (latest revision shown)")).toBeInTheDocument();
    expect(screen.getAllByText("IEX · single venue").length).toBeGreaterThan(0);
    // One-sided quote: a zero ask is "no ask", never a $0 price.
    expect(screen.getByText("no ask")).toBeInTheDocument();
    // Missing intervals from the backend completeness check are marked on the chart.
    await waitFor(() => expect(chartMock.state.markers.at(-1)?.length).toBe(1));
  });

  it("requests the selected timeframe and shows an empty state with a retrieval link", async () => {
    const stub = stubApi({
      ...BASE_ROUTES,
      "/api/v1/market-data/candles": (url) =>
        url.searchParams.get("timeframe") === "5Min" ? { body: candlesResponse([], "5Min") } : { body: candlesResponse([candle(minute(1), "100.50")]) },
    });
    renderAt("/markets/AAPL");
    await screen.findByText("1 candles");

    fireEvent.click(screen.getByRole("radio", { name: "5Min" }));
    expect(await screen.findByText(/No stored 5Min candles for AAPL/)).toBeInTheDocument();
    const requested = stub.calls.filter((c) => c.url.pathname.endsWith("/candles")).map((c) => c.url.searchParams.get("timeframe"));
    expect(requested).toContain("5Min");
    expect(screen.getByRole("link", { name: "Request this history from the provider" }).getAttribute("href")).toMatch(/timeframe=5Min/);
    expect(screen.getByText(/5Min is not streamed by the provider/)).toBeInTheDocument();
  });

  it("applies live bars incrementally and cleans up on unmount", async () => {
    stubApi({ ...BASE_ROUTES, "/api/v1/market-data/candles": { body: candlesResponse([candle(minute(1), "100.50")]) } });
    const view = renderAt("/markets/AAPL");
    await screen.findByText("1 candles");
    const sources = FakeEventSource.open();
    expect(sources).toHaveLength(1); // one shared connection for the whole page
    const source = sources[0] as FakeEventSource;
    act(() => source.emit("hello", { symbols: ["AAPL", "SPY"] }));

    const bar = {
      id: 7,
      symbol: "AAPL",
      timeframe: "1Min",
      event_time: minute(2),
      open: "100.50",
      high: "101.00",
      low: "100.40",
      close: "100.90",
      volume: "300",
      vwap: null,
      trade_count: 4,
      is_complete: true,
      quality_status: "VALID",
      revision: 0,
      source: "LIVE_STREAM",
      observation_kind: "LIVE_INITIAL",
      ingested_at: minute(3),
    };
    act(() => source.emit("bar", bar));
    act(() => source.emit("bar", bar)); // repeated: no duplicate candle
    act(() => source.emit("bar", { ...bar, symbol: "SPY" })); // other asset: ignored
    await screen.findByText("2 candles");
    expect(candleSeries()[0]?.updates).toHaveLength(1);
    expect(screen.getByText(/1 received/)).toBeInTheDocument();

    view.unmount();
    expect(chartMock.state.removed).toBe(1);
    expect(FakeEventSource.open()).toHaveLength(0);
  });

  it("explains an asset that is not monitored", async () => {
    stubApi({ ...BASE_ROUTES, "/api/v1/market-data/snapshots": { body: snapshots([]) } });
    renderAt("/markets/MSFT");
    expect(await screen.findByText("MSFT is not a monitored asset")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ data health
const QUOTE_GAP: DataGap = {
  id: 3,
  asset_id: AAPL_SNAPSHOT.asset.asset_id,
  provider_symbol: "AAPL",
  provider: "alpaca",
  feed: "iex",
  data_type: "QUOTE",
  timeframe: null,
  gap_start: "2026-03-10T13:31:00Z",
  gap_end: "2026-03-10T15:48:00Z",
  cause: "WORKER_NOT_RUNNING",
  status: "OPEN",
  stream_gap_id: 1,
  parent_gap_id: null,
  detected_at: "2026-03-10T15:50:00Z",
  resolved_at: null,
  attempts: 0,
  records_retrieved: 0,
  records_recovered: 0,
  records_duplicate: 0,
  records_revised: 0,
  records_rejected: 0,
  resolution: null,
  details: { reason: "recovery_disabled" },
};

describe("Data Health", () => {
  it("shows each pipeline stage separately and keeps quote gaps outstanding", async () => {
    stubApi({ ...BASE_ROUTES, "/api/v1/market-data/gaps": { body: [QUOTE_GAP] } });
    renderAt("/data-health");

    const stages = await screen.findByRole("table", { name: "Pipeline stages" });
    const persistence = within(stages).getByRole("row", { name: /Database persistence/ });
    expect(persistence).toHaveTextContent("FAILING");
    expect(within(stages).getByRole("row", { name: /Provider connectivity/ })).toHaveTextContent("OK");
    expect(screen.getByText("Pipeline FAILING")).toBeInTheDocument();
    const gaps = await screen.findByRole("table", { name: "Missing intervals" });
    expect(within(gaps).getByText("Open")).toBeInTheDocument();
    expect(within(gaps).getByText("recovery_disabled")).toBeInTheDocument();
    expect(screen.getByText(/Quote gaps stay outstanding/)).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Pipeline stages" })).toBeInTheDocument();
    expect(screen.getAllByText("Provider interruptions")).toHaveLength(2); // stat and panel
    expect(await screen.findByText("DB interruptions")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ ingestion
describe("Ingestion", () => {
  it("lists jobs with their counters and validates and submits requests", async () => {
    const posted: unknown[] = [];
    const stub = stubApi({
      ...BASE_ROUTES,
      "/api/v1/market-data/ingestion-jobs": { body: [ingestionJob({ status: "PARTIALLY_COMPLETED", counters: { ...ingestionJob().counters, records_rejected: 3 } })] },
      "/api/v1/market-data/historical-fetch": (_url, init) => {
        posted.push(JSON.parse(String(init?.body)));
        return { status: 202, body: ingestionJob({ status: "PENDING" }) };
      },
      [`/api/v1/market-data/ingestion-jobs/${ingestionJob().job_id}`]: { body: ingestionJob({ status: "RUNNING" }) },
    });
    renderAt("/ingestion?symbol=AAPL&timeframe=5Min");

    const jobs = await screen.findByRole("table", { name: "Historical retrieval jobs" });
    expect(within(jobs).getByText("Partially completed")).toBeInTheDocument();
    expect(within(jobs).getByText("385")).toBeInTheDocument();

    const form = screen.getByRole("form", { name: "Historical retrieval request" });
    fireEvent.change(within(form).getByLabelText("Start date"), { target: { value: "2026-03-09" } });
    fireEvent.change(within(form).getByLabelText("End date"), { target: { value: "2026-03-08" } });
    expect(within(form).getByText("The end must be after the start.")).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: /Submit request/ })).toBeDisabled();

    fireEvent.change(within(form).getByLabelText("End date"), { target: { value: "2026-03-09" } });
    fireEvent.click(within(form).getByRole("button", { name: /Submit request/ }));
    expect(await screen.findByText("Accepted: the backend queued the retrieval.")).toBeInTheDocument();
    expect(posted).toEqual([{ symbols: ["AAPL"], timeframe: "5Min", start: "2026-03-09T00:00:00.000Z", end: "2026-03-09T23:59:00.000Z" }]);
    expect(await screen.findByText("Running: pages are being retrieved from the provider.", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(stub.calls.some((c) => c.url.pathname.endsWith(ingestionJob().job_id))).toBe(true);
  });

  it("shows the backend's validation message when a request is refused", async () => {
    stubApi({
      ...BASE_ROUTES,
      "/api/v1/market-data/historical-fetch": { status: 422, body: { error: { code: "invalid_request", message: "range exceeds the provider's limit" } } },
    });
    renderAt("/ingestion?symbol=AAPL");
    const form = await screen.findByRole("form", { name: "Historical retrieval request" });
    fireEvent.change(within(form).getByLabelText("Start date"), { target: { value: "2026-03-02" } });
    fireEvent.change(within(form).getByLabelText("End date"), { target: { value: "2026-03-03" } });
    fireEvent.click(within(form).getByRole("button", { name: /Submit request/ }));
    expect(await within(form).findByText(/range exceeds the provider's limit \(HTTP 422\)/)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ settings
describe("Settings", () => {
  it("shows configuration status only and switches the display time zone", async () => {
    stubApi(BASE_ROUTES);
    renderAt("/settings");

    expect(await screen.findByText("Configured")).toBeInTheDocument();
    expect(screen.getByText("status only; values are never sent to the browser")).toBeInTheDocument();
    expect(screen.getByRole("main").textContent).not.toMatch(/secret|api[_ ]key/i);
    const header = screen.getByRole("banner");
    expect(within(header).getByText(/ UTC/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Exchange (ET)" }));
    expect(within(header).getByText(/ ET/)).toBeInTheDocument();
  });
});
