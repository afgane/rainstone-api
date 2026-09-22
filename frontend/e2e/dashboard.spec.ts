import { expect, test } from "@playwright/test";

const BASE = process.env.BASE_URL || "http://localhost:8000";
const PREFIX = process.env.PREFIX_URL || BASE;
// The demonstration fixture is dated, so scenarios that assert amounts pin the
// period explicitly instead of depending on today's date.
const FIXTURE_PERIOD = "period=custom&from=2026-09-19&to=2026-09-20";

test("a first-time user gets a scoped answer without typing dates", async ({ page }) => {
  await page.goto(BASE);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  const headline = page.locator(".headline");
  await expect(headline.getByText("Estimated run compute cost")).toBeVisible();
  await expect(headline.getByText("Compute started for your tool and workflow runs")).toBeVisible();

  for (const period of ["Yesterday", "Last week", "Last month"]) {
    await page.getByRole("button", { name: period, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`period=${period.toLowerCase().replace(" ", "-")}`));
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  }
  // The resolved dates are always visible, so a preset is never ambiguous.
  await expect(page.locator(".resolved")).toContainText("UTC");
});

test("a workflow run shows its whole cost and its share of the period", async ({ page }) => {
  await page.goto(`${BASE}?view=runs&${FIXTURE_PERIOD}`);
  await page.getByPlaceholder("Find a workflow run").fill("RNA-seq");
  await expect(page).toHaveURL(/search=RNA-seq/);
  const run = page.getByRole("button", { name: /RNA-seq mixed execution demo/ });
  await expect(run).toBeVisible();
  await expect(page.getByText("Run total").first()).toBeVisible();

  await run.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Run total", { exact: false })).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "Steps" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Close details" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // A run whose cost crosses midnight shows how much of it falls in the period.
  await page.goto(`${BASE}?view=tool-runs&period=custom&from=2026-09-20&to=2026-09-20&search=midnight-price`);
  // The first button in the table head sorts; the row's tool name opens details.
  await page.locator(".jobs-table tbody").getByRole("button").first().click();
  await expect(page.getByRole("dialog")).toContainText("falls inside the selected dates");
});

test("ordinary language explains zero, unknown and incomplete costs", async ({ page }) => {
  await page.goto(`${BASE}?view=tool-runs&${FIXTURE_PERIOD}`);
  const table = page.locator(".jobs-table");
  await expect(table.getByText("Used your Galaxy server").first()).toBeVisible();
  await expect(
    table.getByText("Price unavailable").or(table.getByText("Cost incomplete")).first(),
  ).toBeVisible();

  await table.locator("tbody").getByRole("button", { name: "goseq" }).first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("The server continues to incur costs");
  await expect(dialog.getByRole("heading", { name: "Where it ran" })).toBeVisible();
  await dialog.getByRole("button", { name: "Close details" }).click();
});

test("advanced filters stay visible and removable while collapsed", async ({ page }) => {
  await page.goto(`${BASE}?${FIXTURE_PERIOD}`);
  const disclosure = page.getByRole("button", { name: /More filters/ });
  await expect(disclosure).toHaveAttribute("aria-expanded", "false");
  await disclosure.click();
  await page.getByLabel("Cost coverage").selectOption("known_zero");
  await expect(page).toHaveURL(/quality=known_zero/);

  await disclosure.click();
  await expect(disclosure).toHaveAttribute("aria-expanded", "false");
  const chip = page.getByRole("button", { name: /Cost coverage.*known_zero/ });
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(page).not.toHaveURL(/quality=known_zero/);
});

test("a long filter value stays inside the sidebar", async ({ page }) => {
  const toolId = "toolshed.g2.bx.psu.edu/repos/iuc/goseq/goseq/2.0.1";
  await page.goto(`${BASE}?view=tool-runs&${FIXTURE_PERIOD}&tool_id=${toolId}`);
  const chip = page.getByRole("button", { name: /Tool ID/ });
  await expect(chip).toBeVisible();

  const sidebar = await page.locator(".sidebar").boundingBox();
  const box = await chip.boundingBox();
  expect(box!.x + box!.width).toBeLessThanOrEqual(sidebar!.x + sidebar!.width + 1);
  // The whole value stays available even though the label is truncated.
  await expect(chip).toHaveAttribute("title", new RegExp(toolId.replace(/[.]/g, "\\.")));
});

test("status is operational and carries no report controls", async ({ page }) => {
  await page.goto(BASE);
  await page.getByRole("button", { name: "Status", exact: true }).click();
  await expect(page).toHaveURL(/view=status/);
  await expect(page.getByRole("heading", { name: "Deployment status" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Download diagnostics" })).toBeVisible();
  await expect(page.getByRole("button", { name: "This month", exact: true })).toBeHidden();
  await expect(page.getByRole("cell", { name: "migration_state" })).toBeVisible();
});

test("the report works behind a proxy prefix and on a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${PREFIX}?${FIXTURE_PERIOD}`);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();

  const filters = page.getByRole("button", { name: /Filters/ });
  await expect(filters).toBeVisible();
  await filters.click();
  await page.getByRole("button", { name: "Workflow runs", exact: true }).click();
  await expect(page).toHaveURL(/view=runs/);

  await page.reload();
  await expect(page.getByRole("heading", { name: "Workflow runs" }).first()).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});
