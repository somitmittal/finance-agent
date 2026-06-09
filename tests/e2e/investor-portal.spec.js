const { test, expect } = require("@playwright/test");

test.describe("Finance Agent investor portal", () => {
  test("loads, registers, and manages portfolio holdings", async ({ page }) => {
    const email = `e2e-${Date.now()}@example.com`;

    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Finance Agent" })).toBeVisible();
    await expect(page.locator("#actionText")).toHaveText("Search a stock");
    await expect(page.locator("#portfolioSymbol")).toBeDisabled();

    await page.locator("#authEmail").fill(email);
    await page.locator("#authPassword").fill("password123");
    await page.locator("#registerBtn").click();
    await expect(page.locator("#authStatus")).toContainText(email);
    await expect(page.locator("#portfolioSymbol")).toBeEnabled();

    await page.locator("#portfolioSymbol").fill("TCS");
    await page.locator("#companyName").fill("Tata Consultancy Services");
    await page.locator("#bseCode").fill("532540");
    await page.locator("#quantity").fill("2");
    await page.locator("#avgPrice").fill("3500");
    await page.locator("#thesis").fill("Test holding for E2E automation");
    await page.getByRole("button", { name: "Save Stock" }).click();
    await expect(page.locator(".holding").filter({ hasText: "TCS" })).toBeVisible();
    await expect(page.locator(".holding").filter({ hasText: "Qty 2" })).toBeVisible();

    await page.locator(".holding").filter({ hasText: "TCS" }).getByRole("button", { name: "Edit" }).click();
    await page.locator("#quantity").fill("3");
    await page.getByRole("button", { name: "Save Stock" }).click();
    await expect(page.locator(".holding").filter({ hasText: "Qty 3" })).toBeVisible();

    page.once("dialog", async (dialog) => {
      expect(dialog.message()).toContain("Delete TCS");
      await dialog.accept();
    });
    await page.locator(".holding").filter({ hasText: "TCS" }).getByRole("button", { name: "Delete" }).click();
    await expect(page.locator(".holding").filter({ hasText: "TCS" })).toHaveCount(0);
    await expect(page.locator("#portfolioList")).toContainText("No saved stocks yet");
  });

  test("searches, analyzes a live stock, switches tabs, and asks chat", async ({ page }) => {
    test.setTimeout(240_000);

    await page.goto("/");
    await page.locator("#symbolInput").fill("TCS");
    await expect(page.locator("#searchRecommendations")).toBeVisible();
    await expect(page.locator(".search-recommendation").first()).toBeVisible({ timeout: 45_000 });

    await page.getByRole("button", { name: "Analyze" }).click();
    await expect(page.locator("#actionText")).not.toContainText("Analyzing", { timeout: 180_000 });
    await expect(page.locator(".investor-hero")).toBeVisible();
    await expect(page.locator(".investor-hero")).toContainText(/Score \/ 100|No investor stance|candidate|signal|setup/i);
    await expect(page.locator("#valuationText")).not.toHaveText("-");
    await expect(page.locator("#themeText")).not.toHaveText("-");

    await page.getByRole("tab", { name: "Investor Analysis" }).click();
    await expect(page.locator("#dossier")).toBeVisible();
    await expect(page.locator("#dossier")).toContainText("Investor Decision Framework");
    await expect(page.locator("#dossier")).toContainText("Fetched Order / Contract Disclosures by FY");
    await expect(page.locator("#summary")).toHaveClass(/hidden/);

    await page.getByRole("tab", { name: "Risk Buckets" }).click();
    await expect(page.locator("#riskBuckets")).toBeVisible();
    await expect(page.locator("#riskBuckets")).toContainText("Risk Verdict");

    await page.getByRole("tab", { name: "Readout" }).click();
    await page.locator("#chatQuestion").fill("What are the red flags and valuation caveats?");
    await page.getByRole("button", { name: "Ask" }).click();
    await expect(page.locator(".chat-message.assistant").last()).toBeVisible({ timeout: 180_000 });
    await expect(page.locator(".chat-message.assistant").last()).toContainText(/not financial advice|risk|valuation|current scan/i);
  });
});
