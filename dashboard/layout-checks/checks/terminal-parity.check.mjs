// The agent terminal is the host shell page's terminal (Jack, fleet-pinnacle-21)
// at every viewport, at every width. This check opens both on a phone — iPhone
// SE, iPhone 14, an Android and landscape — and compares them: the same colours,
// the same font, no watch/type mode on either, both attach at a size fitted to
// the phone, and a touch drag reaches earlier output on both. It also names the
// shared compact transport: tmux scrollback and restoring the session's
// window size after detaching. `phone-terminal` covers the
// agent terminal in depth; this is the comparison that keeps them the same.
import assert from "node:assert/strict";
import { expectLayout, rect, waitForText } from "../probes.mjs";
import { SESSION } from "../fixtures/base.mjs";
import { HOST_SHELL } from "../fixtures/host-shell.mjs";
import { TERMINAL_PHONES } from "../profiles.mjs";

export const name = "terminal-parity";
export const profiles = TERMINAL_PHONES;

const SHELL_HOST = "[data-interactive-terminal]";
const PHONE_HOST = "[data-interactive-terminal]";
const PHONE_STATUS = '[aria-label="worker-a terminal connection"]';
const MIN_COLUMNS = 40;
const RULER = 200;
const HISTORY_LINES = 300;

/** tmux history (numbered lines), a colour sample, then a ruler that wraps at the column count. */
const SCREEN = [
  ...Array.from({ length: HISTORY_LINES }, (_, i) => `history line ${String(i + 1).padStart(3, "0")}`),
  "\x1b[31mansi-red\x1b[0m \x1b[38;5;208mansi-256\x1b[0m \x1b[38;2;120;200;80mansi-true\x1b[0m \x1b[44mansi-bg\x1b[0m",
  "#".repeat(RULER),
  "$ ",
].join("\r\n");

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

/**
 * No watch/type mode anywhere: no control that switches the terminal between
 * watching and typing. (A "Type" *focus* button is not a mode — the host shell
 * page keeps one for a keyboard, and it moves focus rather than changing what
 * the terminal draws.)
 */
async function assertNoModeToggle(page, where) {
  for (const label of ['aria-label="Watch only"', 'aria-label="Watch"']) {
    assert.equal(await page.$(`[${label}]`), null, `${where} offers a watch/type mode`);
  }
  assert.equal(await page.$('input[type="radio"][name="mode"]'), null, `${where} offers a watch/type mode`);
}

async function poll(read, ok, timeout, message) {
  for (const end = Date.now() + timeout; ;) {
    const value = await read();
    if (ok(value)) return value;
    if (Date.now() > end) throw new Error(`${message}: ${JSON.stringify(value)}`);
    await sleep(50);
  }
}

/** The size tmux was last given: the attach query, then each resize. */
function reported(viewer) {
  const query = new URL(viewer.url, "http://stub.invalid").searchParams;
  let size = { cols: Number(query.get("cols")), rows: Number(query.get("rows")) };
  for (const frame of viewer.frames) {
    if (typeof frame !== "string" && frame.type === "resize") size = { cols: frame.cols, rows: frame.rows };
  }
  return size;
}

