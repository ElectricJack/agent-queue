// `/focus/sessions/:id` is the host shell page's terminal at every profile, so
// a phone and a desktop see the same window. This check covers the page around
// it: geometry, the pinned process, a restart never followed silently, ended
// and missing sessions, the transport a compact viewport asks tmux for, and a
// refused attach saying so and sending nothing. `phone-terminal` covers the
// attached terminal itself.
import assert from "node:assert/strict";
import { expectLayout, waitForText } from "../probes.mjs";
import { SESSION } from "../fixtures/base.mjs";
import { HOST_SHELL } from "../fixtures/host-shell.mjs";
import { ENDED_SESSION, SESSIONS, STARTED, showSession } from "../fixtures/session.mjs";

export const name = "focus-session";

const HEADER = ['header [aria-label="Back"]', 'header [aria-label="Focus home"]', 'header [aria-label="Open in full dashboard"]'];
const TOOLS = ['[aria-label^="Details for "]', '[aria-label="Focus worker-a terminal"]'];
const SCREEN = "claimed: fixture-task-1\r\n$ ";
const SCREEN_TEXT = "claimed: fixture-task-1";
const terminalFrames = (t, sessionId) => t.sockets.sent.filter((s) => s.url?.includes(`/ws/terminal/${sessionId}`));
const statusMatches = (t, pattern, timeout = 15_000) => t.page.waitForFunction(
  (source) => new RegExp(source).test(document.querySelector('[aria-label$="terminal connection"]')?.textContent ?? ""),
  { timeout }, pattern.source);
const attachQuery = (t, sessionId) => t.stub.terminalUpgrades
  .map((raw) => new URL(raw, "http://stub.invalid"))
  .filter((url) => url.pathname === `/ws/terminal/${sessionId}`);

export async function run(t) {
  const wide = t.page.viewport().width >= 768;
  t.stub.allowTerminal(SESSION, SCREEN);
  await t.page.goto(t.url(`/focus/sessions/${SESSION}`), { waitUntil: "domcontentloaded" });

  // Live: the host shell page's terminal, attached and drawing.
  await waitForText(t.page, SCREEN_TEXT);
  await statusMatches(t, /connected/);
  assert.equal(await t.page.$('[aria-label="Watch only"]'), null, "a watch/type mode is back");
  assert.deepEqual(t.stub.statePuts(), [], "a focus route wrote roaming preferences");
  // The terminal's frame runs edge to edge.
  const frame = await t.page.$eval("[data-interactive-terminal]", (el) => {
    const r = el.parentElement.getBoundingClientRect();
    return { left: r.left, right: r.right };
  });
  const vp = t.page.viewport();
  assert.ok(frame.left <= 1 && frame.right >= vp.width - 1, `the terminal is not edge to edge (${frame.left}–${frame.right})`);
  await expectLayout(t, { primary: [...HEADER, ...TOOLS] });
  await t.shot("live");

  // A compact viewport asks tmux for scrollback and for the agent's window size
  // back; a wide one asks for neither. That transport is the only difference.
  const attaches = attachQuery(t, SESSION);
  assert.ok(attaches.length > 0, "the page never attached");
  for (const url of attaches) {
    assert.equal(url.searchParams.get("history"), wide ? null : "2000", `wrong history in ${url}`);
    assert.equal(url.searchParams.get("restore_size"), wide ? null : "1", `wrong restore_size in ${url}`);
  }

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
  await t.page.waitForSelector("[data-interactive-terminal]");
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

  // A refused viewer is told so on the same terminal and sends nothing: there
  // is no separate watch-only console to fall back to.
  await t.page.goto(t.url("/host-shell"), { waitUntil: "domcontentloaded" });
  await t.page.waitForFunction((session) => {
    const status = document.querySelector(`[aria-label="${session} terminal connection"]`);
    return !!status && /refused/i.test(status.getAttribute("title") ?? "") && status.textContent?.includes("error");
  }, { timeout: 15_000 }, HOST_SHELL);
  assert.ok(attachQuery(t, HOST_SHELL).length > 0, "the host shell page never tried to attach");
  assert.deepEqual(terminalFrames(t, HOST_SHELL), [], "frames were sent on a refused terminal socket");
  await t.shot("refused");
}
