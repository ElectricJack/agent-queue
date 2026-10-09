// A viewer the daemon refuses an attach watches the pane stream in the phone
// terminal: geometry at every profile, refused attaches that send no frames,
// font changes send nothing, full screen is a trapped sheet that survives
// rotation and Back, the stream's stale state and Retry, restart, ended and
// missing sessions. phone-terminal covers the attached terminal.
import assert from "node:assert/strict";
import { expectLayout, rect, waitForText } from "../probes.mjs";
import { SESSION } from "../fixtures/base.mjs";
import { ENDED_SESSION, SESSIONS, STARTED, showSession } from "../fixtures/session.mjs";

export const name = "focus-session";

const HEADER = ['header [aria-label="Back"]', 'header [aria-label="Focus home"]', 'header [aria-label="Open in full dashboard"]'];
const TOOLS = ['[aria-label^="Details for "]', '[aria-label="Full screen"]'];
const FONTS = ['[aria-label="Smaller text"]', '[aria-label="Larger text"]'];
const RETRY = "xpath/.//button[normalize-space()='Retry']";
const SCREEN_TEXT = "claimed: fixture-task-1";
const terminalFrames = (t) => t.sockets.sent.filter((s) => s.url?.includes("/ws/terminal"));
const statusMatches = (t, pattern, timeout = 15_000) => t.page.waitForFunction(
  (source) => new RegExp(source).test(document.querySelector('[aria-label$="terminal status"]')?.textContent ?? ""),
  { timeout }, pattern.source);
const WATCHING = /^Watch only — typing is not available/;
// The terminal refits on the frame after a resize; wait for it before probing.
const refitted = (t) => t.page.waitForFunction(() => {
  const frame = document.querySelector("[data-phone-terminal]")?.parentElement;
  return !!frame && frame.scrollWidth <= frame.clientWidth;
});
const paneRequests = (t) => t.stub.requests.filter((r) => r.path === `/api/sessions/${SESSION}/pane`).length;

async function until(predicate, timeout, message) {
  for (const end = Date.now() + timeout; !predicate();) {
    if (Date.now() > end) throw new Error(message);
    await new Promise((done) => setTimeout(done, 25));
  }
}

