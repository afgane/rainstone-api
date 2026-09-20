import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import CostAmount from "./CostAmount.vue";

describe("CostAmount", () => {
  it("does not display a small positive estimate as zero", () => {
    expect(mount(CostAmount, { props: { amount: "0.0029", currency: "USD" } }).text()).toBe("Less than $0.01");
  });
  it("labels missing costs as unknown", () => {
    expect(mount(CostAmount, { props: { amount: null } }).text()).toBe("Unknown");
  });
});
