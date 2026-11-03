import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../App";
import { DisplayZoneProvider } from "../lib/time";
import { installFakeLive, LAYOUT_ROUTES, stubApi } from "../test/marketFixtures";
import {
  CALENDAR,
  DETAIL,
  EMPTY_FEED,
  FEED,
  JOB,
  PROVIDER,
  SOURCES,
  STREAM,
  SUMMARY,
} from "../test/newsFixtures";

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
  "/api/v1/news/articles": { body: FEED },
  "/api/v1/news/summary": { body: SUMMARY },
  "/api/v1/news/sources": { body: SOURCES },
  "/api/v1/news/providers": { body: [PROVIDER] },
  "/api/v1/news/stream": { body: STREAM },
  "/api/v1/news/economic-calendar": { body: CALENDAR },
  "/api/v1/news/ingestion-jobs": { body: [JOB] },
};

beforeEach(() => {
  installFakeLive();
});

describe("News & Events", () => {
  it("shows headlines with publisher, time, origin, categories, assets and links", async () => {
    stubApi(ROUTES);
    renderAt("/news");
    const list = await screen.findByRole("list", { name: "News articles" });
    const [live, rest] = within(list).getAllByRole("listitem");
    const first = within(live!);
    // Untrusted text is rendered as text: the markup appears literally, no element is created.
    expect(first.getByRole("link", { name: /TEST FIXTURE Corp Beats Earnings Estimates <img/ })).toHaveAttribute(
      "href",
      "/news/articles/11111111-1111-1111-1111-111111111111",
    );
    expect(live!.querySelector("img")).toBeNull();
    expect(first.getByText("fixturewire")).toBeInTheDocument();
    expect(first.getByText("Fixture Desk")).toBeInTheDocument();
    expect(first.getByText("2026-01-02 14:00 UTC")).toBeInTheDocument();
    expect(first.getByText("Real-time")).toBeInTheDocument();
    expect(first.getByText("rev 2")).toBeInTheDocument();
    expect(first.getByText("Earnings")).toHaveAttribute("title", expect.stringMatching(/Keyword estimate \(unverified\)/));
    expect(first.getByText("Analyst ratings")).toBeInTheDocument();
    expect(first.getByRole("link", { name: "FXC" })).toHaveAttribute("href", "/discovery/assets/aaaaaaaa-0000-0000-0000-000000000001");
    expect(first.getByText("ZZZZ")).toHaveAttribute("title", expect.stringMatching(/no mapping/));
    expect(first.getByText("Fixture summary of the quarter.")).toBeInTheDocument();
    const source = first.getByRole("link", { name: /example\.com/ });
    expect(source).toHaveAttribute("href", "https://example.com/fixture-earnings");
    expect(source).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(source).toHaveAttribute("target", "_blank");

    const second = within(rest!);
    expect(second.getByText("Retrieved later")).toBeInTheDocument();
    expect(second.getByText("Inflation")).toBeInTheDocument();
    expect(second.getByText("No related symbols")).toBeInTheDocument();
    expect(second.queryByRole("link", { name: /example/ })).toBeNull(); // no URL, no link
    expect(screen.getAllByText(/trading signal/).length).toBeGreaterThan(0);
  });

  it("shows provider, stream and economic-calendar status", async () => {
    stubApi(ROUTES);
    renderAt("/news");
    const status = await screen.findByLabelText("News provider status");
    await within(status).findByText("Alpaca News API");
    expect(within(status).getByText("Configured")).toBeInTheDocument();
    expect(within(status).getByText("streaming")).toBeInTheDocument();
    expect(within(status).getByText("all news (*)")).toBeInTheDocument();
    expect(within(status).getByText("10 min ago")).toBeInTheDocument();
    expect(within(status).getByText("Not configured")).toBeInTheDocument();
    expect(within(status).getByText(/not an economic calendar/)).toBeInTheDocument();
    expect(within(status).getByText("not stored")).toBeInTheDocument();
    const categories = await screen.findByRole("list", { name: "Category counts" });
    expect(within(categories).getByText("Earnings")).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Recent news retrievals" })).toBeInTheDocument();
  });

  it("sends the chosen filters to the API", async () => {
    const stub = stubApi(ROUTES);
    renderAt("/news");
    await screen.findByRole("list", { name: "News articles" });
    fireEvent.change(screen.getByLabelText("Event category"), { target: { value: "INFLATION" } });
    await waitFor(() => {
      const last = stub.calls.filter((c) => c.url.pathname === "/api/v1/news/articles").at(-1)!;
      expect(last.url.searchParams.get("category")).toBe("INFLATION");
    });
    fireEvent.change(screen.getByLabelText("Asset symbol"), { target: { value: "fxc" } });
    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "fixturewire" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    fireEvent.click(screen.getByRole("radio", { name: "Retrieved later" }));
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "Time range" })).getByRole("radio", { name: "All" }));
    await waitFor(() => {
      const last = stub.calls.filter((c) => c.url.pathname === "/api/v1/news/articles").at(-1)!;
      expect(last.url.searchParams.get("symbol")).toBe("FXC");
      expect(last.url.searchParams.get("source")).toBe("fixturewire");
      expect(last.url.searchParams.get("origin")).toBe("HISTORICAL_REST");
      expect(last.url.searchParams.get("start")).toBeNull(); // "All" time range
    });
  });

  it("says when nothing is stored and when the feed cannot be loaded", async () => {
    stubApi({ ...ROUTES, "/api/v1/news/articles": { body: EMPTY_FEED } });
    const { unmount } = renderAt("/news");
    expect(await screen.findByText("No articles match these filters.")).toBeInTheDocument();
    unmount();
    stubApi({ ...ROUTES, "/api/v1/news/articles": { status: 503, body: { error: { code: "x", message: "database down" } } } });
    renderAt("/news");
    expect(await screen.findByText("database down")).toBeInTheDocument();
  });

  it("starts a historical retrieval with the operator token", async () => {
    const stub = stubApi({
      ...ROUTES,
      "/api/v1/news/ingestion-jobs": (_url, init) => (init?.method === "POST" ? { status: 202, body: JOB } : { body: [JOB] }),
    });
    renderAt("/news");
    const form = await screen.findByRole("form", { name: "Historical news retrieval" });
    fireEvent.change(within(form).getByLabelText("Symbols to retrieve"), { target: { value: "FXC, TWO" } });
    fireEvent.change(within(form).getByLabelText("Operator token"), { target: { value: "op-token" } });
    fireEvent.click(within(form).getByRole("button", { name: "Retrieve" }));
    expect(await within(form).findByText(/Retrieval 33333333 started/)).toBeInTheDocument();
    const post = stub.calls.find((c) => c.init?.method === "POST")!;
    expect(JSON.parse(String(post.init!.body)).symbols).toEqual(["FXC", "TWO"]);
    expect((post.init!.headers as Record<string, string>)["X-Atlas-Operator-Token"]).toBe("op-token");
  });

  it("shows an article's timestamps, classification evidence and revision history", async () => {
    stubApi({ ...ROUTES, "/api/v1/news/articles/22222222-2222-2222-2222-222222222222": { body: DETAIL } });
    renderAt("/news/articles/22222222-2222-2222-2222-222222222222");
    expect(await screen.findByRole("heading", { name: "TEST FIXTURE: Inflation Report Shows Prices Rising" })).toBeInTheDocument();
    expect(screen.getByText("No summary was provided.")).toBeInTheDocument();
    expect(screen.getByText("never (retrieved later)")).toBeInTheDocument();
    expect(screen.getByText(/Retrieved after publication/)).toBeInTheDocument();
    const evidence = screen.getByRole("table", { name: "Event categories" });
    expect(within(evidence).getByText(/headline: “Inflation” \(inflation\)/)).toBeInTheDocument();
    expect(within(evidence).getByText("Unverified")).toBeInTheDocument();
    const revisions = screen.getByRole("table", { name: "Article revisions" });
    expect(within(revisions).getAllByRole("row")).toHaveLength(3);
    expect(within(revisions).getByText("Provider update")).toBeInTheDocument();
    expect(within(revisions).getByText("TEST FIXTURE: Inflation Report (first version)")).toBeInTheDocument();
  });

  it("is in the navigation", async () => {
    stubApi(ROUTES);
    renderAt("/news");
    expect(await screen.findByRole("link", { name: /News & Events/ })).toBeInTheDocument();
  });
});
