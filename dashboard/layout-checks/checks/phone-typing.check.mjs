// A phone types into an agent (mobile interactive terminal spec): watch only
// until Type, then the input-only socket carries a line (Enter as its own
// frame), the key strip's keys and a multi-line paste. A key tap keeps focus in
// the input bar, a screen tap moves it there, nothing sends a size, and nothing
// ever attaches. Watch only closes the socket again.
import assert from "node:assert/strict";
import { expectLayout, rect, waitForText } from "../probes.mjs";
import { SESSION } from "../fixtures/base.mjs";
import { PHONES } from "../profiles.mjs";

export const name = "phone-typing";
export const profiles = PHONES;

const SCREEN_TEXT = "claimed: fixture-task-1";
const INPUT = 'textarea[aria-label="worker-a terminal input"]';
const TYPE = "xpath/.//button[normalize-space()='Type']";
const WATCH = 'button[aria-label="Watch only"]';
const STRIP = '[aria-label="worker-a terminal keys"]';
const SEND = '[aria-label="Send to worker-a"]';
const INPUT_PATH = `/ws/terminal/${SESSION}/input`;

async function until(predicate, timeout, message) {
  for (const end = Date.now() + timeout; !predicate();) {
    if (Date.now() > end) throw new Error(message);
    await new Promise((done) => setTimeout(done, 25));
  }
}
const focused = (t) => t.page.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? null);
const modeHint = (t, selector) => t.page.$eval(selector, (el) => el.getAttribute("aria-description"));

export async function run(t) {
  await t.page.goto(t.url(`/focus/sessions/${SESSION}`), { waitUntil: "domcontentloaded" });
  await waitForText(t.page, SCREEN_TEXT);
  const vp = t.page.viewport();

  // Every visit starts watch only: a tap on the screen types nothing and opens nothing.
  assert.equal(await t.page.$(WATCH), null);
  assert.equal(await modeHint(t, TYPE), "Watch only. Enable typing.");
  await t.page.tap("[data-allow-overflow-x] pre");
  await new Promise((done) => setTimeout(done, 300));
  assert.equal(await t.page.$(INPUT), null, "watch only shows an input bar");
  assert.deepEqual(t.sockets.terminal(), [], "watch only opened a terminal socket");

  // Type opens exactly one input-only socket — no dimensions, never an attach.
  await t.page.tap(TYPE);
  await t.page.waitForSelector(`${STRIP} button:not([disabled])`);
  await until(() => t.stub.terminalInputs.length === 1, 5_000, "Type opened no input socket");
  assert.equal(t.stub.terminalInputs[0].url, INPUT_PATH, "the input socket carries a query (a size?)");
  assert.equal(await modeHint(t, WATCH), "Typing is active. Switch to watch only.");
  await expectLayout(t, { primary: [WATCH, `${STRIP} button`, SEND] });
  const strip = await rect(t.page, STRIP);
  assert.ok(strip.left >= -1 && strip.right <= vp.width + 1, `the key strip leaves the viewport (${strip.left}–${strip.right})`);
  const bar = await rect(t.page, INPUT);
  assert.ok(bar.bottom <= vp.height + 1 && bar.height >= 44, `the input bar is off screen or short (${bar.top}–${bar.bottom})`);
  await t.shot("typing");

  // A tap on the screen focuses the input bar, never a hidden terminal textarea.
  await t.page.tap("[data-allow-overflow-x] pre");
  assert.equal(await focused(t), "worker-a terminal input", "a screen tap did not focus the input bar");

  // A line, then Enter as its own frame.
  await t.page.keyboard.type("hello from the phone");
  await t.page.keyboard.press("Enter");
  await until(() => t.stub.typed(SESSION).length >= 2, 5_000, "the line and its Enter never reached the terminal socket");
  assert.deepEqual(t.stub.typed(SESSION), ["hello from the phone", "\r"]);
  assert.equal(await t.page.$eval(INPUT, (el) => el.value), "", "the sent line stayed in the input bar");

  // The key strip: each tap is its bytes, and focus (the on-screen keyboard) stays put.
  for (const key of ["Send 2", "Send Escape", "Send Ctrl-C", "Send Up arrow", "Send Enter"]) {
    await t.page.tap(`[aria-label="${key}"]`);
  }
  await until(() => t.stub.typed(SESSION).length >= 7, 5_000, "key strip taps never reached the terminal socket");
  assert.deepEqual(t.stub.typed(SESSION).slice(2), ["2", "\x1b", "\x03", "\x1b[A", "\r"]);
  assert.equal(await focused(t), "worker-a terminal input", "a key tap took focus from the input bar (the keyboard would close)");

  // Several lines (Shift+Enter) go as one bracketed paste, then Enter.
  await t.page.keyboard.type("line one");
  await t.page.keyboard.down("Shift");
  await t.page.keyboard.press("Enter");
  await t.page.keyboard.up("Shift");
  await t.page.keyboard.type("line two");
  await t.page.tap(SEND);
  await until(() => t.stub.typed(SESSION).length >= 9, 5_000, "the multi-line entry never reached the terminal socket");
  assert.deepEqual(t.stub.typed(SESSION).slice(7), ["\x1b[200~line one\nline two\x1b[201~", "\r"]);
  await t.shot("typed");

  // Only input (and keepalive) on the socket; nothing attached, nothing remembered.
  const controls = t.stub.terminalInputs.flatMap((r) => r.frames.filter((f) => typeof f !== "string"));
  assert.ok(controls.every((f) => f.control.type === "ping"), `the input socket got ${JSON.stringify(controls)}`);
  assert.deepEqual(t.stub.terminalUpgrades, [], "a phone attached a terminal");
  assert.ok(t.sockets.terminal().every((u) => new URL(u).pathname === INPUT_PATH), `unexpected terminal sockets: ${t.sockets.terminal()}`);

  // Watch only closes the socket and removes the input bar.
  await t.page.tap(WATCH);
  await t.page.waitForFunction((selector) => !document.querySelector(selector), {}, INPUT);
  await until(() => !t.stub.terminalInputs[0].open, 5_000, "Watch only left the input socket open");
  assert.equal(t.stub.terminalInputs.length, 1, "the socket reconnected after Watch only");
  await expectLayout(t, { primary: [TYPE] });
  assert.deepEqual(t.stub.statePuts(), [], "typing wrote roaming preferences");
}
