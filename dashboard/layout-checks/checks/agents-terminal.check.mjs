// /agents attaches for a fixed worker and for a pool instance: on a phone the
// phone terminal asks for history and for the agent's size back, and a viewer
// the daemon refuses (the stub refuses unless a check allows the session)
// watches the pane stream in the same terminal; the desktop attaches as before.
import assert from "node:assert/strict";
import { expectLayout, waitForText } from "../probes.mjs";
import { NOW, POOL_SESSION, SESSION } from "../fixtures/base.mjs";

export const name = "agents-terminal";

const VIEWS = [
  { agent: "worker-a", session: SESSION, text: "claimed: fixture-task-1", href: `/focus/sessions/${SESSION}` },
  { agent: "pool:deep-high-claude", session: POOL_SESSION, text: "pool worker idle", href: `/focus/sessions/${POOL_SESSION}?started=${NOW - 600}` },
];
const STATUS = '[aria-label$="terminal status"]';
const attachesFor = (t, session) => t.stub.terminalUpgrades
  .map((url) => new URL(url, "http://stub.invalid"))
  .filter((url) => url.pathname === `/ws/terminal/${session}`);

export async function run(t) {
  const compact = t.page.viewport().width < 768;
  for (const view of VIEWS) {
    await t.page.goto(t.url(`/agents?agent=${encodeURIComponent(view.agent)}`), { waitUntil: "domcontentloaded" });
    if (!compact) {
      const before = t.stub.terminalUpgrades.length;
      await t.page.waitForFunction(() => document.querySelector("[data-interactive-terminal]"));
      await new Promise((done) => setTimeout(done, 500));
      assert.ok(t.stub.terminalUpgrades.length > before, `desktop no longer attaches ${view.agent}`);
      continue;
    }
    await t.page.waitForSelector("[data-phone-terminal]");
    // Refused, the phone falls back to the pane stream, drawn by the same terminal.
    await waitForText(t.page, view.text);
    await t.page.waitForFunction((selector) => document.querySelector(selector)?.textContent?.startsWith("Watch only"), { timeout: 10_000 }, STATUS);
    const attaches = attachesFor(t, view.session);
    assert.ok(attaches.length > 0, `the phone never tried to attach ${view.agent}`);
    for (const url of attaches) {
      assert.equal(url.searchParams.get("history"), "2000", `the phone attach asks for no history: ${url}`);
      assert.equal(url.searchParams.get("restore_size"), "1", `the phone attach keeps the phone's size: ${url}`);
    }
    // Watching is read-only: no input bar and no keys to a refused viewer.
    assert.equal(await t.page.$('[aria-label$=" terminal input"], [aria-label$=" terminal keys"]'), null, "a refused phone offers typing");
    await t.page.click('[aria-label^="Details for "]');
    await expectLayout(t, { primary: ['[aria-label="Open navigation"]', '[aria-label="Full screen"]', '[aria-label="Larger text"]'] });
    await t.page.keyboard.press("Escape");
    assert.equal(await t.page.$eval('a[aria-label="Full screen"]', (a) => a.getAttribute("href")), view.href);
    await t.shot(view.agent.startsWith("pool:") ? "pool" : "agent");
  }
  if (!compact) return;
  assert.equal(await t.page.$("[data-interactive-terminal]"), null, "a phone mounted the desktop terminal");
  const sessions = new Set(t.stub.terminalUpgrades.map((url) => new URL(url, "http://stub.invalid").pathname));
  assert.deepEqual([...sessions].sort(), VIEWS.map((view) => `/ws/terminal/${view.session}`).sort(), "a phone attached another session");
}
