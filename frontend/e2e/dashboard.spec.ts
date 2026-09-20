import { expect, test } from "@playwright/test";

test("packaged dashboard reports fixture costs and switches basis", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Understand what your work cost" })).toBeVisible();
  await expect(page.getByText("Demo data")).toBeVisible();
  await expect(page.getByText("Less than $0.01").first()).toBeVisible();
  await page.getByLabel("Cost basis").selectOption("allocated");
  await expect(page).toHaveURL(/basis=allocated/);
  await expect(page.locator(".stat-card.featured").getByText("Allocated resource cost")).toBeVisible();
});
