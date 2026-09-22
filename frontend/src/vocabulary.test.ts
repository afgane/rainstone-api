import { describe, expect, it } from "vitest";
import { capacityLabel, coverageSentence, costExplanation, formatCost, qualityLabel } from "./vocabulary";

describe("money", () => {
  it("keeps zero, small amounts and unknown distinct", () => {
    expect(formatCost("0")).toBe("$0.00");
    expect(formatCost("0.000004")).toBe("less than $0.01");
    expect(formatCost(null)).toBe("Not available");
    expect(formatCost("12.3456")).toBe("$12.35");
  });
});

describe("vocabulary", () => {
  it("explains a known zero as the existing server rather than free", () => {
    expect(qualityLabel("known_zero")).toContain("Used your Galaxy server");
    expect(costExplanation({ quality: "known_zero", amount: "0" }))
      .toContain("The server continues to incur costs");
  });

  it("says why a cost is missing instead of showing it as zero", () => {
    expect(qualityLabel("unpriced")).toBe("Price unavailable");
    expect(costExplanation({ quality: "unpriced", amount: null }))
      .toContain("unavailable rather than zero");
    expect(costExplanation({ quality: "partial", amount: "1" })).toContain("subtotal");
  });

  it("names where work ran only when the relationship is established", () => {
    expect(capacityLabel(["existing"])).toBe("Your Galaxy server");
    expect(capacityLabel(["dedicated"])).toBe("Dedicated cloud compute");
    expect(capacityLabel(["unknown"])).toBe("Not established");
    expect(capacityLabel([])).toBe("Not established");
  });

  it("counts incomplete coverage next to the amount", () => {
    expect(coverageSentence(5, 2)).toBe("2 runs still need cost data.");
    expect(coverageSentence(5, 0)).toBe("5 runs included.");
    expect(coverageSentence(0, 0)).toBe("No runs in this period.");
  });
});
