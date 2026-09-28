import { chromium } from "/opt/visual/node_modules/playwright-core/index.js";
import fs from "node:fs/promises";

const [url, outputDirectory] = process.argv.slice(2);
if (!url || !outputDirectory) {
  throw new Error("capture.mjs requires a URL and output directory");
}

const viewports = [
  { name: "desktop", width: 1440, height: 900, isMobile: false, hasTouch: false },
  { name: "mobile", width: 390, height: 844, isMobile: true, hasTouch: true },
];

await fs.mkdir(outputDirectory, { recursive: true });
const browser = await chromium.launch({
  executablePath: "/usr/bin/chromium",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
});

const results = [];
try {
  for (const viewport of viewports) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      screen: { width: viewport.width, height: viewport.height },
      isMobile: viewport.isMobile,
      hasTouch: viewport.hasTouch,
      deviceScaleFactor: 1,
      locale: "zh-CN",
    });
    const page = await context.newPage();
    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", (message) => {
      if (message.type() === "error" && consoleErrors.length < 20) {
        consoleErrors.push(message.text().slice(0, 500));
      }
    });
    page.on("pageerror", (error) => {
      if (pageErrors.length < 20) pageErrors.push(String(error).slice(0, 500));
    });
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
    await page.evaluate(() => document.fonts?.ready);
    const horizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    );
    await page.screenshot({
      path: `${outputDirectory}/${viewport.name}.png`,
      fullPage: false,
      animations: "disabled",
    });
    results.push({
      name: viewport.name,
      width: viewport.width,
      height: viewport.height,
      horizontal_overflow: horizontalOverflow,
      console_errors: consoleErrors,
      page_errors: pageErrors,
    });
    await context.close();
  }
} finally {
  await browser.close();
}

await fs.writeFile(
  `${outputDirectory}/manifest.json`,
  JSON.stringify({ browser: "chromium", url, viewports: results }, null, 2),
  "utf8",
);