export async function run(t) {
  await t.page.goto(t.url(`/focus/sessions/${SESSION}`), { waitUntil: "domcontentloaded" });
  await waitForText(t.page, SCREEN_TEXT);
  await statusMatches(t, WATCHING);
  await expectLayout(t, { primary: [...HEADER, ...TOOLS] });
  assert.equal(await t.page.$('[aria-label$=" terminal input"], [aria-label$=" terminal keys"]'), null, "a refused viewer is offered typing");
  // The screen's frame (terminal background) runs edge to edge.
  const frame = await t.page.$eval("[data-phone-terminal]", (el) => {
    const r = el.parentElement.getBoundingClientRect();
    return { left: r.left, right: r.right };
  });
  const vp = t.page.viewport();
  assert.ok(frame.left <= 1 && frame.right >= vp.width - 1, `the pane is not edge to edge (${frame.left}–${frame.right})`);

  // Font: rendering only — no request, no socket, no new stream.
  await t.page.click('[aria-label^="Details for "]');
  await expectLayout(t, { primary: FONTS });
  const before = t.stub.requests.length;
  const shownFont = () => t.page.$eval('[aria-label="Text size"]', (el) => el.textContent);
  const drawnFont = () => t.page.$eval("[data-phone-terminal] .xterm-rows", (el) => getComputedStyle(el).fontSize);
  assert.equal(await shownFont(), "12px");
  assert.equal(await drawnFont(), "12px");
  for (let i = 0; i < 2; i++) {
    if (await t.page.$eval('[aria-label="Larger text"]', (el) => el.disabled)) break;
    await t.page.click('[aria-label="Larger text"]');
  }
  // Settles at 16 px, unless a narrow screen capped it to keep MIN_COLUMNS
  // (Larger then disabled); the drawn size matches the shown one either way.
  await t.page.waitForFunction(() => {
    const shown = document.querySelector('[aria-label="Text size"]')?.textContent;
    const rows = document.querySelector("[data-phone-terminal] .xterm-rows");
    const larger = document.querySelector('[aria-label="Larger text"]');
    return !!rows && shown === getComputedStyle(rows).fontSize && (shown === "16px" || larger.disabled);
  }, { timeout: 5_000 }).catch(async () => assert.fail(`text stopped at ${await shownFont()} with room to grow`));
  if (vp.width >= 768) assert.equal(await shownFont(), "16px");
  assert.deepEqual(t.stub.requests.slice(before), [], "a font change sent a request");
  await expectLayout(t, { primary: TOOLS });
  await t.shot("font-details");
  await t.page.keyboard.press("Escape");
  await t.shot("watch");

  // Full screen: covers the viewport, traps focus, survives rotation, Back closes it.
  await t.page.click('[aria-label="Full screen"]');
  await t.page.waitForSelector('[role=dialog][aria-label$="full screen"]');
  const sheet = await rect(t.page, "[role=dialog]");
  assert.ok(sheet.width >= vp.width - 1 && sheet.height >= vp.height - 1, "full screen does not cover the viewport");
  for (let i = 0; i < 6; i++) await t.page.keyboard.press("Tab");
  assert.ok(await t.page.evaluate(() => document.querySelector("[role=dialog]").contains(document.activeElement)), "focus escaped the sheet");
  await expectLayout(t, { primary: ['[aria-label="Exit full screen"]'] });
  await t.shot("full-screen");
  if (t.isPhone) {
    const rotated = { ...vp, width: vp.height, height: vp.width, isLandscape: vp.width < vp.height };
    await t.page.setViewport(rotated);
    await refitted(t);
    const turned = await rect(t.page, "[role=dialog]");
    assert.ok(turned.width >= rotated.width - 1 && turned.height >= rotated.height - 1, "full screen lost on rotation");
    await expectLayout(t, { primary: ['[aria-label="Exit full screen"]'] });
    await t.shot("full-screen-rotated");
    await t.page.setViewport(vp);
    await t.page.waitForSelector('[role=dialog][aria-label$="full screen"]');
    await refitted(t);
  }
  await t.page.goBack();
  await t.page.waitForFunction(() => !document.querySelector("[role=dialog]"));
  assert.ok(t.page.url().endsWith(`/focus/sessions/${SESSION}`), "Back left the session instead of closing full screen");
  await t.page.click('[aria-label="Full screen"]');
  await t.page.waitForSelector("[role=dialog]");
  await t.page.keyboard.press("Escape");
  await t.page.waitForFunction(() => !document.querySelector("[role=dialog]"));
  assert.equal(await t.page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Full screen", "focus not returned");
  assert.ok(t.page.url().endsWith(`/focus/sessions/${SESSION}`), "Escape left the session");
  assert.equal(paneRequests(t), 1, "full screen opened another pane stream");

  // Network loss: the stream reconnects on its own; the screen stays, marked stale.
  t.stub.dropPane(SESSION);
  await statusMatches(t, /^Watch only — reconnecting/);
  await waitForText(t.page, SCREEN_TEXT);
  await statusMatches(t, WATCHING);
  // Refused reconnects back off, the last screen still on show; Retry
  // reconnects at once instead of waiting out the backoff.
  t.stub.failPane(SESSION, 503);
  const refusedFrom = paneRequests(t);
  t.stub.dropPane(SESSION);
  await statusMatches(t, /^Watch only — reconnecting · screen from /);
  await waitForText(t.page, SCREEN_TEXT);
  await t.page.click('[aria-label^="Details for "]');
  await expectLayout(t, { primary: [RETRY] });
  assert.ok(await t.page.$eval("[data-terminal-details]", (el) => el.innerText.includes("screen from")), "the details omit the age of the stale screen");
  await t.shot("reconnecting");
  // After three refusals the next automatic attempt is seconds away (≥3.2 s).
  await until(() => paneRequests(t) >= refusedFrom + 3, 20_000, "the stream stopped retrying a refused reconnect");
  t.stub.failPane(SESSION, null);
  await t.page.click(RETRY);
  await statusMatches(t, WATCHING, 1_500);
  assert.equal(await t.page.$(RETRY), null, "Retry stayed after the stream came back");
  await t.page.keyboard.press("Escape");

  // A restart is never followed silently.
  t.stub.override("POST /api/system/session-show", (body) => body.session_id === SESSION
    ? { session: { ...SESSIONS[SESSION], started_at: STARTED + 50 } }
    : showSession(body));
  await t.page.goto(t.url(`/focus/sessions/${SESSION}?started=${STARTED}`), { waitUntil: "domcontentloaded" });
  await waitForText(t.page, "restarted");
  await t.shot("restarted");
  assert.equal(await t.page.$("[data-allow-overflow-x]"), null, "the restarted process is shown without a tap");
  await expectLayout(t, { primary: [...HEADER, "xpath/.//button[normalize-space()='Watch the new process']"] });
  await t.page.click("xpath/.//button[normalize-space()='Watch the new process']");
  await waitForText(t.page, SCREEN_TEXT);
  assert.ok(t.page.url().endsWith(`?started=${STARTED + 50}`), "the new process is not pinned in the URL");

  // An ended session links the agent's current one.
  await t.page.goto(t.url(`/focus/sessions/${ENDED_SESSION}`), { waitUntil: "domcontentloaded" });
  await waitForText(t.page, "Session ended");
  await waitForText(t.page, "Task closed: pass");
  await t.page.waitForSelector(`a[href="/focus/sessions/${SESSION}"]`);
  await expectLayout(t, { primary: [...HEADER, `a[href="/focus/sessions/${SESSION}"]`] });
  await t.shot("ended");

  // A missing session is a scoped error with a way back (once: the query retries first).
  if (t.profile === "phone-320") {
    await t.page.goto(t.url("/focus/sessions/fixture-missing"), { waitUntil: "domcontentloaded" });
    await t.page.waitForSelector("[role=alert]", { timeout: 20_000 });
    await expectLayout(t, { primary: ["xpath/.//button[normalize-space()='Retry']", "xpath/.//button[normalize-space()='Go back']"] });
  }

  // The phone tried to attach (asking for history and its size back), was
  // refused, and sent nothing.
  assert.ok(t.stub.terminalUpgrades.length > 0, "the phone never tried to attach");
  for (const raw of t.stub.terminalUpgrades) {
    const url = new URL(raw, "http://stub.invalid");
    assert.equal(url.searchParams.get("history"), "2000", `an attach asks for no history: ${raw}`);
    assert.equal(url.searchParams.get("restore_size"), "1", `an attach keeps the phone's size: ${raw}`);
  }
  assert.deepEqual(terminalFrames(t), [], "frames were sent on a refused terminal socket");
  assert.deepEqual(t.stub.statePuts(), [], "a focus route wrote roaming preferences");
}
