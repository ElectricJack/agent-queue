// The phone terminal (mobile terminal spec,
// docs/superpowers/specs/2026-10-08-mobile-terminal-design.md): one attach,
// sized to the phone, with tmux history ahead of the screen. Its columns and
// rows fill the terminal area and follow rotation and the on-screen keyboard; a
// drag scrolls back through earlier output, with momentum, and never the page;
// a tap opens the input bar, which types over the same attach with no
// watch/type toggle. The colours match the desktop terminal's.
import assert from "node:assert/strict";
import { expectLayout, rect } from "../probes.mjs";
import { SESSION } from "../fixtures/base.mjs";
import { TERMINAL_PHONES } from "../profiles.mjs";

export const name = "phone-terminal";
export const profiles = TERMINAL_PHONES;

const STATUS = '[aria-label="worker-a terminal status"]';
const HOST = "[data-phone-terminal]";
const INPUT = 'textarea[aria-label="worker-a terminal input"]';
const STRIP = '[aria-label="worker-a terminal keys"]';
const SEND = '[aria-label="Send to worker-a"]';
const JUMP = '[aria-label="Jump to latest output"]';
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
const focused = (t) => t.page.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? null);
const pageScroll = (t) => t.page.evaluate(() => ({ x: window.scrollX, y: window.scrollY }));

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
/** Where the view is: the ordinal of its top row, from the first numbered line in view (history, then output). */
const topLine = (rows) => {
  for (const [index, row] of rows.entries()) {
    const line = row.match(/(history|output) line (\d+)/);
    if (line) return (line[1] === "output" ? HISTORY_LINES : 0) + Number(line[2]) - index;
  }
  return null;
};

/**
 * tmux's size is the size shown, and it fills the terminal area: less than one
 * cell is left over either way. A fresh ruler at the bottom keeps a full ruler
 * row in view however few rows the keyboard leaves. xterm reflows it on resize
 * only with the cursor below it: the cursor's own line is left for the program
 * to redraw, as tmux does and this stub does not.
 */
async function expectFilled(t, viewer, label) {
  t.stub.terminalWrite(SESSION, `\r\n${"#".repeat(RULER)}\r\n`);
  const measure = async () => {
    const size = reported(viewer);
    const shown = await screen(t);
    const cell = shown.width / size.cols;
    const row = shown.height / size.rows;
    return {
      size, cell, row, shown: { rows: shown.rows.length, cols: shown.rulerCols },
      matches: shown.rows.length === size.rows && shown.rulerCols === size.cols,
      slackX: shown.hostWidth - SCROLLBAR_PX - shown.width,
      slackY: shown.hostHeight - shown.height,
    };
  };
  const fills = (m) => m.matches && m.slackX > -1 && m.slackX < m.cell + 1 && m.slackY > -1 && m.slackY < m.row + 1;
  // The resize lands a frame or two after the viewport changes; until then the old size still matches.
  const last = await poll(measure, fills, 5_000, `${label}: the terminal does not fill its area at the size tmux was given`);
  assert.ok(last.size.cols >= MIN_COLUMNS, `${label}: only ${last.size.cols} columns`);
  return last.size;
}

/** iOS keeps the layout viewport and shrinks the visual one: fake that, `covered` px of keyboard. */
const setKeyboard = (t, covered) => t.page.evaluate((px) => {
  const vv = window.visualViewport;
  if (!px) delete vv.height;
  else {
    const height = window.innerHeight - px;
    Object.defineProperty(vv, "height", { configurable: true, get: () => height });
  }
  vv.dispatchEvent(new Event("resize"));
}, covered);

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

/** Computed colours of the sample, the default text and the background. */
const palette = (t, root) => t.page.$eval(root, (el) => {
  const span = (text) => [...el.querySelectorAll(".xterm-rows span")].find((s) => s.textContent?.includes(text));
  const colour = (text, property) => {
    const found = span(text);
    return found ? getComputedStyle(found)[property] : null;
  };
  const rows = el.querySelector(".xterm-rows");
  return {
    font: getComputedStyle(rows).fontFamily,
    foreground: getComputedStyle(rows).color,
    background: [".xterm", ".xterm-viewport", ".xterm-scrollable-element", ".xterm-screen"]
      .map((selector) => { const node = el.querySelector(selector); return node ? getComputedStyle(node).backgroundColor : null; }),
    red: colour("ansi-red", "color"),
    indexed: colour("ansi-256", "color"),
    truecolor: colour("ansi-true", "color"),
    blueBackground: colour("ansi-bg", "backgroundColor"),
  };
});

