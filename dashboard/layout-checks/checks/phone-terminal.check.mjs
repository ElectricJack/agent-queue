// The agent terminal at a phone viewport (Jack, fleet-pinnacle-21): one window
// at every width, the same one the host shell page draws. It attaches at a size
// fitted to the phone, asks tmux for its scrollback and for the agent's window
// size back, follows rotation and the on-screen keyboard, and a touch drag
// reaches earlier output. There is no watch/type mode and no phone chrome.
// `terminal-parity` compares this terminal with the host shell page's;
// `terminal-headers` covers the header at every profile.
import assert from "node:assert/strict";
import { expectLayout, rect } from "../probes.mjs";
import { SESSION } from "../fixtures/base.mjs";
import { TERMINAL_PHONES } from "../profiles.mjs";

export const name = "phone-terminal";
export const profiles = TERMINAL_PHONES;

const HOST = "[data-interactive-terminal]";
const STATUS = '[aria-label="worker-a terminal connection"]';
/** FitAddon always keeps this much of the width for xterm's scrollbar. */
const SCROLLBAR_PX = 14;
const MIN_COLUMNS = 40;
const RULER = 200;
const HISTORY_LINES = 300;
const OUTPUT_LINES = 60;

/** tmux history (numbered lines), a colour sample, a ruler that wraps at the column count, then the prompt. */
const SCREEN = [
  ...Array.from({ length: HISTORY_LINES }, (_, i) => `history line ${String(i + 1).padStart(3, "0")}`),
  "\x1b[31mansi-red\x1b[0m \x1b[38;5;208mansi-256\x1b[0m \x1b[38;2;120;200;80mansi-true\x1b[0m \x1b[44mansi-bg\x1b[0m",
  "#".repeat(RULER),
  "claimed: fixture-task-1",
  "$ ",
].join("\r\n");

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
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

