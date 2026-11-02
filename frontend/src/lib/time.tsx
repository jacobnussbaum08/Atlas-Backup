import { createContext, type ReactNode, useCallback, useContext, useMemo, useState } from "react";

/**
 * Display time zone. All data is UTC; the dashboard shows times in one clearly labelled
 * zone. Default: US equity exchange time (America/New_York, "ET"), the zone the NYSE
 * calendar and the IEX session are defined in. The choice is a per-browser preference.
 */
export type DisplayZone = "ET" | "UTC" | "LOCAL";

export const ZONE_IDS: Record<Exclude<DisplayZone, "LOCAL">, string> = {
  ET: "America/New_York",
  UTC: "UTC",
};

const STORAGE_KEY = "atlas.displayZone";

function readStored(): DisplayZone {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value === "ET" || value === "UTC" || value === "LOCAL") return value;
  } catch {
    // Storage may be unavailable (private mode); fall back to the default.
  }
  return "ET";
}

export function timeZoneId(zone: DisplayZone): string | undefined {
  return zone === "LOCAL" ? undefined : ZONE_IDS[zone];
}

/** Short label shown next to every time, e.g. "ET", "UTC" or the local abbreviation. */
export function zoneLabel(zone: DisplayZone, at: Date = new Date()): string {
  if (zone !== "LOCAL") return zone;
  const part = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
    .formatToParts(at)
    .find((p) => p.type === "timeZoneName");
  return part?.value ?? "local";
}

interface ZoneContextValue {
  zone: DisplayZone;
  setZone: (zone: DisplayZone) => void;
}

const ZoneContext = createContext<ZoneContextValue>({ zone: "ET", setZone: () => undefined });

export function DisplayZoneProvider({
  children,
  initial,
}: {
  children: ReactNode;
  initial?: DisplayZone;
}) {
  const [zone, setZoneState] = useState<DisplayZone>(() => initial ?? readStored());
  const setZone = useCallback((next: DisplayZone) => {
    setZoneState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Not persisted; still applied for this session.
    }
  }, []);
  const value = useMemo(() => ({ zone, setZone }), [zone, setZone]);
  return <ZoneContext.Provider value={value}>{children}</ZoneContext.Provider>;
}

export function useDisplayZone(): ZoneContextValue {
  return useContext(ZoneContext);
}

function offsetMinutes(utcMs: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(utcMs));
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  const asUtc = Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second"));
  return (asUtc - utcMs) / 60_000;
}

/**
 * The UTC instant of a wall-clock time ("2026-10-30", "09:30") in the display zone.
 * Handles DST: the offset is taken at the resulting instant (two passes).
 * Returns null for unparseable input.
 */
export function wallTimeToUtc(date: string, time: string, zone: DisplayZone): Date | null {
  const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  const t = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(time);
  if (!d || !t) return null;
  const wall = Date.UTC(Number(d[1]), Number(d[2]) - 1, Number(d[3]), Number(t[1]), Number(t[2]), Number(t[3] ?? 0));
  if (Number.isNaN(wall)) return null;
  if (zone === "UTC") return new Date(wall);
  if (zone === "LOCAL") {
    const local = new Date(Number(d[1]), Number(d[2]) - 1, Number(d[3]), Number(t[1]), Number(t[2]), Number(t[3] ?? 0));
    return Number.isNaN(local.getTime()) ? null : local;
  }
  const tz = ZONE_IDS[zone];
  let guess = wall - offsetMinutes(wall, tz) * 60_000;
  guess = wall - offsetMinutes(guess, tz) * 60_000;
  return new Date(guess);
}

/** "YYYY-MM-DD" of an instant in the display zone. */
export function dateInZone(at: Date, zone: DisplayZone): string {
  const tz = timeZoneId(zone);
  return new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", ...(tz ? { timeZone: tz } : {}) }).format(at);
}
