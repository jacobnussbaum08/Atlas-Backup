import { describe, expect, it } from "vitest";

import { candle } from "../test/marketFixtures";
import type { LiveBar } from "../types/marketData";
import { applyLiveBar, isOnGrid, normalizeCandles } from "./candles";

function live(eventTime: string, close: string, extra: Partial<LiveBar> = {}): LiveBar {
  return {
    id: 1,
    symbol: "AAPL",
    timeframe: "1Min",
    event_time: eventTime,
    open: "100.00",
    high: "102.00",
    low: "99.00",
    close,
    volume: "500",
    vwap: null,
    trade_count: 5,
    is_complete: true,
    quality_status: "VALID",
    revision: 0,
    source: "LIVE_STREAM",
    observation_kind: "LIVE_INITIAL",
    ingested_at: eventTime,
    ...extra,
  };
}

const t = (minute: number) => `2026-03-10T14:${String(minute).padStart(2, "0")}:00Z`;
const sec = (iso: string) => Date.parse(iso) / 1000;

describe("normalizeCandles", () => {
  it("orders by time and keeps one candle per timestamp (highest revision)", () => {
    const { bars, issues } = normalizeCandles(
      [candle(t(2), "101.00"), candle(t(1), "100.50"), candle(t(2), "101.20", { revision: 1 })],
      "1Min",
    );
    expect(bars.map((b) => [b.time, b.close, b.revision])).toEqual([
      [sec(t(1)), 100.5, 0],
      [sec(t(2)), 101.2, 1],
    ]);
    expect(issues.map((i) => i.kind)).toEqual(["out_of_order", "duplicate"]);
  });

  it("drops impossible OHLC and off-grid candles instead of drawing them", () => {
    const { bars, issues } = normalizeCandles(
      [
        candle(t(1), "103.00"), // close above high
        candle("2026-03-10T14:02:30Z", "101.00"), // not on the minute grid
        candle(t(3), "101.00"),
      ],
      "1Min",
    );
    expect(bars.map((b) => b.time)).toEqual([sec(t(3))]);
    expect(issues.map((i) => i.kind)).toEqual(["invalid_ohlc", "off_grid"]);
  });

  it("keeps missing volume missing, distinct from zero", () => {
    const { bars } = normalizeCandles([candle(t(1), "101.00", { volume: null }), candle(t(2), "101.00", { volume: "0" })], "1Min");
    expect(bars.map((b) => b.volume)).toEqual([null, 0]);
  });

  it("checks daily bars against New York midnight across DST", () => {
    expect(isOnGrid(sec("2026-03-09T04:00:00Z"), "1Day")).toBe(true); // EDT
    expect(isOnGrid(sec("2026-01-05T05:00:00Z"), "1Day")).toBe(true); // EST
    expect(isOnGrid(sec("2026-03-09T00:00:00Z"), "1Day")).toBe(false); // UTC midnight
    expect(isOnGrid(sec("2026-03-10T14:05:00Z"), "5Min")).toBe(true);
    expect(isOnGrid(sec("2026-03-10T14:07:00Z"), "5Min")).toBe(false);
  });
});

describe("applyLiveBar", () => {
  const base = normalizeCandles([candle(t(1), "100.50"), candle(t(2), "101.00")], "1Min").bars;

  it("appends a new minute bar and updates the last bar on a higher revision", () => {
    const appended = applyLiveBar(base, live(t(3), "101.10"), "1Min");
    expect(appended.action).toBe("append");
    expect(appended.bars).toHaveLength(3);
    const revised = applyLiveBar(appended.bars, live(t(3), "101.30", { revision: 1, observation_kind: "LIVE_UPDATED" }), "1Min");
    expect(revised.action).toBe("update");
    expect(revised.bars).toHaveLength(3);
    expect(revised.bars[2]?.close).toBe(101.3);
  });

  it("ignores repeated updates of the same revision (no duplicate candles)", () => {
    const once = applyLiveBar(base, live(t(3), "101.10"), "1Min");
    const again = applyLiveBar(once.bars, live(t(3), "101.10"), "1Min");
    expect(again.action).toBe("ignore");
    expect(again.bars).toBe(once.bars);
    const older = applyLiveBar(once.bars, live(t(2), "99.00", { revision: 0 }), "1Min");
    expect(older.action).toBe("ignore");
  });

  it("inserts a late earlier bar and redraws; ignores other timeframes", () => {
    const sparse = normalizeCandles([candle(t(1), "100.50"), candle(t(5), "101.00")], "1Min").bars;
    const inserted = applyLiveBar(sparse, live(t(3), "100.80"), "1Min");
    expect(inserted.action).toBe("insert");
    expect(inserted.bars.map((b) => b.time)).toEqual([sec(t(1)), sec(t(3)), sec(t(5))]);
    expect(applyLiveBar(sparse, live(t(6), "1", { timeframe: "1Day" }), "1Min").action).toBe("ignore");
  });

  it("asks for a reload when a bar's current revision is no longer displayable", () => {
    const result = applyLiveBar(base, live(t(2), "101.00", { revision: 1, quality_status: "INVALID" }), "1Min");
    expect(result.action).toBe("reload");
  });

  it("replaces a forming daily bar with its next revision", () => {
    const day = "2026-03-10T04:00:00Z";
    const daily = normalizeCandles([candle(day, "100.00", { is_complete: false, quality_status: "INCOMPLETE", revision: 3 })], "1Day").bars;
    const next = applyLiveBar(daily, live(day, "100.40", { timeframe: "1Day", is_complete: false, quality_status: "INCOMPLETE", revision: 4, observation_kind: "LIVE_RUNNING" }), "1Day");
    expect(next.action).toBe("update");
    expect(next.bars).toHaveLength(1);
    expect(next.bars[0]?.close).toBe(100.4);
    expect(next.bars[0]?.isComplete).toBe(false);
  });
});

describe("gap markers", () => {
  it("groups intervals that end at the same candle into one counted marker", async () => {
    const { gapMarkers } = await import("../pages/AssetDetailPage");
    const { bars } = normalizeCandles([candle(t(1), "100.50"), candle(t(10), "101.00")], "1Min");
    const markers = gapMarkers(bars, {
      symbol: "AAPL",
      data_type: "BAR",
      timeframe: "1Min",
      start: t(0),
      end: t(20),
      status: "INCOMPLETE",
      expected: [],
      covered: [],
      missing: [
        { start: t(2), end: t(3) },
        { start: t(4), end: t(5) },
        { start: t(11), end: t(12) },
      ],
      unverified: [],
      known_gaps: [],
      notes: [],
    });
    expect(markers.map((m) => [m.time, m.text])).toEqual([[sec(t(10)), "3× unverified"]]);
  });
});
