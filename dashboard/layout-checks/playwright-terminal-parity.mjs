// Playwright acceptance for fleet-pinnacle-21. Uses the built SPA and the same
// isolated fake PTY as the layout harness; never connects to the live daemon.
import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, devices } from "playwright-core";
import { loadFixtures, startStubServer } from "./server.mjs";
import { SESSION, POOL_SESSION } from "./fixtures/base.mjs";
import { HOST_SHELL } from "./fixtures/host-shell.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const outIndex = process.argv.indexOf("--out");
const out = outIndex < 0 ? join(here, ".out", "playwright") : process.argv[outIndex + 1];
mkdirSync(out, { recursive: true });
const fixtures = await loadFixtures(join(here, "fixtures"));
const HOST = "[data-interactive-terminal]";
const SCREEN = [
  ...Array.from({ length: 300 }, (_, i) => `history line ${String(i + 1).padStart(3, "0")}`),
  "\x1b[31mansi-red\x1b[0m", "\x1b[38;5;208mansi-256\x1b[0m",
  "\x1b[38;2;120;200;80mansi-true\x1b[0m", "\x1b[44mansi-bg\x1b[0m",
  "#".repeat(200), "$ ",
].join("\r\n");
const profiles = [
  { name: "phone-390", device: { ...devices["iPhone 14"], viewport: { width: 390, height: 844 } } },
  { name: "android-412", device: { ...devices["Pixel 7"], viewport: { width: 412, height: 915 } } },
  { name: "phone-320", device: { ...devices["iPhone SE"], viewport: { width: 320, height: 568 } } },
];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(read, accept, message) {
  const deadline = Date.now() + 10_000;
  let value;
  do {
    value = await read();
    if (accept(value)) return value;
    await sleep(50);
  } while (Date.now() < deadline);
  throw new Error(`${message}: ${JSON.stringify(value)}`);
}
function sizeOf(viewer) {
  const query = new URL(viewer.url, "http://stub.invalid").searchParams;
  let size = { cols: Number(query.get("cols")), rows: Number(query.get("rows")) };
  for (const frame of viewer.frames) if (frame.type === "resize") size = { cols: frame.cols, rows: frame.rows };
  return size;
}
const measure = (page) => page.locator(HOST).evaluate((host) => {
  const rows = [...host.querySelectorAll(".xterm-rows > div")].map((row) => row.textContent ?? "");
  const box = host.getBoundingClientRect();
  const screen = host.querySelector(".xterm-screen").getBoundingClientRect();
  const numbered = rows.find((row) => /history line \d+/.test(row));
  return {
    rows: rows.length, width: box.width, height: box.height, bottom: box.bottom,
    screenWidth: screen.width, screenHeight: screen.height,
    firstLine: numbered ? Number(numbered.match(/history line (\d+)/)[1]) : null,
  };
});
const palette = (page) => page.locator(HOST).evaluate((host) => {
  const rows = getComputedStyle(host.querySelector(".xterm-rows"));
  const sample = (text, property) => {
    const span = [...host.querySelectorAll(".xterm-rows span")].find((el) => el.textContent.includes(text));
    return span ? getComputedStyle(span)[property] : null;
  };
  return {
    font: rows.fontFamily, fontSize: rows.fontSize, lineHeight: rows.lineHeight, foreground: rows.color,
    backgrounds: [".xterm", ".xterm-viewport", ".xterm-screen"]
      .map((selector) => getComputedStyle(host.querySelector(selector)).backgroundColor),
    red: sample("ansi-red", "color"), indexed: sample("ansi-256", "color"),
    truecolor: sample("ansi-true", "color"), blueBackground: sample("ansi-bg", "backgroundColor"),
  };
});
async function fitted(page, viewer) {
  return until(async () => ({ shown: await measure(page), size: sizeOf(viewer) }), ({ shown, size }) => {
    const cell = shown.screenWidth / size.cols;
    const row = shown.screenHeight / size.rows;
    return size.cols > 0 && size.rows > 0 && shown.rows === size.rows
      && shown.width - shown.screenWidth - 14 >= -1 && shown.width - shown.screenWidth - 14 < cell + 1
      && shown.height - shown.screenHeight >= -1 && shown.height - shown.screenHeight < row + 1;
  }, "the drawn grid does not match the PTY size");
}
async function keyboard(page, covered) {
  await page.evaluate((px) => {
    const viewport = window.visualViewport;
    if (px) Object.defineProperty(viewport, "height", { configurable: true, get: () => window.innerHeight - px });
    else delete viewport.height;
    viewport.dispatchEvent(new Event("resize"));
  }, covered);
}
async function flick(page, cdp) {
  const box = await page.locator(HOST).boundingBox();
  const x = box.x + box.width / 2;
  const y = box.y + box.height * 0.15;
  const touch = async (type, at) => cdp.send("Input.dispatchTouchEvent", {
    type, touchPoints: at === null ? [] : [{ x, y: at }],
  });
  await touch("touchStart", y);
  for (let step = 1; step <= 8; step++) {
    await touch("touchMove", y + box.height * 0.65 * step / 8);
    await sleep(12);
  }
  await touch("touchEnd", null);
}

