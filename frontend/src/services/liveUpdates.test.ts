import { afterEach, describe, expect, it, vi } from "vitest";

import { FakeEventSource } from "../test/marketFixtures";
import { LIVE_URL, LiveUpdateClient } from "./liveUpdates";

function client() {
  FakeEventSource.instances = [];
  return new LiveUpdateClient((url) => new FakeEventSource(url) as unknown as EventSource);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("LiveUpdateClient", () => {
  it("shares one connection between listeners and closes it with the last one", () => {
    const live = client();
    const a = vi.fn();
    const b = vi.fn();
    const stopA = live.subscribe({ bar: a });
    const stopB = live.subscribe({ bar: b });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0]?.url).toBe(LIVE_URL);

    const source = FakeEventSource.instances[0] as FakeEventSource;
    source.emit("hello", { symbols: ["AAPL"] });
    expect(live.state).toBe("open");
    source.emit("bar", { symbol: "AAPL", close: "101.00" });
    expect(a).toHaveBeenCalledWith({ symbol: "AAPL", close: "101.00" });
    expect(b).toHaveBeenCalledTimes(1);

    stopA();
    expect(source.closed).toBe(false);
    stopB();
    expect(source.closed).toBe(true);
    expect(live.state).toBe("idle");
  });

  it("reconnects after a dropped connection and asks listeners to reload", () => {
    vi.useFakeTimers();
    const live = client();
    const resync = vi.fn();
    const states: string[] = [];
    live.subscribe({ resync, state: (s) => states.push(s) });
    const first = FakeEventSource.instances[0] as FakeEventSource;
    first.emit("hello", {});
    first.fail();
    expect(first.closed).toBe(true);
    expect(live.state).toBe("reconnecting");
    vi.advanceTimersByTime(1_000);
    const second = FakeEventSource.instances[1] as FakeEventSource;
    expect(second).toBeDefined();
    second.emit("hello", {});
    expect(resync).toHaveBeenCalledWith("reconnected");
    expect(states).toEqual(["idle", "connecting", "open", "reconnecting", "open"]);
    expect(FakeEventSource.open()).toHaveLength(1);
  });

  it("forwards server-side errors and resync requests without reconnecting", () => {
    const live = client();
    const error = vi.fn();
    const resync = vi.fn();
    live.subscribe({ error, resync });
    const source = FakeEventSource.instances[0] as FakeEventSource;
    source.emit("hello", {});
    source.emit("error", { message: "stored market data is unavailable" });
    source.emit("resync", { reason: "client queue overflow" });
    expect(error).toHaveBeenCalledWith("stored market data is unavailable");
    expect(resync).toHaveBeenCalledWith("client queue overflow");
    expect(source.closed).toBe(false);
  });

  it("reports unavailable when the browser has no EventSource", () => {
    const live = new LiveUpdateClient(null);
    const states: string[] = [];
    live.subscribe({ state: (s) => states.push(s) });
    expect(states).toEqual(["idle", "unavailable"]);
  });
});
