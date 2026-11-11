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

/**
 * The decimal string with its sign flipped, exactly (no floating point). Used where the
 * backend reports a loss as a positive number and the dashboard shows P/L.
 */
export function negateDecimal(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const plain = toPlain(value);
  const match = DECIMAL.exec(plain);
  if (!match) return null;
  if (signOf(plain) === 0) return plain.replace(/^[+-]/, "");
  return match[1] === "-" ? plain.slice(1) : `-${plain.replace(/^\+/, "")}`;
}

function scaled(value: string): { units: bigint; places: number } | null {
  const match = DECIMAL.exec(toPlain(value));
  if (!match) return null;
  const fraction = match[3] ?? "";
  const units = BigInt(`${match[2] ?? "0"}${fraction}`);
  return { units: match[1] === "-" ? -units : units, places: fraction.length };
}

/** ``a - b`` of two decimal strings, exactly (BigInt), as a plain decimal string. */
export function subtractDecimal(a: string | null | undefined, b: string | null | undefined): string | null {
  if (a === null || a === undefined || b === null || b === undefined) return null;
  const x = scaled(a);
  const y = scaled(b);
  if (!x || !y) return null;
  const places = Math.max(x.places, y.places);
  const diff = x.units * 10n ** BigInt(places - x.places) - y.units * 10n ** BigInt(places - y.places);
  const negative = diff < 0n;
  const digits = (negative ? -diff : diff).toString().padStart(places + 1, "0");
  const text = places > 0 ? `${digits.slice(0, -places)}.${digits.slice(-places)}` : digits;
  return negative ? `-${text}` : text;
}

/**
 * ``numerator / denominator`` as a percentage for display only (2 places by default).
 * Money of record is never derived from this; it is a ratio of two exact decimals shown
 * rounded. Missing or zero denominators give null, never 0 or infinity.
 */
export function percentOf(
  numerator: string | null | undefined,
  denominator: string | null | undefined,
): number | null {
  if (numerator === null || numerator === undefined || denominator === null || denominator === undefined) return null;
  if (!DECIMAL.test(toPlain(numerator)) || !DECIMAL.test(toPlain(denominator))) return null;
  const d = Number(toPlain(denominator));
  if (!Number.isFinite(d) || d === 0) return null;
  const n = Number(toPlain(numerator));
  return Number.isFinite(n) ? (n / d) * 100 : null;
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