const browser = await chromium.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome", headless: true });
const results = [];
try {
  for (const profile of profiles) {
    const stub = await startStubServer({ distDir: join(here, "..", "dist"), fixtures });
    const { defaultBrowserType: _browserType, ...device } = profile.device;
    const context = await browser.newContext(device);
    const page = await context.newPage();
    // SessionDetail opens its transcript before selecting the live Pane tab.
    // Finish that unrelated SSE stream and supply its shell attach command.
    await page.route("**/api/sessions/*/stream", (route) => route.fulfill({
      status: 200, contentType: "text/event-stream", body: "event: complete\ndata: {}\n\n",
    }));
    stub.override("POST /api/system/session-attach", () => ({
      success: true, attach_command: "tmux attach -t fixture-session",
    }));
    const cdp = await context.newCDPSession(page);
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    const shot = (label) => page.screenshot({ path: join(out, `${profile.name}-${label}.png`) });
    const open = async (path, sessionId, selectPane = false, pushPane = false) => {
      stub.allowTerminal(sessionId, SCREEN);
      await page.goto(stub.url + path);
      if (selectPane) await page.getByRole("tab", { name: "Pane", exact: true }).click();
      if (pushPane) await until(async () => {
        // Compact viewports deliberately suppress restoring a desktop pane.
        // Exercise the real pane-open event path instead of changing that rule.
        stub.pushEvent({ event_type: "message.sent", to_kind: "user", to_id: "dashboard",
          message_id: "fixture-pane-open", pane_open: { view: "session-peek", args: { sessionId } } });
        return page.locator(HOST).count();
      }, (count) => count === 1, "the session-peek pane never opened");
      await page.locator(`${HOST} .xterm-rows`).filter({ hasText: "ansi-red" }).waitFor();
      const viewer = stub.terminalViewers.filter((row) => row.sessionId === sessionId).at(-1);
      assert.ok(viewer, `no attach for ${path}`);
      await fitted(page, viewer);
      assert.equal(await page.locator('[aria-label="Watch only"], [data-phone-terminal], [data-terminal-input-bar]').count(), 0);
      assert.equal(await page.getByRole("button", { name: /^Focus .+ terminal$/ }).count(), 1);
      const query = new URL(viewer.url, stub.url).searchParams;
      assert.equal(query.get("history"), "2000");
      assert.equal(query.get("restore_size"), "1");
      console.log(`checked ${profile.name}: ${pushPane ? "session-peek" : path}`);
      return viewer;
    };
    try {
      await open("/host-shell", HOST_SHELL);
      const reference = await palette(page);
      assert.equal(reference.fontSize, "12px");
      assert.equal(reference.backgrounds[1], "rgb(13, 17, 23)");
      for (const color of ["red", "indexed", "truecolor", "blueBackground"]) assert.ok(reference[color]);
      const shellImage = await shot("host-shell");

      const viewer = await open(`/focus/sessions/${SESSION}`, SESSION);
      assert.deepEqual(await palette(page), reference);
      const agentImage = await shot("agent-terminal");
      const initial = sizeOf(viewer);
      const viewport = page.viewportSize();
      await page.setViewportSize({ width: viewport.height, height: viewport.width });
      await until(() => sizeOf(viewer), (size) => size.cols > initial.cols, "rotation did not resize the PTY");
      await fitted(page, viewer);
      await page.setViewportSize(viewport);
      await until(() => sizeOf(viewer), (size) => Math.abs(size.cols - initial.cols) <= 1 && size.rows === initial.rows,
        "rotation back did not restore rows and columns");
      const covered = Math.round(viewport.height * 0.4);
      await keyboard(page, covered);
      await until(() => sizeOf(viewer), (size) => size.rows < initial.rows, "the keyboard did not resize the PTY");
      const typing = await fitted(page, viewer);
      assert.ok(typing.shown.bottom <= viewport.height - covered + 1, "terminal rows extend behind the keyboard");
      await shot("agent-terminal-keyboard");
      await keyboard(page, 0);
      await until(() => sizeOf(viewer), (size) => size.rows === initial.rows, "closing the keyboard did not restore rows");
      await page.locator(HOST).tap();
      await page.keyboard.type("phone parity");
      await page.keyboard.press("Enter");
      await until(() => stub.typed(SESSION).join(""), (bytes) => bytes === "phone parity\r", "xterm input did not reach the attach");
      await page.locator(`${HOST} textarea`).evaluate((input) => input.blur());
      const before = (await measure(page)).firstLine;
      await flick(page, cdp);
      await until(() => measure(page), (shown) => shown.firstLine !== null && shown.firstLine < before, "touch did not reach earlier output");
      for (let i = 0; i < 30 && (await measure(page)).firstLine !== 1; i++) {
        await flick(page, cdp);
        await sleep(150);
      }
      await until(() => measure(page), (shown) => shown.firstLine === 1, "touch did not reach the first history line");
      assert.deepEqual(await page.evaluate(() => [window.scrollX, window.scrollY]), [0, 0]);
      await shot("agent-terminal-scrolled-back");

      // Every entry point reuses the reference terminal, including the former
      // snapshot-only session-peek pane and the full session's Pane tab.
      for (const [path, sessionId, pane] of [
        ["/agents?agent=worker-a", SESSION],
        ["/agents?agent=pool%3Adeep-high-claude", POOL_SESSION],
        [`/sessions/${SESSION}`, SESSION, true],
      ]) {
        await open(path, sessionId, pane);
        assert.deepEqual(await palette(page), reference, `${path} changed the terminal palette`);
      }
      await open("/agents", SESSION, false, true);
      assert.deepEqual(await palette(page), reference, "session-peek changed the terminal palette");
      const paneViewer = stub.terminalViewers.filter((row) => row.sessionId === SESSION).at(-1);
      await keyboard(page, covered);
      const paneTyping = await fitted(page, paneViewer);
      assert.ok(paneTyping.shown.bottom <= viewport.height - covered + 1, "docked terminal extends behind the keyboard");
      assert.deepEqual(errors, []);
      assert.deepEqual(stub.unhandled, []);

      // A browser composes the exact screenshots so the side-by-side artifact
      // needs no image-editing dependencies or lossy recompression.
      const comparison = await context.newPage();
      await comparison.setViewportSize({ width: viewport.width * 2, height: viewport.height + 32 });
      await comparison.setContent(`<meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0;background:#0d1117;color:#d1d5db;font:14px monospace}main{display:flex}figure{margin:0;width:${viewport.width}px}figcaption{height:32px;display:grid;place-items:center}img{display:block;width:100%}</style><main><figure><figcaption>Host shell</figcaption><img src="data:image/png;base64,${shellImage.toString("base64")}"></figure><figure><figcaption>Agent terminal</figcaption><img src="data:image/png;base64,${agentImage.toString("base64")}"></figure></main>`);
      await comparison.locator("img").first().evaluate((img) => img.decode());
      await comparison.locator("img").last().evaluate((img) => img.decode());
      await comparison.screenshot({ path: join(out, `side-by-side-${profile.name}.png`) });
      results.push({ profile: profile.name, ok: true, initial, keyboard: typing.size });
      console.log(`ok Playwright terminal parity @ ${profile.name}`);
    } catch (error) {
      await shot("failure");
      results.push({ profile: profile.name, ok: false, error: String(error.stack ?? error) });
      console.error(`FAIL ${profile.name}: ${error.message}`);
    } finally {
      await context.close();
      await stub.close();
    }
  }
} finally {
  writeFileSync(join(out, "playwright-report.json"), JSON.stringify({ browser: browser.version(), results }, null, 2));
  await browser.close();
}
if (results.some((result) => !result.ok)) process.exitCode = 1;
