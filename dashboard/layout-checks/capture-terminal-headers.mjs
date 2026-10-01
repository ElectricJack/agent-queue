// Capture the same three-pane fixture against an already built bundle.
// node dashboard/layout-checks/capture-terminal-headers.mjs before|after
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";
import { startStubServer, loadFixtures } from "./server.mjs";
import { terminalHeaderFixtures, TERMINAL_SELECTION } from "./fixtures/terminal-headers.mjs";
import { terminalHeaderGeometry } from "./terminal-header-geometry.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const phase = process.argv[2] ?? "after";
if (!["before", "after"].includes(phase)) throw new Error("Expected before or after");
const out = join(here, "../../docs/reports/terminal-header-evidence");
mkdirSync(out, { recursive: true });
const fixtures = await loadFixtures(join(here, "fixtures"));
const browser = await puppeteer.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome", headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
const measurements = {};
try {
  for (const [name, viewport] of Object.entries({
    desktop: { width: 1440, height: 900 }, small: { width: 1100, height: 700 },
    phone: { width: 390, height: 844, isMobile: true, hasTouch: true },
  })) {
    const stub = await startStubServer({ distDir: join(here, "../dist"), fixtures });
    terminalHeaderFixtures(stub);
    const page = await browser.newPage();
    try {
      await page.setViewport(viewport);
      await page.goto(stub.url + TERMINAL_SELECTION, { waitUntil: "domcontentloaded" });
      await page.waitForFunction(() => document.querySelectorAll('section[aria-label$="agent window"]').length === 3);
      await new Promise((done) => setTimeout(done, 500));
      measurements[name] = await terminalHeaderGeometry(page);
      await page.screenshot({ path: join(out, `${phase}-${name}.png`) });
      if (name === "phone") {
        await page.$eval('section[aria-label="deep-high-claude pool agent window"]', (el) => el.scrollIntoView());
        await page.screenshot({ path: join(out, `${phase}-${name}-pool.png`) });
      }
      if (stub.unhandled.length) throw new Error(`Unhandled API requests: ${stub.unhandled}`);
    } finally {
      await page.close();
      await stub.close();
    }
  }
  writeFileSync(join(out, `${phase}.json`), JSON.stringify(measurements, null, 2) + "\n");
  console.log(`Captured ${phase} screenshots and geometry in ${out}`);
} finally {
  await browser.close();
}
