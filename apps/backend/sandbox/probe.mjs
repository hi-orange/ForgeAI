import playwright from "/opt/visual/node_modules/playwright-core/index.js";
import fs from "node:fs/promises";

const { chromium } = playwright;
const [baseUrl, manifestPath] = process.argv.slice(2);
if (!baseUrl || !manifestPath) {
  throw new Error("probe.mjs requires a base URL and smoke manifest path");
}

const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
const contract = JSON.parse(
  await fs.readFile(new URL("./smoke_contract.json", import.meta.url), "utf8"),
);
const checks = Array.isArray(manifest.browser_checks) ? manifest.browser_checks : [];
if (!checks.length) throw new Error("forgeai.smoke.json must define browser_checks");

const actionHandlers = {
  click: async (page, action) => page.locator(action.selector).first().click(),
  fill: async (page, action) => page.locator(action.selector).first().fill(action.value),
  select: async (page, action) =>
    page.locator(action.selector).first().selectOption(action.value),
  navigate: async (page, action) =>
    page.goto(new URL(action.url, baseUrl).toString(), { waitUntil: "domcontentloaded" }),
  wait_for_selector: async (page, action) =>
    page.locator(action.selector).first().waitFor({ state: "visible" }),
  expect_text: async (page, action) =>
    page.getByText(action.text, { exact: false }).first().waitFor({ state: "visible" }),
  expect_any_text: async (page, action) =>
    page.waitForFunction(
      (texts) => texts.some((text) => document.body?.innerText.includes(text)),
      action.texts,
    ),
  expect_path: async (page, action) =>
    page.waitForURL((url) => url.pathname === action.path),
  expect_count: async (page, action) => {
    const locator = page.locator(action.selector);
    if (action.count > 0) await locator.first().waitFor({ state: "attached" });
    const actual = await locator.count();
    if (actual !== action.count) {
      throw new Error(
        "expected " + action.count + " elements for " + action.selector + ", found " + actual,
      );
    }
  },
  expect_no_horizontal_scroll: async (page) => {
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    );
    if (overflow) throw new Error("horizontal viewport overflow");
  },
  screenshot: async (page, action) =>
    page.screenshot({ path: "/tmp/" + action.name + ".png", fullPage: true }),
};
const contractActions = Object.keys(contract.browser.actions).sort();
const implementedActions = Object.keys(actionHandlers).sort();
if (JSON.stringify(contractActions) !== JSON.stringify(implementedActions)) {
  throw new Error("smoke contract and browser action handlers are out of sync");
}

const browser = await chromium.launch({
  executablePath: "/usr/bin/chromium",
  headless: true,
  timeout: 30000,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
});

const results = [];
let failed = false;
try {
  for (const check of checks) {
    const context = await browser.newContext({
      viewport: check.viewport || contract.browser.default_viewport,
      locale: "zh-CN",
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.setDefaultNavigationTimeout(30000);
    const consoleErrors = [];
    const pageErrors = [];
    const apiResponses = [];
    page.on("console", (message) => {
      const text = message.text();
      const locationUrl = message.location().url;
      let isMissingFavicon = false;
      try {
        isMissingFavicon =
          text.includes("404") && new URL(locationUrl).pathname === "/favicon.ico";
      } catch {
        // Keep malformed or absent console locations visible as real evidence.
      }
      if (message.type() === "error" && !isMissingFavicon && consoleErrors.length < 20) {
        consoleErrors.push(text.slice(0, 500));
      }
    });
    page.on("pageerror", (error) => {
      if (pageErrors.length < 20) pageErrors.push(String(error).slice(0, 500));
    });
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (url.pathname.startsWith("/api/")) {
        apiResponses.push({ path: url.pathname, status: response.status() });
      }
    });

    const errors = [];
    try {
      await page.goto(new URL(check.route, baseUrl).toString(), {
        waitUntil: "domcontentloaded",
      });
      await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
      for (const text of check.expect_text || []) {
        await page.getByText(text, { exact: false }).first().waitFor({ state: "visible" });
      }
      for (const action of check.actions || []) {
        const handler = actionHandlers[action.type];
        if (!handler) throw new Error("unsupported browser action: " + action.type);
        await handler(page, action);
        await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
      }
      for (const expected of check.expect_api || []) {
        const matches = apiResponses.filter((item) => item.path.startsWith(expected));
        if (!matches.length) errors.push(`missing frontend API request: ${expected}`);
        else if (matches.every((item) => item.status >= 400)) {
          errors.push(`frontend API request failed: ${expected}`);
        }
      }
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth > window.innerWidth + 1,
      );
      if (overflow) errors.push("horizontal viewport overflow");
      errors.push(...consoleErrors.map((item) => `console: ${item}`));
      errors.push(...pageErrors.map((item) => `page: ${item}`));
    } catch (error) {
      errors.push(String(error).slice(0, 1000));
    }
    if (errors.length) failed = true;
    results.push({
      id: check.id,
      route: check.route,
      ok: errors.length === 0,
      errors,
      api_responses: apiResponses,
    });
    await context.close();
  }
} finally {
  await browser.close();
}

for (const check of results.filter((item) => !item.ok)) {
  for (const error of check.errors) {
    const detail = String(error).replace(/\s+/g, " ").slice(0, 500);
    console.error(`BROWSER_CHECK_FAILED ${check.id} ${check.route}: ${detail}`);
  }
}
console.log(JSON.stringify({ browser: "chromium", checks: results }, null, 2));
if (failed) process.exitCode = 1;