export async function run(t) {
  t.stub.allowTerminal(SESSION, SCREEN);
  await t.page.goto(t.url(`/focus/sessions/${SESSION}`), { waitUntil: "domcontentloaded" });
  await t.page.waitForFunction((selector) => document.querySelector(selector)?.textContent === "Live", { timeout: 10_000 }, STATUS);
  const vp = t.page.viewport();

  // One attach, sized to the phone, asking for history and for the agent's size back.
  const viewers = () => t.stub.terminalViewers.filter((row) => row.sessionId === SESSION);
  assert.equal(viewers().length, 1, "the phone opened more than one attach");
  const viewer = viewers()[0];
  const query = new URL(viewer.url, "http://stub.invalid").searchParams;
  assert.ok(Number(query.get("cols")) > 0 && Number(query.get("rows")) > 0, `the attach carries no size: ${viewer.url}`);
  assert.equal(query.get("history"), "2000", `the attach asks for no history: ${viewer.url}`);
  assert.equal(query.get("restore_size"), "1", `the attach leaves the agent's window at the phone's size: ${viewer.url}`);

  // No watch/type mode: the input bar and the key strip are there from the start.
  assert.equal(await t.page.$("xpath/.//button[normalize-space()='Type']"), null, "a Type toggle is back");
  assert.equal(await t.page.$('button[aria-label="Watch only"]'), null, "a Watch only toggle is back");
  await t.page.waitForSelector(`${STRIP} button:not([disabled])`);
  await t.page.waitForSelector(INPUT);
  await expectLayout(t, { primary: [`${STRIP} button`, SEND] });
  const strip = await rect(t.page, STRIP);
  assert.ok(strip.left >= -1 && strip.right <= vp.width + 1, `the key strip leaves the viewport (${strip.left}–${strip.right})`);
  const bar = await rect(t.page, INPUT);
  assert.ok(bar.bottom <= vp.height + 1 && bar.height >= 44, `the input bar is off screen or short (${bar.top}–${bar.bottom})`);

  // The columns and rows fill the screen, and tmux has exactly that size.
  const initial = await expectFilled(t, viewer, "on load");
  await t.shot("live");

  // Rotation: the size follows, then comes back.
  await t.page.setViewport({ ...vp, width: vp.height, height: vp.width, isLandscape: !vp.isLandscape });
  const rotated = await expectFilled(t, viewer, "rotated");
  assert.notEqual(rotated.cols, initial.cols, "rotation left the column count alone");
  await t.shot("rotated");
  await t.page.setViewport(vp);
  assert.deepEqual(await expectFilled(t, viewer, "rotated back"), initial);

  // The on-screen keyboard: the terminal keeps what the keyboard leaves, the
  // input bar stays above it, and tmux gets fewer rows; closing it gives them back.
  await t.page.tap(HOST);
  assert.equal(await focused(t), "worker-a terminal input", "a tap on the terminal did not focus the input bar");
  const covered = Math.round(vp.height * 0.4);
  await setKeyboard(t, covered);
  await t.page.waitForSelector("[data-keyboard-open]");
  const typingSize = await expectFilled(t, viewer, "keyboard open");
  assert.ok(typingSize.rows < initial.rows, `the keyboard left ${typingSize.rows} of ${initial.rows} rows`);
  assert.equal(typingSize.cols, initial.cols, "the keyboard changed the column count");
  const raised = await rect(t.page, INPUT);
  assert.ok(raised.bottom <= vp.height - covered + 1, `the input bar is behind the keyboard (${raised.bottom} > ${vp.height - covered})`);
  await t.shot("keyboard");
  await setKeyboard(t, 0);
  await t.page.waitForFunction(() => !document.querySelector("[data-keyboard-open]"));
  assert.deepEqual(await expectFilled(t, viewer, "keyboard closed"), initial);

  // Typing goes over the attach: a line, then Enter as its own frame.
  await t.page.keyboard.type("hello from the phone");
  await t.page.keyboard.press("Enter");
  await poll(() => t.stub.typed(SESSION), (typed) => typed.length >= 2, 5_000, "the line and its Enter never reached the attach");
  assert.deepEqual(t.stub.typed(SESSION), ["hello from the phone", "\r"]);
  assert.equal(await t.page.$eval(INPUT, (el) => el.value), "", "the sent line stayed in the input bar");

  // The key strip: each tap is its bytes, and focus (the on-screen keyboard) stays put.
  for (const key of ["Send 2", "Send Escape", "Send Ctrl-C", "Send Up arrow", "Send Enter"]) {
    await t.page.tap(`[aria-label="${key}"]`);
  }
  await poll(() => t.stub.typed(SESSION), (typed) => typed.length >= 7, 5_000, "key strip taps never reached the attach");
  assert.deepEqual(t.stub.typed(SESSION).slice(2), ["2", "\x1b", "\x03", "\x1b[A", "\r"]);
  assert.equal(await focused(t), "worker-a terminal input", "a key tap took focus from the input bar (the keyboard would close)");
  const clipped = await t.page.$$eval(`${STRIP} button`, (keys) =>
    keys.filter((key) => key.scrollWidth > key.clientWidth).map((key) => key.textContent));
  assert.deepEqual(clipped, [], "a key label is wider than its key");

  // Several lines (Shift+Enter) go as one bracketed paste with CR line ends, then Enter.
  await t.page.keyboard.type("line one");
  await t.page.keyboard.down("Shift");
  await t.page.keyboard.press("Enter");
  await t.page.keyboard.up("Shift");
  await t.page.keyboard.type("line two");
  await t.page.tap(SEND);
  await poll(() => t.stub.typed(SESSION), (typed) => typed.length >= 9, 5_000, "the multi-line entry never reached the attach");
  assert.deepEqual(t.stub.typed(SESSION).slice(7), ["\x1b[200~line one\rline two\x1b[201~", "\r"]);
  await t.shot("typed");

  // A drag scrolls the terminal, not the page, and keeps going after the finger lifts.
  t.stub.terminalWrite(SESSION, Array.from({ length: OUTPUT_LINES }, (_, i) => `\r\noutput line ${String(i + 1).padStart(3, "0")}`).join(""));
  await poll(async () => (await screen(t)).rows, (rows) => rows.some((row) => row.includes(`output line ${String(OUTPUT_LINES).padStart(3, "0")}`)), 3_000, "live output never drew");
  await t.page.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur());
  const before = topLine((await screen(t)).rows);
  await flick(t, (await rect(t.page, HOST)).height * 0.5);
  const released = topLine((await screen(t)).rows);
  assert.ok(before !== null && released !== null && released < before, `the drag did not scroll back (top line ${before} → ${released})`);
  const glided = await poll(async () => topLine((await screen(t)).rows), (line) => line < released, 2_000,
    "the terminal stopped when the finger lifted (no momentum)");
  assert.ok(glided < released);
  await t.page.waitForSelector(JUMP);
  assert.notEqual(await focused(t), "worker-a terminal input", "a drag focused the input bar (it is not a tap)");

  // Flicks reach the first history line, hundreds of lines back.
  for (let i = 0; i < 80 && topLine((await screen(t)).rows) !== 1; i++) {
    await flick(t, (await rect(t.page, HOST)).height * 0.5);
    await sleep(200);
  }
  await poll(async () => topLine((await screen(t)).rows), (line) => line === 1, 3_000, "flicks never reached the first history line");
  assert.deepEqual(await pageScroll(t), { x: 0, y: 0 }, "the drag scrolled the page");
  await expectLayout(t);
  await t.shot("scrolled-back");

  // Output that arrives while reading back does not pull the view down;
  // Jump to latest shows it.
  t.stub.terminalWrite(SESSION, "\r\nlive output while scrolled back\r\n$ ");
  await sleep(300);
  assert.ok((await screen(t)).rows.some((row) => row.includes("history line 001")), "live output pulled the view off the history");
  await t.page.tap(JUMP);
  await t.page.waitForFunction((selector) => !document.querySelector(selector), {}, JUMP);
  await poll(async () => (await screen(t)).rows, (rows) => rows.some((row) => row.includes("live output while scrolled back")),
    3_000, "Jump to latest did not show the live screen");

  // One attach throughout; besides input only sizes, flow-control credit and
  // keepalive; nothing remembered.
  assert.equal(viewers().length, 1, "the phone reattached");
  const controls = viewer.frames.filter((frame) => typeof frame !== "string");
  assert.ok(controls.every((frame) => ["resize", "ack", "ping"].includes(frame.type)), `the attach got ${JSON.stringify(controls)}`);
  assert.deepEqual(t.stub.statePuts(), [], "the phone terminal wrote roaming preferences");

  // Leaving closes the attach, which is when the daemon gives the agent its size back.
  await t.page.goto(t.url("/focus"), { waitUntil: "domcontentloaded" });
  await poll(() => viewer.open, (open) => !open, 5_000, "leaving left the attach open");

  // The phone and the desktop terminal draw the same screen in the same colours.
  // Compared in portrait: a landscape phone is too wide for the Agents page's
  // compact layout, so it shows the desktop terminal there.
  if (vp.isLandscape) return;
  await t.page.goto(t.url("/agents?agent=worker-a"), { waitUntil: "domcontentloaded" });
  await t.page.waitForFunction(() => [...document.querySelectorAll("[data-phone-terminal] .xterm-rows span")].some((s) => s.textContent?.includes("ansi-red")), { timeout: 10_000 });
  const phone = await palette(t, HOST);
  await t.shot("theme-phone");
  await t.page.setViewport({ ...vp, width: 1024, height: 768, isLandscape: true });
  await t.page.waitForFunction(() => [...document.querySelectorAll("[data-interactive-terminal] .xterm-rows span")].some((s) => s.textContent?.includes("ansi-red")), { timeout: 10_000 });
  const desktop = await palette(t, "[data-interactive-terminal]");
  await t.shot("theme-desktop");
  assert.ok(phone.red && phone.indexed && phone.truecolor && phone.blueBackground, `the phone drew no colour sample: ${JSON.stringify(phone)}`);
  assert.notEqual(phone.red, phone.foreground, "the phone drew red in the default colour");
  // xterm.css paints the viewport #000; the band below the last row shows it.
  assert.equal(phone.background[1], "rgb(13, 17, 23)", "the terminal viewport is not the theme background");
  assert.deepEqual(phone, desktop, "the phone and the desktop terminal use different colours");
  await t.page.setViewport(vp);
}
