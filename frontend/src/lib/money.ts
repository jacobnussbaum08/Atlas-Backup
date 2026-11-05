import { MISSING } from "./format";

/**
 * Display formatting of decimal strings from the ledger, without ever converting them to
 * floating point: digits are rounded half-even (as the backend rounds) with BigInt and
 * grouped by thousands. Values that are not decimal strings show as missing, never as 0.
 */
const DECIMAL = /^([+-])?(\d+)(?:\.(\d+))?$/;
const EXPONENT = /^([+-])?(\d+)(?:\.(\d+))?[eE]([+-]?\d+)$/;

/** Plain notation of a decimal string: Python writes a zero with 8 places as "0E-8". */
export function toPlain(value: string): string {
  const match = EXPONENT.exec(value.trim());
  if (!match) return value.trim();
  const sign = match[1] === "-" ? "-" : "";
  const digits = `${match[2] ?? ""}${match[3] ?? ""}`;
  const point = (match[2] ?? "").length + Number(match[4]);
  if (point <= 0) return `${sign}0.${"0".repeat(-point)}${digits}`;
  if (point >= digits.length) return `${sign}${digits}${"0".repeat(point - digits.length)}`;
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

function roundHalfEven(value: string, places: number): { negative: boolean; digits: bigint } | null {
  const match = DECIMAL.exec(toPlain(value));
  if (!match) return null;
  const negative = match[1] === "-";
  const integer = match[2] ?? "0";
  const fraction = match[3] ?? "";
  let digits = BigInt(integer + fraction.padEnd(places, "0").slice(0, places));
  const rest = fraction.slice(places);
  if (/[1-9]/.test(rest)) {
    const first = Number(rest[0]);
    const tailNonZero = /[1-9]/.test(rest.slice(1));
    if (first > 5 || (first === 5 && (tailNonZero || digits % 2n === 1n))) digits += 1n;
  }
  return { negative: negative && digits !== 0n, digits };
}

function group(integer: string): string {
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export function formatDecimal(
  value: string | null | undefined,
  { places = 2, signed = false }: { places?: number; signed?: boolean } = {},
): string {
  if (value === null || value === undefined || value === "") return MISSING;
  const rounded = roundHalfEven(value, places);
  if (rounded === null) return MISSING;
  const text = rounded.digits.toString().padStart(places + 1, "0");
  const integer = places > 0 ? text.slice(0, -places) : text;
  const fraction = places > 0 ? `.${text.slice(-places)}` : "";
  const sign = rounded.negative ? "−" : signed && rounded.digits !== 0n ? "+" : "";
  return `${sign}${group(integer)}${fraction}`;
}

export function formatMoney(
  value: string | null | undefined,
  currency: string,
  options: { signed?: boolean; places?: number } = {},
): string {
  const text = formatDecimal(value, options);
  return text === MISSING ? MISSING : `${text} ${currency}`;
}

/** -1, 0 or 1 for a decimal string (null when it is not one). */
export function signOf(value: string | null | undefined): -1 | 0 | 1 | null {
  if (value === null || value === undefined) return null;
  const match = DECIMAL.exec(toPlain(value));
  if (!match) return null;
  if (!/[1-9]/.test(`${match[2] ?? ""}${match[3] ?? ""}`)) return 0;
  return match[1] === "-" ? -1 : 1;
}

/** Trims trailing zeros of a quantity ("60.000000000000000000" -> "60"). */
export function formatQuantityExact(value: string | null | undefined): string {
  if (value === null || value === undefined || !DECIMAL.test(toPlain(value))) return MISSING;
  const [integer = "0", fraction = ""] = toPlain(value).replace(/^\+/, "").split(".");
  const trimmed = fraction.replace(/0+$/, "");
  const negative = integer.startsWith("-");
  const digits = negative ? integer.slice(1) : integer;
  return `${negative ? "−" : ""}${group(digits)}${trimmed ? `.${trimmed}` : ""}`;
}
