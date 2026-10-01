// /agents on a phone watches and never attaches, for a fixed worker and for a
// pool instance; the desktop still attaches (the stub refuses the socket,
// which is enough to see the attempt).
import assert from "node:assert/strict";
import { expectLayout, waitForText } from "../probes.mjs";
import { NOW, POOL_SESSION, SESSION } from "../fixtures/base.mjs";

export const name = "agents-watch-only";

const VIEWS = [
  { agent: "worker-a", text: "claimed: fixture-task-1", href: `/focus/sessions/${SESSION}` },
  { agent: "pool:deep-high-claude", text: "pool worker idle", href: `/focus/sessions/${POOL_SESSION}?started=${NOW - 600}` },
];

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
    await waitForText(t.page, view.text);
    await t.page.click('[aria-label^="Details for "]');
    await expectLayout(t, { primary: ['[aria-label="Open navigation"]', '[aria-label="Full screen"]', '[aria-label="Larger text"]'] });
    await t.page.keyboard.press("Escape");
    assert.equal(await t.page.$eval('a[aria-label="Full screen"]', (a) => a.getAttribute("href")), view.href);
    await t.shot(view.agent.startsWith("pool:") ? "pool" : "agent");
  }
  if (!compact) return;
  await new Promise((done) => setTimeout(done, 500));
  assert.equal(await t.page.$("[data-interactive-terminal]"), null, "a phone mounted the interactive terminal");
  assert.deepEqual(t.sockets.terminal(), [], "a phone opened a terminal WebSocket");
  assert.deepEqual(t.stub.terminalUpgrades, []);
}