/** What the phone shows: row texts, the screen and its host, and the ruler's first row (the column count). */
const screen = (t) => t.page.$eval(HOST, (host) => {
  const rows = [...host.querySelectorAll(".xterm-rows > div")].map((row) => row.textContent ?? "");
  const box = host.getBoundingClientRect();
  const drawn = host.querySelector(".xterm-screen").getBoundingClientRect();
  const ruler = rows.map((row) => row.trimEnd()).filter((row) => /^#+$/.test(row)).map((row) => row.length);
  return { rows, hostWidth: box.width, hostHeight: box.height, width: drawn.width, height: drawn.height, rulerCols: ruler.length ? Math.max(...ruler) : null };
});

/** Where the view is: the ordinal of its top row, from the first numbered line in view. */
const topLine = (rows) => {
  for (const [index, row] of rows.entries()) {
    const line = row.match(/(history|output) line (\d+)/);
    if (line) return (line[1] === "output" ? HISTORY_LINES : 0) + Number(line[2]) - index;
  }
  return null;
};

/** A finger dragged `distance` px down (toward earlier output), released while moving. */
async function flick(t, distance) {
  const box = await rect(t.page, HOST);
  const x = box.left + box.width / 2;
  const y = box.top + box.height * 0.2;
  await t.page.touchscreen.touchStart(x, y);
  for (let step = 1; step <= 8; step++) {
    await t.page.touchscreen.touchMove(x, y + (distance * step) / 8);
    await sleep(12);
  }
  await t.page.touchscreen.touchEnd();
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
const setKeyboard = (t, covered) => t.page.evaluate((px) => {
  const vv = window.visualViewport;
  if (!px) delete vv.height;
  else Object.defineProperty(vv, "height", { configurable: true, get: () => window.innerHeight - px });
  vv.dispatchEvent(new Event("resize"));
}, covered);

export async function run(t) {
  const wide = t.page.viewport().width >= 768;
  t.stub.allowTerminal(SESSION, SCREEN);
  await t.page.goto(t.url(`/focus/sessions/${SESSION}`), { waitUntil: "domcontentloaded" });
  await t.page.waitForFunction((selector) => document.querySelector(selector)?.textContent === "connected", { timeout: 15_000 }, STATUS);
  const vp = t.page.viewport();

  // One attach, sized to the phone, asking for tmux's scrollback and for the
  // agent's window size back. The host shell uses the same compact transport.
  const viewers = () => t.stub.terminalViewers.filter((row) => row.sessionId === SESSION);
  assert.equal(viewers().length, 1, "the phone opened more than one attach");
  const viewer = viewers()[0];
  const query = new URL(viewer.url, "http://stub.invalid").searchParams;
  assert.ok(Number(query.get("cols")) > 0 && Number(query.get("rows")) > 0, `the attach carries no size: ${viewer.url}`);
  assert.equal(query.get("history"), wide ? null : "2000", `wrong history in the attach: ${viewer.url}`);
  assert.equal(query.get("restore_size"), wide ? null : "1", `wrong restore_size in the attach: ${viewer.url}`);

  // No watch/type mode and no phone chrome: the terminal and its typing surface.
  assert.equal(await t.page.$('[aria-label="Watch only"]'), null, "a Watch only toggle is back");
  await t.page.waitForSelector('textarea[aria-label="worker-a terminal input"]');
  await t.page.waitForSelector('button[aria-label="Focus worker-a terminal"]');
  await expectLayout(t);

  /** tmux's size is the size shown, and it fills the terminal area. */
  const filled = async (label) => {
    t.stub.terminalWrite(SESSION, `\r\n${"#".repeat(RULER)}\r\n`);
    const size = reported(viewer);
    const shown = await screen(t);
    const cell = shown.width / size.cols;
    const row = shown.height / size.rows;
    const slackX = shown.hostWidth - SCROLLBAR_PX - shown.width;
    const slackY = shown.hostHeight - shown.height;
    const ok = shown.rows.length === size.rows && shown.rulerCols === size.cols
      && slackX > -1 && slackX < cell + 1 && slackY > -1 && slackY < row + 1;
    if (!ok) throw new Error(`${label}: the terminal shows ${shown.rows.length}×${shown.rulerCols} in ${Math.round(shown.width)}×${Math.round(shown.height)} (+${Math.round(slackX)},+${Math.round(slackY)}), tmux was given ${size.cols}×${size.rows}`);
    assert.ok(size.cols >= MIN_COLUMNS, `${label}: only ${size.cols} columns`);
    return size;
  };
  /** tmux's size after a layout change, once the last resize frame has landed. */
  const sized = (cols, rows) => poll(() => stable(viewer),
    (size) => (cols === null || size.cols === cols) && (rows === null || size.rows === rows), 5_000,
    `tmux was never given ${cols ?? "any"}×${rows ?? "any"} columns (it has ${JSON.stringify(reported(viewer))})`);

  const initial = await poll(() => filled("on load").catch(() => null), (size) => !!size, 5_000, "the terminal never filled its area");
  await t.shot("live");

  // Rotation: the size follows, then comes back.
  await t.page.setViewport({ ...vp, width: vp.height, height: vp.width, isLandscape: !vp.isLandscape });
  await poll(() => stable(viewer), (size) => size.cols !== initial.cols, 5_000, "rotation left the column count alone");
  await t.shot("rotated");
  await t.page.setViewport(vp);
  await sized(null, initial.rows);

  // The on-screen keyboard covers the terminal without changing the layout
  // viewport, so tmux follows the visual one; closing it gives the rows back.
  const covered = Math.round(vp.height * 0.4);
  await setKeyboard(t, covered);
  const typing = await poll(() => stable(viewer), (size) => size.rows < initial.rows, 5_000,
    "the on-screen keyboard did not take rows from the terminal");
  // A keyboard never changes the width, so the column count may only move by
  // the one column xterm's own scrollbar costs the first fit.
  assert.ok(Math.abs(typing.cols - initial.cols) <= 1, `the keyboard changed the column count (${initial.cols} → ${typing.cols})`);
  await t.shot("keyboard");
  await setKeyboard(t, 0);
  await poll(() => stable(viewer), (size) => Math.abs(size.cols - initial.cols) <= 1, 5_000,
    `rotating back left ${JSON.stringify(initial.cols)} columns`);

  // Typing goes over the attach: xterm's own textarea, as on the host shell page.
  await t.page.tap(HOST);
  await t.page.keyboard.type("hello from the phone");
  await t.page.keyboard.press("Enter");
  // xterm sends what the keyboard sends: one key per frame, then Enter.
  await poll(() => t.stub.typed(SESSION).join(""), (typed) => typed === "hello from the phone\r", 5_000,
    `typing never reached the attach (${JSON.stringify(t.stub.typed(SESSION))})`);
  assert.equal(t.stub.typed(SESSION).at(-1), "\r", "Enter did not follow the line");

  // A drag scrolls the terminal, not the page, and keeps going after the finger lifts.
  t.stub.terminalWrite(SESSION, Array.from({ length: OUTPUT_LINES }, (_, i) => `\r\noutput line ${String(i + 1).padStart(3, "0")}`).join(""));
  await poll(async () => (await screen(t)).rows,
    (rows) => rows.some((row) => row.includes(`output line ${String(OUTPUT_LINES).padStart(3, "0")}`)), 5_000, "live output never drew");
  const before = topLine((await screen(t)).rows);
  await flick(t, (await rect(t.page, HOST)).height * 0.5);
  const released = topLine((await screen(t)).rows);
  assert.ok(before !== null && released !== null && released < before, `the drag did not scroll back (top line ${before} → ${released})`);
  await poll(async () => topLine((await screen(t)).rows), (line) => line < released, 2_000,
    "the terminal stopped when the finger lifted (no momentum)");

  // Flicks reach the first history line, hundreds of lines back.
  for (let i = 0; i < 90 && topLine((await screen(t)).rows) !== 1; i++) {
    await flick(t, (await rect(t.page, HOST)).height * 0.5);
    await sleep(150);
  }
  await poll(async () => topLine((await screen(t)).rows), (line) => line === 1, 5_000, "flicks never reached the first history line");
  assert.deepEqual(await t.page.evaluate(() => ({ x: window.scrollX, y: window.scrollY })), { x: 0, y: 0 }, "the drag scrolled the page");
  await expectLayout(t);
  await t.shot("scrolled-back");

  // One attach throughout; besides input only sizes, flow-control credit and
  // keepalive; nothing remembered.
  assert.equal(viewers().length, 1, "the phone reattached");
  const controls = viewer.frames.filter((frame) => typeof frame !== "string");
  assert.ok(controls.every((frame) => ["resize", "ack", "ping"].includes(frame.type)), `the attach got ${JSON.stringify(controls)}`);
  assert.deepEqual(t.stub.statePuts(), [], "the phone terminal wrote roaming preferences");

  // Leaving closes the attach, which is when the daemon gives the agent its size back.
  await t.page.goto(t.url("/focus"), { waitUntil: "domcontentloaded" });
  await poll(() => viewer.open, (open) => !open, 5_000, "leaving left the attach open");
}
