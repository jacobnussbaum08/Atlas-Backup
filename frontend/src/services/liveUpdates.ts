import type { LiveBar, LiveHello, LiveQuote, LiveTrade } from "../types/marketData";

/**
 * One Server-Sent Events connection per browser tab to /api/v1/market-data/live,
 * shared by every component that wants live data (never one per component, never a
 * provider connection). Opened when the first listener subscribes, closed when the last
 * one leaves. After any interruption listeners get ``resync`` and reload from REST,
 * because events sent while disconnected are not replayed.
 */
export const LIVE_URL = "/api/v1/market-data/live";

export type LiveConnectionState = "idle" | "connecting" | "open" | "reconnecting" | "unavailable";

export interface LiveListener {
  bar?: (bar: LiveBar) => void;
  trade?: (trade: LiveTrade) => void;
  quote?: (quote: LiveQuote) => void;
  /** Data may have been missed (reconnect, server overflow, database back): reload. */
  resync?: (reason: string) => void;
  /** The server reported that stored data is unavailable. */
  error?: (message: string) => void;
  state?: (state: LiveConnectionState) => void;
}

type EventSourceFactory = (url: string) => EventSource;

const RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000, 10_000, 30_000];

export class LiveUpdateClient {
  private source: EventSource | null = null;
  private readonly listeners = new Set<LiveListener>();
  private stateValue: LiveConnectionState = "idle";
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private hadConnection = false;
  hello: LiveHello | null = null;

  constructor(private readonly factory: EventSourceFactory | null) {}

  get state(): LiveConnectionState {
    return this.stateValue;
  }

  get listenerCount(): number {
    return this.listeners.size;
  }

  subscribe(listener: LiveListener): () => void {
    this.listeners.add(listener);
    listener.state?.(this.stateValue);
    if (this.listeners.size === 1) this.connect();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.disconnect();
    };
  }

  private setState(state: LiveConnectionState): void {
    if (state === this.stateValue) return;
    this.stateValue = state;
    for (const l of this.listeners) l.state?.(state);
  }

  private emit<K extends keyof LiveListener>(key: K, value: Parameters<NonNullable<LiveListener[K]>>[0]): void {
    for (const l of this.listeners) {
      const handler = l[key] as ((v: typeof value) => void) | undefined;
      handler?.(value);
    }
  }

  private connect(): void {
    if (this.factory === null) {
      this.setState("unavailable");
      return;
    }
    this.setState(this.hadConnection ? "reconnecting" : "connecting");
    const source = this.factory(LIVE_URL);
    this.source = source;
    source.addEventListener("hello", (event) => {
      this.hello = JSON.parse((event as MessageEvent<string>).data) as LiveHello;
      this.attempts = 0;
      this.setState("open");
      if (this.hadConnection) this.emit("resync", "reconnected");
      this.hadConnection = true;
    });
    const forward = (type: "bar" | "trade" | "quote") => (event: Event) => {
      this.emit(type, JSON.parse((event as MessageEvent<string>).data));
    };
    source.addEventListener("bar", forward("bar"));
    source.addEventListener("trade", forward("trade"));
    source.addEventListener("quote", forward("quote"));
    source.addEventListener("resync", (event) => {
      const data = JSON.parse((event as MessageEvent<string>).data) as { reason?: string };
      this.emit("resync", data.reason ?? "server requested reload");
    });
    source.addEventListener("error", (event) => {
      const data = (event as MessageEvent<string>).data;
      if (typeof data === "string" && data) {
        // A server-sent "error" event (stored data unavailable), not a transport error.
        const parsed = JSON.parse(data) as { message?: string };
        this.emit("error", parsed.message ?? "stored market data is unavailable");
        return;
      }
      // Transport failure: close and retry with backoff (also covers HTTP errors, for
      // which EventSource gives up on its own).
      this.source?.close();
      this.source = null;
      if (this.listeners.size === 0) return;
      this.setState("reconnecting");
      const delay = RECONNECT_DELAYS_MS[Math.min(this.attempts, RECONNECT_DELAYS_MS.length - 1)];
      this.attempts += 1;
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        if (this.listeners.size > 0) this.connect();
      }, delay);
    });
  }

  private disconnect(): void {
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.source?.close();
    this.source = null;
    this.attempts = 0;
    this.hadConnection = false;
    this.setState("idle");
  }
}

const defaultFactory: EventSourceFactory | null =
  typeof EventSource === "undefined" ? null : (url) => new EventSource(url);

let shared: LiveUpdateClient | null = null;

export function liveClient(): LiveUpdateClient {
  shared ??= new LiveUpdateClient(defaultFactory);
  return shared;
}

/** Tests replace the shared client (e.g. with a fake EventSource factory). */
export function setLiveClient(client: LiveUpdateClient | null): void {
  shared = client;
}
