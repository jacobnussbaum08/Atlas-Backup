import { describe, expect, it } from "vitest";

import { AAPL_SNAPSHOT, SPY_SNAPSHOT } from "../test/marketFixtures";
import type { LiveTrade } from "../types/marketData";
import { formatDateTime, formatPercent, formatPrice, formatSigned, MISSING } from "./format";
import { quoteSide, withLiveTrade } from "./snapshots";
import { wallTimeToUtc } from "./time";

describe("formatting", () => {
  it("labels every timestamp with its zone", () => {
    const iso = "2026-03-10T15:00:00Z";
    expect(formatDateTime(iso, "ET")).toBe("2026-03-10 11:00:00 ET");
    expect(formatDateTime(iso, "UTC")).toBe("2026-03-10 15:00:00 UTC");
    expect(formatDateTime("2026-01-05T15:00:00Z", "ET")).toBe("2026-01-05 10:00:00 ET");
  });

  it("never turns a missing value into zero", () => {
    expect(formatPrice(null)).toBe(MISSING);
    expect(formatSigned(null)).toBe(MISSING);
    expect(formatPercent(undefined)).toBe(MISSING);
    expect(formatPrice("101.5")).toBe("101.50");
    expect(formatPrice("0.1234")).toBe("0.1234");
    expect(formatSigned("-4.21")).toBe("−4.21");
    expect(formatPercent("1.5000")).toBe("+1.50%");
  });
});

describe("wallTimeToUtc", () => {
  it("converts exchange time to UTC across daylight saving", () => {
    expect(wallTimeToUtc("2026-03-10", "09:30", "ET")?.toISOString()).toBe("2026-03-10T13:30:00.000Z");
    expect(wallTimeToUtc("2026-03-06", "09:30", "ET")?.toISOString()).toBe("2026-03-06T14:30:00.000Z");
    expect(wallTimeToUtc("2026-11-02", "00:00", "ET")?.toISOString()).toBe("2026-11-02T05:00:00.000Z");
    expect(wallTimeToUtc("2026-03-10", "09:30", "UTC")?.toISOString()).toBe("2026-03-10T09:30:00.000Z");
    expect(wallTimeToUtc("not-a-date", "09:30", "ET")).toBeNull();
  });
});

describe("live snapshot merging", () => {
  const trade = (price: string, at: string): LiveTrade => ({
    id: 9,
    symbol: "AAPL",
    price,
    size: "3",
    event_time: at,
    received_at: at,
    source: "LIVE_STREAM",
    quality_status: "VALID",
    arrived: 1,
  });

  it("replaces the last price with a newer trade and recomputes the change", () => {
    const merged = withLiveTrade(AAPL_SNAPSHOT, trade("102.00", "2026-03-10T15:00:01Z"));
    expect(merged.last_price?.value).toBe("102.00");
    expect(Number(merged.change)).toBeCloseTo(2);
    expect(Number(merged.change_percent)).toBeCloseTo(2);
  });

  it("ignores older trades and never invents a change without a previous close", () => {
    expect(withLiveTrade(AAPL_SNAPSHOT, trade("90.00", "2026-03-10T14:00:00Z"))).toBe(AAPL_SNAPSHOT);
    const spy = withLiveTrade(SPY_SNAPSHOT, { ...trade("500.00", "2026-03-10T15:00:01Z"), symbol: "SPY" });
    expect(spy.last_price?.value).toBe("500.00");
    expect(spy.change).toBeNull();
    expect(spy.change_percent).toBeNull();
  });

  it("treats a zero-priced quote side as absent (one-sided quote)", () => {
    expect(quoteSide("0", "0")).toBeNull();
    expect(quoteSide(null, null)).toBeNull();
    expect(quoteSide("101.45", "1")).toEqual({ price: "101.45", size: "1" });
  });
});
