import { describe, expect, it } from "vitest";

import { formatDecimal, formatMoney, formatQuantityExact, signOf } from "./money";

describe("decimal display formatting", () => {
  it("groups and keeps exact cents without floating point", () => {
    expect(formatDecimal("100000.00000000")).toBe("100,000.00");
    expect(formatDecimal("12345678901234567.89")).toBe("12,345,678,901,234,567.89");
    expect(formatDecimal("0.1")).toBe("0.10");
  });

  it("rounds half-even like the backend", () => {
    expect(formatDecimal("10.0025")).toBe("10.00");
    expect(formatDecimal("10.0035")).toBe("10.00");
    expect(formatDecimal("10.005")).toBe("10.00");
    expect(formatDecimal("10.015")).toBe("10.02");
    expect(formatDecimal("0.015")).toBe("0.02");
    expect(formatDecimal("0.025")).toBe("0.02");
    expect(formatDecimal("0.0251")).toBe("0.03");
  });

  it("signs values and never shows a negative zero", () => {
    expect(formatDecimal("-58.55", { signed: true })).toBe("−58.55");
    expect(formatDecimal("30", { signed: true })).toBe("+30.00");
    expect(formatDecimal("-0.001")).toBe("0.00");
    expect(formatDecimal("0", { signed: true })).toBe("0.00");
  });

  it("shows missing values as missing, never as zero", () => {
    expect(formatDecimal(null)).toBe("—");
    expect(formatDecimal("abc")).toBe("—");
    expect(formatMoney(undefined, "USD")).toBe("—");
    expect(formatMoney("1.5", "USD")).toBe("1.50 USD");
  });

  it("reads Python's exponent notation", () => {
    expect(formatDecimal("0E-8")).toBe("0.00");
    expect(formatDecimal("-1.5E+2")).toBe("−150.00");
    expect(formatDecimal("1.23E-3", { places: 4 })).toBe("0.0012");
    expect(signOf("0E-8")).toBe(0);
    expect(formatQuantityExact("6E+1")).toBe("60");
  });

  it("reads signs and trims quantities", () => {
    expect(signOf("-0.00")).toBe(0);
    expect(signOf("-1")).toBe(-1);
    expect(signOf("2.5")).toBe(1);
    expect(signOf("x")).toBeNull();
    expect(formatQuantityExact("60.000000000000000000")).toBe("60");
    expect(formatQuantityExact("-1250.125000")).toBe("−1,250.125");
  });
});
