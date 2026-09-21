import { expect, test } from "@playwright/test";

for (const target of [
  { name: "root", url: process.env.BASE_URL || "http://localhost:8000" },
  { name: "nested proxy prefix", url: process.env.PREFIX_URL || "http://localhost:8000" },
]) {
  test(`dashboard works at ${target.name}`, async ({ page }) => {
    if (target.name === "nested proxy prefix") {
      await page.setViewportSize({ width: 390, height: 844 });
    }
    await page.goto(target.url);
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
    await expect(page.getByText(/Demo data/)).toBeVisible();
    await page.getByRole("button", { name: "Jobs", exact: true }).click();
    await expect(page).toHaveURL(/view=jobs/);
    await page.getByPlaceholder("Search job, tool, owner, or workflow").fill("fastqc");
    await expect(page).toHaveURL(/search=fastqc/);
    await expect(page.getByText("3 with known amounts")).toBeVisible();
    await page.reload();
    await expect(page.getByPlaceholder("Search job, tool, owner, or workflow")).toHaveValue("fastqc");
    await page.getByRole("button", { name: /#16/ }).click();
    await expect(page).toHaveURL(/detail_kind=jobs/);
    await expect(page.getByRole("dialog")).toContainText("Full job");
    await expect(page.getByRole("button", { name: "Close details" })).toBeFocused();
    await page.goBack();
    await expect(page.getByRole("dialog")).toBeHidden();
    await page.goForward();
    await expect(page.getByRole("dialog")).toContainText("Full job");
    await page.keyboard.press("Escape");
    await page.getByLabel("Tool version").fill("0.74+galaxy1");
    await page.getByLabel("Tool version").press("Tab");
    await expect(page).toHaveURL(/tool_version=0.74%2Bgalaxy1/);
    await page.getByLabel("Cost basis").selectOption("allocated");
    await expect(page).toHaveURL(/basis=allocated/);
    await page.getByRole("button", { name: "Status", exact: true }).click();
    await expect(page).toHaveURL(/view=status/);
    await expect(page.getByRole("heading", { name: "Deployment status" })).toBeVisible();
    await expect(page.getByText(/identity mode/)).toBeVisible();
    await expect(page.getByRole("link", { name: "Download diagnostics" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "migration_state" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });
}
