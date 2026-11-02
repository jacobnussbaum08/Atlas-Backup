import type { AssetSnapshot, LiveQuote, LiveTrade } from "../types/marketData";
import { toNumber } from "./format";

/**
 * Merge a live trade into a snapshot. Only a newer trade replaces the last price; the
 * change is recomputed against the backend's previous close (never invented when absent).
 */
export function withLiveTrade(snapshot: AssetSnapshot, trade: LiveTrade): AssetSnapshot {
  if (trade.symbol !== snapshot.asset.symbol || trade.quality_status !== "VALID") return snapshot;
  const current = snapshot.last_price;
  if (current && Date.parse(current.event_time) >= Date.parse(trade.event_time)) return snapshot;
  const previous = snapshot.previous_close ? toNumber(snapshot.previous_close.value) : null;
  const last = toNumber(trade.price);
  let change: string | null = null;
  let changePercent: string | null = null;
  if (previous !== null && last !== null && previous > 0) {
    change = (last - previous).toFixed(4);
    changePercent = (((last - previous) / previous) * 100).toFixed(4);
  }
  return {
    ...snapshot,
    last_price: { value: trade.price, basis: "TRADE", event_time: trade.event_time, source: trade.source },
    change,
    change_percent: changePercent,
    quality_status: trade.quality_status,
    latest_trade: {
      price: trade.price,
      size: trade.size,
      event_time: trade.event_time,
      received_at: trade.received_at,
      source: trade.source,
      quality_status: trade.quality_status,
      exchange: snapshot.latest_trade?.exchange ?? null,
      conditions: [],
    },
    latest_event_at: later(snapshot.latest_event_at, trade.event_time),
  };
}

export function withLiveQuote(snapshot: AssetSnapshot, quote: LiveQuote): AssetSnapshot {
  if (quote.symbol !== snapshot.asset.symbol || quote.quality_status !== "VALID") return snapshot;
  const current = snapshot.latest_quote;
  if (current && Date.parse(current.event_time) > Date.parse(quote.event_time)) return snapshot;
  return {
    ...snapshot,
    latest_quote: {
      bid_price: quote.bid_price,
      ask_price: quote.ask_price,
      bid_size: quote.bid_size,
      ask_size: quote.ask_size,
      event_time: quote.event_time,
      received_at: quote.received_at,
      source: quote.source,
      quality_status: quote.quality_status,
    },
    latest_event_at: later(snapshot.latest_event_at, quote.event_time),
  };
}

function later(a: string | null, b: string): string {
  return a !== null && Date.parse(a) >= Date.parse(b) ? a : b;
}

/**
 * A quote side with price 0 means "no quote on that side" (IEX publishes one-sided
 * quotes, e.g. after hours). It is shown as absent, never as a price of 0.
 */
export function quoteSide(price: string | null, size: string | null): { price: string; size: string | null } | null {
  const p = toNumber(price);
  if (p === null || p <= 0) return null;
  return { price: price as string, size };
}
