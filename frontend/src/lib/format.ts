import { type DisplayZone, timeZoneId, zoneLabel } from "./time";

/** Placeholder for a value that does not exist. Never a zero. */
export const MISSING = "—";

export function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Prices keep the provider's precision up to 4 decimals, at least 2. */
export function formatPrice(value: string | number | null | undefined): string {
  const n = toNumber(value);
  if (n === null) return MISSING;
  const text = typeof value === "string" ? value : String(value);
  const decimals = Math.min(Math.max((text.split(".")[1] ?? "").length, 2), 4);
  return n.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatSigned(value: string | number | null | undefined, digits = 2): string {
  const n = toNumber(value);
  if (n === null) return MISSING;
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function formatPercent(value: string | number | null | undefined): string {
  const text = formatSigned(value, 2);
  return text === MISSING ? MISSING : `${text}%`;
}

export function formatQuantity(value: string | number | null | undefined): string {
  const n = toNumber(value);
  if (n === null) return MISSING;
  return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

export function formatCount(value: number | null | undefined): string {
  return value === null || value === undefined ? MISSING : value.toLocaleString("en-US");
}

export function direction(value: string | number | null | undefined): "up" | "down" | "flat" {
  const n = toNumber(value);
  if (n === null || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

function parts(iso: string, zone: DisplayZone, options: Intl.DateTimeFormatOptions): string {
  const tz = timeZoneId(zone);
  return new Intl.DateTimeFormat("en-CA", {
    hourCycle: "h23",
    ...options,
    ...(tz ? { timeZone: tz } : {}),
  }).format(new Date(iso));
}

/** "2026-10-30 16:58:59 ET" */
export function formatDateTime(
  iso: string | null | undefined,
  zone: DisplayZone,
  { seconds = true, label = true }: { seconds?: boolean; label?: boolean } = {},
): string {
  if (!iso) return MISSING;
  const date = parts(iso, zone, { year: "numeric", month: "2-digit", day: "2-digit" });
  const time = parts(iso, zone, {
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" } : {}),
  });
  return label ? `${date} ${time} ${zoneLabel(zone, new Date(iso))}` : `${date} ${time}`;
}

/** "16:58 ET" */
export function formatClock(iso: string | null | undefined, zone: DisplayZone): string {
  if (!iso) return MISSING;
  return `${parts(iso, zone, { hour: "2-digit", minute: "2-digit" })} ${zoneLabel(zone, new Date(iso))}`;
}

/** "Mon 02 Nov 09:30 ET" */
export function formatSessionTime(iso: string | null | undefined, zone: DisplayZone): string {
  if (!iso) return MISSING;
  const tz = timeZoneId(zone);
  const day = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "2-digit", month: "short", ...(tz ? { timeZone: tz } : {}) }).format(new Date(iso));
  return `${day} ${formatClock(iso, zone)}`;
}

/** "16:58:59 ET" */
export function formatTime(iso: string | null | undefined, zone: DisplayZone): string {
  if (!iso) return MISSING;
  const time = parts(iso, zone, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return `${time} ${zoneLabel(zone, new Date(iso))}`;
}

/** "42s ago", "5m ago", "3h ago", "2d ago" (from ``now``). */
export function formatAge(iso: string | null | undefined, now: number): string {
  if (!iso) return MISSING;
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function formatDuration(startIso: string, endIso: string | null): string {
  if (!endIso) return "ongoing";
  const seconds = Math.max(0, Math.round((Date.parse(endIso) - Date.parse(startIso)) / 1000));
  if (seconds < 120) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 120) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function humanize(code: string): string {
  const text = code.replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}
