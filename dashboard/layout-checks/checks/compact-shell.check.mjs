// The regular shell: one column below 768 px (phones, and 200% zoom), a drawer
// that traps focus and closes on Escape/Back, full-screen activity and pane
// sheets, no roaming writes; the desktop keeps its rail.
import assert from "node:assert/strict";
import { expectLayout, rect, targets } from "../probes.mjs";
import { PROJECT, stateDocuments } from "../fixtures/base.mjs";

export const name = "compact-shell";

export async function run(t) {
  t.stub.override("POST /api/dashboard/state-list", () => stateDocuments({
    right_surface: { width: 760, kind: "drawer", activity_tab: "events", pane: null },
  }));
  await t.page.goto(t.url("/reviews"), { waitUntil: "networkidle0" });
  const compact = t.page.viewport().width < 768;
  if (!compact) {
    assert.ok(await t.page.$("aside nav"), "desktop lost its rail");
    assert.equal(await t.page.$('[aria-label="Open navigation"]'), null);
    await expectLayout(t);
    return;
  }
  assert.equal(await t.page.$("[role=dialog]"), null, "a stored drawer popped over a compact first view");
  await expectLayout(t, { primary: ['[aria-label="Open navigation"]', '[aria-label="Back"]', '[aria-label="Activity"]'] });

  // Drawer: covers the page's left, traps focus, Escape closes and returns focus.
  await t.page.click('[aria-label="Open navigation"]');
  await t.page.waitForSelector('[role=dialog][aria-label="Navigation"]');
  const drawer = await rect(t.page, '[role=dialog][aria-label="Navigation"]');
  assert.ok(drawer.left <= 0 && drawer.width <= t.page.viewport().width);
  for (let i = 0; i < 40; i++) await t.page.keyboard.press("Tab");
  assert.ok(await t.page.evaluate(() => document.querySelector('[aria-label="Navigation"]').contains(document.activeElement)), "focus escaped the drawer");
  await expectLayout(t, { primary: ['[aria-label="Close navigation"]', "[role=dialog] nav a"] });
  await t.shot("drawer");
  await t.page.keyboard.press("Escape");
  await t.page.waitForFunction(() => !document.querySelector('[aria-label="Navigation"]'));
  assert.equal(await t.page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Open navigation");

  // Back closes it; a link inside it replaces the drawer entry.
  await t.page.click('[aria-label="Open navigation"]');
  await t.page.waitForSelector('[aria-label="Navigation"]');
  await t.page.goBack();
  await t.page.waitForFunction(() => !document.querySelector('[aria-label="Navigation"]'));
  assert.ok(t.page.url().endsWith("/reviews"));
  await t.page.click('[aria-label="Open navigation"]');
  await t.page.waitForSelector('[aria-label="Navigation"] a[href="/metrics"]');
  await t.page.click('[aria-label="Navigation"] a[href="/metrics"]');
  await t.page.waitForFunction(() => location.pathname === "/metrics");
  await t.page.goBack();
  await t.page.waitForFunction(() => location.pathname === "/reviews");
  assert.equal(await t.page.$('[aria-label="Navigation"]'), null, "Back reopened the drawer");

  // The activity drawer is a full-screen sheet; Back closes it.
  await t.page.click('[aria-label="Activity"]');
  await t.page.waitForSelector('[role=dialog][aria-label="Activity"]');
  const sheet = await rect(t.page, '[role=dialog][aria-label="Activity"]');
  const vp = t.page.viewport();
  assert.ok(sheet.width >= vp.width - 1 && sheet.height >= vp.height - 1);
  await expectLayout(t, { primary: ['[aria-label="Close activity"]'] });
  await t.shot("activity");
  await t.page.goBack();
  await t.page.waitForFunction(() => !document.querySelector('[aria-label="Activity"][role=dialog]'));

  // A pane opened below 768 px is a full-screen sheet as well; Back closes it.
  // (The Tasks tab's own compact layout is Task 8's check.)
  await t.page.goto(t.url(`/projects/${PROJECT}/tasks`), { waitUntil: "networkidle0" });
  await t.page.click('[data-task-row="fixture-task-2"] .line-clamp-2'); // the title: a table row's centre can be its status select
  await t.page.waitForSelector('[role=dialog][aria-label="Pane"]');
  const paneSheet = await rect(t.page, '[role=dialog][aria-label="Pane"]');
  assert.ok(paneSheet.width >= vp.width - 1 && paneSheet.height >= vp.height - 1, "the pane is not full-screen");
  await t.shot("pane");
  if (t.isPhone) {
    for (const target of await targets(t.page, '[aria-label="Close pane"]')) {
      assert.ok(target.width >= 44 && target.height >= 44, `Close pane is ${target.width}×${target.height}`);
    }
  }
  await t.page.goBack();
  await t.page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Pane"]'));
  assert.ok(t.page.url().endsWith(`/projects/${PROJECT}/tasks`), "Back left the page instead of closing the pane");

  assert.deepEqual(t.stub.statePuts(), [], "the compact shell wrote roaming preferences");
}
