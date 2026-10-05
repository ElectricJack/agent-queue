// Browser -> Vite proxy -> real disposable daemon -> PostgreSQL + vault.
// Invoked by test_dashboard_pool_toggle, never against an operator daemon.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";
import { createServer } from "vite";

const apiUrl = process.env.AQ_E2E_API_URL;
assert.ok(apiUrl && existsSync(join(process.env.AQ_E2E_HOME ?? "", ".aq-e2e")), "requires an owned disposable e2e world");
assert.equal(new URL(apiUrl).hostname, "127.0.0.1");
assert.ok(!["8081", "8082", "5173"].includes(new URL(apiUrl).port), "refusing an operator endpoint");
process.env.AQ_API_TARGET = apiUrl;
process.env.VITE_WS_URL = apiUrl.replace(/^http/, "ws");
process.env.VITE_API_URL = "";
const dashboardPort = Number(process.env.AQ_E2E_DASHBOARD_PORT);
assert.ok(dashboardPort > 0 && ![8081, 8082, 5173].includes(dashboardPort), "requires a disposable dashboard port");
const server = await createServer({
  root: fileURLToPath(new URL("../../dashboard", import.meta.url)),
  server: { host: "127.0.0.1", port: dashboardPort, strictPort: true },
});
let browser;
try {
  await server.listen();
  const { port } = server.httpServer.address();
  browser = await puppeteer.launch({
    executablePath: process.env.CHROME ?? "/usr/bin/google-chrome",
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const toggle = '[aria-label="Worker pools"] [role="switch"][aria-label$="worker pool"]';
  const heldReads = [];
  let holdReads = false;
  let refuseWrite = false;
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path === "/api/pool/status" && holdReads) {
      heldReads.push(request);
    } else if (path === "/api/pool/set-enabled" && refuseWrite) {
      void request.respond({ status: 504, contentType: "application/json", body: JSON.stringify({ error: "daemon_timeout" }) });
    } else {
      void request.continue();
    }
  });

  async function shown(enabled) {
    await page.waitForFunction((selector, value) => {
      const button = document.querySelector(selector);
      return button && !button.disabled && button.getAttribute("aria-checked") === String(value);
    }, { timeout: 2_000 }, toggle, enabled);
  }

  async function persisted(enabled) {
    const response = await fetch(`${apiUrl}/api/pool/status`, {
      method: "POST", headers: { "content-type": "application/json" }, body: "{}",
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.equal(result.pools.find((pool) => pool.profile_id === "worker")?.enabled, enabled);
  }

  await page.goto(`http://127.0.0.1:${port}/agents`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(toggle);
  await shown(true);
  for (const enabled of [false, true]) {
    // Hold every status refetch: a successful write must show its confirmed
    // state promptly without waiting for the entire fleet to be measured.
    holdReads = true;
    const responsePromise = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/pool/set-enabled");
    const started = performance.now();
    await page.click(toggle);
    const response = await responsePromise;
    assert.equal(response.status(), 200);
    const result = await response.json();
    assert.equal(result.success, true);
    assert.equal(result.enabled, enabled);
    assert.deepEqual(JSON.parse(response.request().postData()), { profile_id: "worker", enabled });
    await shown(enabled);
    const elapsed = Math.round(performance.now() - started);
    assert.ok(heldReads.length > 0, "status refresh was not delayed");
    await persisted(enabled);
    holdReads = false;
    await Promise.all(heldReads.splice(0).map((request) => request.continue().catch(() => {})));
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(toggle);
    await shown(enabled);
    console.log(`PASS browser ${enabled ? "enable" : "disable"} (${elapsed}ms): real API, persisted status, reload`);
  }

  refuseWrite = true;
  await page.click(toggle);
  await page.waitForFunction(() => [...document.querySelectorAll('[role="alert"]')].some((alert) => alert.textContent.includes("daemon_timeout")));
  await shown(true);
  await persisted(true);
  console.log("PASS browser transport failure: visible error, unchanged persisted state");
} finally {
  await browser?.close();
  await server.close();
}