/** The drawn screen: its rows, its host and the ruler's first row (the column count). */
const drawn = (page, host) => page.$eval(host, (el) => {
  const rows = [...el.querySelectorAll(".xterm-rows > div")].map((row) => row.textContent ?? "");
  const box = el.getBoundingClientRect();
  const ruler = rows.map((row) => row.trimEnd()).filter((row) => /^#+$/.test(row)).map((row) => row.length);
  return { rows, hostWidth: box.width, hostHeight: box.height, cols: ruler.length ? Math.max(...ruler) : null };
});

/** Computed colours of the sample, the default text and every background the terminal paints. */
const palette = (page, host) => page.$eval(host, (el) => {
  const span = (text) => [...el.querySelectorAll(".xterm-rows span")].find((s) => s.textContent?.includes(text));
  const colour = (text, property) => {
    const found = span(text);
    return found ? getComputedStyle(found)[property] : null;
  };
  const rows = el.querySelector(".xterm-rows");
  return {
    font: getComputedStyle(rows).fontFamily,
    fontSize: getComputedStyle(rows).fontSize,
    lineHeight: getComputedStyle(rows).lineHeight,
    foreground: getComputedStyle(rows).color,
    background: [".xterm", ".xterm-viewport", ".xterm-scrollable-element", ".xterm-screen"]
      .map((selector) => { const node = el.querySelector(selector); return node ? getComputedStyle(node).backgroundColor : null; }),
    red: colour("ansi-red", "color"),
    indexed: colour("ansi-256", "color"),
    truecolor: colour("ansi-true", "color"),
    blueBackground: colour("ansi-bg", "backgroundColor"),
  };
});

/** The first numbered line in view, so a drag's reach is measurable. */
const topLine = (rows) => {
  for (const [index, row] of rows.entries()) {
    const line = row.match(/history line (\d+)/);
    if (line) return Number(line[1]) - index;
  }
  return null;
};

/** A finger dragged `distance` px down (toward earlier output), released while moving. */
async function flick(page, host, distance) {
  const box = await rect(page, host);
  const x = box.left + box.width / 2;
  const y = box.top + box.height * 0.2;
  await page.touchscreen.touchStart(x, y);
  for (let step = 1; step <= 8; step++) {
    await page.touchscreen.touchMove(x, y + (distance * step) / 8);
    await sleep(12);
  }
  await page.touchscreen.touchEnd();
}

/**
 * tmux's size once it has stopped moving. Three agreeing reads, because the
 * first fit happens before xterm's scrollbar takes its 14 px, and the refit
 * that follows is one more resize.
 */
async function stable(viewer, timeout = 6_000) {
  const end = Date.now() + timeout;
  let last = reported(viewer);
  let agrees = 0;
  for (;;) {
    await sleep(120);
    const next = reported(viewer);
    agrees = next.cols === last.cols && next.rows === last.rows ? agrees + 1 : 0;
    last = next;
    if (agrees >= 3 || Date.now() > end) return next;
  }
}

/** iOS keeps the layout viewport and shrinks the visual one: fake that, `covered` px of keyboard. */
const setKeyboard = (page, covered) => page.evaluate((px) => {
  const vv = window.visualViewport;
  if (!px) delete vv.height;
  else Object.defineProperty(vv, "height", { configurable: true, get: () => window.innerHeight - px });
  vv.dispatchEvent(new Event("resize"));
}, covered);

export async function run(t) {
  const vp = t.page.viewport();
  t.stub.allowTerminal(HOST_SHELL, SCREEN);
  t.stub.allowTerminal(SESSION, SCREEN);

  // ---- The host shell page, on the phone. This is the look the agent terminal must match.
  await t.page.goto(t.url("/host-shell"), { waitUntil: "domcontentloaded" });
  await t.page.waitForFunction((selector) => [...document.querySelectorAll(`${selector} .xterm-rows span`)]
    .some((s) => s.textContent?.includes("ansi-red")), { timeout: 15_000 }, SHELL_HOST);
  await waitForText(t.page, "Host shell");
  await poll(() => t.stub.terminalViewers.filter((r) => r.sessionId === HOST_SHELL).length, (n) => n === 1, 10_000,
    "the host shell page never attached");
  const shellViewer = t.stub.terminalViewers.find((r) => r.sessionId === HOST_SHELL);
  const shellSize = reported(shellViewer);
  const shellDrawn = await drawn(t.page, SHELL_HOST);
  assert.equal(shellDrawn.cols, shellSize.cols, `the host shell draws ${shellDrawn.cols} columns, tmux was given ${shellSize.cols}`);
  assert.ok(shellSize.cols >= MIN_COLUMNS, `only ${shellSize.cols} columns for the host shell`);
  const shellPalette = await palette(t.page, SHELL_HOST);
  assert.equal(shellPalette.background[1], "rgb(13, 17, 23)", "the host shell terminal viewport is not the theme background");
  // No watch/type mode on the host shell page either.
  await assertNoModeToggle(t.page, "the host shell page");
  await expectLayout(t);
  await t.shot("host-shell");

  // A drag reaches earlier output on the host shell page.
  const shellBefore = topLine((await drawn(t.page, SHELL_HOST)).rows);
  await flick(t.page, SHELL_HOST, (await rect(t.page, SHELL_HOST)).height * 0.5);
  await poll(async () => topLine((await drawn(t.page, SHELL_HOST)).rows), (line) => line !== null && line < shellBefore, 5_000,
    "a touch drag on the host shell terminal did not scroll back");

  // ---- The agent session terminal, same phone, same moment.
  await t.page.goto(t.url(`/focus/sessions/${SESSION}`), { waitUntil: "domcontentloaded" });
  await t.page.waitForFunction((selector) => document.querySelector(selector)?.textContent === "connected", { timeout: 15_000 }, PHONE_STATUS);
  await t.page.waitForFunction((selector) => [...document.querySelectorAll(`${selector} .xterm-rows span`)]
    .some((s) => s.textContent?.includes("ansi-red")), { timeout: 15_000 }, PHONE_HOST);
  const phoneViewer = t.stub.terminalViewers.filter((r) => r.sessionId === SESSION).pop();
  const phoneQuery = new URL(phoneViewer.url, "http://stub.invalid").searchParams;
  // xterm takes input directly, with the host shell's focus control.
  await assertNoModeToggle(t.page, "the phone's agent terminal");
  await t.page.waitForSelector('textarea[aria-label="worker-a terminal input"]');

  // ---- Same terminal: the colours are the host shell page's, not merely alike.
  const phonePalette = await palette(t.page, PHONE_HOST);
  assert.ok(phonePalette.red && phonePalette.indexed && phonePalette.truecolor && phonePalette.blueBackground,
    `the phone drew no colour sample: ${JSON.stringify(phonePalette)}`);
  assert.notEqual(phonePalette.red, phonePalette.foreground, "the phone drew red in the default colour");
  assert.equal(phonePalette.background[1], "rgb(13, 17, 23)", "the phone terminal viewport is not the theme background");
  assert.deepEqual(phonePalette, shellPalette, "the phone terminal and the host shell page draw different colours");
  await expectLayout(t, { primary: ['[aria-label="Focus worker-a terminal"]'] });
  await t.shot("agent-terminal");

  // ---- The agent terminal follows the phone: rotation, then the keyboard.
  const initial = await stable(phoneViewer);
  await t.page.setViewport({ ...vp, width: vp.height, height: vp.width, isLandscape: !vp.isLandscape });
  await poll(() => reported(phoneViewer), (size) => size.cols !== initial.cols, 5_000,
    "rotation left the agent terminal's size alone");
  await t.shot("agent-terminal-rotated");
  await t.page.setViewport(vp);
  // Back to the portrait size, within the one column xterm's own scrollbar
  // costs the first fit (it is not in the host's width yet, so the refit that
  // follows a rotation is the first that accounts for it).
  await poll(() => stable(phoneViewer), (size) => Math.abs(size.cols - initial.cols) <= 1, 5_000,
    `rotating back left ${JSON.stringify(initial.cols)} columns`);

  const covered = Math.round(vp.height * 0.4);
  await setKeyboard(t.page, covered);
  const typing = await poll(() => reported(phoneViewer), (size) => size.rows < initial.rows, 5_000,
    "the on-screen keyboard did not take rows from the agent terminal");
  // A keyboard never changes the width, so the column count may only move by
  // the one column xterm's own scrollbar costs the first fit.
  assert.ok(Math.abs(typing.cols - initial.cols) <= 1, `the keyboard changed the agent terminal's column count (${initial.cols} → ${typing.cols})`);
  await t.shot("agent-terminal-keyboard");
  await setKeyboard(t.page, 0);
  await poll(() => stable(phoneViewer), (size) => Math.abs(size.cols - initial.cols) <= 1 && size.rows === initial.rows, 5_000,
    "closing the keyboard did not give the rows back");

  // ---- Touch scroll reaches earlier output on the agent terminal, hundreds of lines back.
  await t.page.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur());
  const before = topLine((await drawn(t.page, PHONE_HOST)).rows);
  for (let i = 0; i < 90 && (topLine((await drawn(t.page, PHONE_HOST)).rows) ?? 1) !== 1; i++) {
    await flick(t.page, PHONE_HOST, (await rect(t.page, PHONE_HOST)).height * 0.5);
    await sleep(150);
  }
  await poll(async () => topLine((await drawn(t.page, PHONE_HOST)).rows), (line) => line === 1, 5_000,
    "flicks on the agent terminal never reached the first history line");
  assert.ok(before !== null, "the agent terminal showed no numbered history to scroll back through");
  assert.deepEqual(await t.page.evaluate(() => ({ x: window.scrollX, y: window.scrollY })), { x: 0, y: 0 },
    "the drag scrolled the page");
  await t.shot("agent-terminal-scrolled-back");

  // Both attaches carry the same transport: below the compact breakpoint
  // each asks tmux for scrollback
  // and for the window size back, at or above it neither does.
  const shellQuery = new URL(shellViewer.url, "http://stub.invalid").searchParams;
  const wide = vp.width >= 768;
  const transport = (query) => ({ history: query.get("history"), restore_size: query.get("restore_size") });
  assert.deepEqual(transport(phoneQuery), transport(shellQuery), "the two attaches asked tmux for different transport");
  assert.deepEqual(transport(phoneQuery), wide ? { history: null, restore_size: null } : { history: "2000", restore_size: "1" },
    `the attach asked for the wrong transport: ${phoneViewer.url}`);
  assert.ok(shellQuery.get("cols") && shellQuery.get("rows"), `the host shell attach carries no size: ${shellViewer.url}`);
}
