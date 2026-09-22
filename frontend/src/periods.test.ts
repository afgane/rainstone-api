import { describe, expect, it } from "vitest";
import { describePeriod, exclusiveEnd, resolvePeriod, weekStart } from "./periods";

// A Wednesday, so week boundaries are unambiguous.
const NOW = new Date("2026-09-23T10:30:00Z");

describe("calendar periods", () => {
  it("treats yesterday as the preceding calendar day", () => {
    const period = resolvePeriod("yesterday", "UTC", undefined, NOW);
    expect(period.fromDate).toBe("2026-09-22");
    expect(period.toDate).toBe("2026-09-22");
    expect(period.open).toBe(false);
  });

  it("uses the previous Monday to Sunday week, not a rolling seven days", () => {
    const period = resolvePeriod("last-week", "UTC", undefined, NOW);
    expect(period.fromDate).toBe("2026-09-14");
    expect(period.toDate).toBe("2026-09-20");
    expect(weekStart("2026-09-20")).toBe("2026-09-14");
  });

  it("uses the previous calendar month, not the last thirty days", () => {
    const period = resolvePeriod("last-month", "UTC", undefined, NOW);
    expect(period.fromDate).toBe("2026-08-01");
    expect(period.toDate).toBe("2026-08-31");
  });

  it("runs this month up to today and says so", () => {
    const period = resolvePeriod("this-month", "UTC", undefined, NOW);
    expect(period.fromDate).toBe("2026-09-01");
    expect(period.toDate).toBe("2026-09-23");
    expect(describePeriod(period, "UTC")).toContain("(so far)");
  });

  it("converts an inclusive last day into an exclusive API boundary", () => {
    const period = resolvePeriod("yesterday", "UTC", undefined, NOW);
    expect(exclusiveEnd(period)).toBe("2026-09-23");
  });

  it("resolves the calendar day in the reporting timezone", () => {
    const lateEvening = new Date("2026-09-23T03:30:00Z");
    // 03:30 UTC is still the previous day in New York.
    expect(resolvePeriod("today", "America/New_York", undefined, lateEvening).fromDate)
      .toBe("2026-09-22");
    expect(resolvePeriod("today", "UTC", undefined, lateEvening).fromDate).toBe("2026-09-23");
  });

  it("shows the resolved dates so a label is never ambiguous", () => {
    const period = resolvePeriod("last-week", "UTC", undefined, NOW);
    const description = describePeriod(period, "UTC");
    expect(description).toContain("Sep 14");
    expect(description).toContain("Sep 20");
    expect(description).toContain("UTC");
  });
});
