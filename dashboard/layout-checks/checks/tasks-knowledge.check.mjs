// Real browser checks for the combined workspace, including its 1024px sheet breakpoint.
import assert from "node:assert/strict";
import { expectLayout, rect } from "../probes.mjs";
import { PROJECT } from "../fixtures/base.mjs";

export const name = "tasks-knowledge";

export async function run(t) {
  const items = [
    { kind: "task", record_id: "task-record", task_id: "fixture-task-2", title: "Verify recovery", status: "READY" },
    ...Array.from({ length: 25 }, (_, index) => ({ kind: "knowledge", record_id: `finding-${index}`,
      revision_id: "revision-1", title: `Recovery procedure ${index}`, category: "incident", lifecycle: "active", verification: "unverified" })),
  ];
  t.stub.override("POST /api/record/capabilities", () => ({ success: true,
    capabilities: { enabled: true, ui_enabled: true, enabled_projects: [PROJECT], writes_enabled: false } }));
  t.stub.override("POST /api/record/search", () => ({ success: true, items, next_cursor: null }));
  t.stub.override("POST /api/record/link-list", () => ({ success: true, links: [] }));
  t.stub.override("POST /api/collaboration/list", () => ({ success: true, collaborations: [] }));
  t.stub.override("POST /api/knowledge/show", (body) => ({ success: true,
    record_id: body.identity.replace("record:", ""), knowledge_alias: "kn-recovery", revision_id: "revision-1", sequence: 1,
    current_revision_id: "revision-1", current_sequence: 1, scope_key: `project:${PROJECT}`,
    created_at: "2026-10-05T00:00:00Z", actor_id: "operator", change_kind: "create", allowed_actions: [],
    snapshot: { title: "Recovery procedure", body: "## Recover safely\n\nCheck the current claim, verify readiness, and resume the worker loop.",
      summary: "Steps retained after the recovery exercise.", category: "incident", lifecycle: "active", verification: "unverified",
      tags: ["operations"], sources: [], outgoing_links: [] } }));

  await t.page.goto(t.url(`/projects/${PROJECT}/records?q=Recovery`), { waitUntil: "networkidle0" });
  assert.equal(new URL(t.page.url()).pathname, `/projects/${PROJECT}/tasks-knowledge`);
  assert.equal(new URL(t.page.url()).searchParams.get("q"), "Recovery");
  const tabs = await t.page.$$eval('[aria-label="Command Center views"] a', (els) => els.map((el) => el.textContent));
  assert.ok(tabs.includes("Tasks & Knowledge"));
  assert.ok(!tabs.includes("Tasks") && !tabs.includes("Knowledge") && !tabs.includes("All records"));
  await t.page.waitForSelector("[data-record-row]");
  await t.shot("list");

  const scroller = "[data-record-scroll]";
  await t.page.$eval(scroller, (el) => { el.scrollTop = 240; });
  const selected = await t.page.$$eval("[data-record-row]", (els) => {
    const top = document.querySelector("[data-record-scroll]").getBoundingClientRect().top;
    return els.find((el) => el.getBoundingClientRect().top > top + 10)?.getAttribute("data-record-row");
  });
  assert.ok(selected);
  await t.page.click(`[data-record-row="${selected}"]`);
  await t.page.waitForSelector("[data-record-detail] article");
  const scrolled = await t.page.$eval(scroller, (el) => el.scrollTop);
  assert.equal(new URL(t.page.url()).searchParams.get("record"), selected);
  assert.equal(await t.page.$eval(`[data-record-row="${selected}"]`, (el) => el.getAttribute("aria-pressed")), "true");
  const sheet = t.page.viewport().width < 1024;
  assert.equal(await t.page.$eval("[data-record-detail]", (el) => el.getAttribute("data-layout")), sheet ? "sheet" : "pane");
  if (sheet) {
    const bounds = await rect(t.page, "[data-record-detail]");
    const viewport = t.page.viewport();
    assert.ok(bounds.left === 0 && bounds.top === 0 && bounds.width === viewport.width && bounds.height === viewport.height);
    assert.equal(await t.page.$eval("[data-record-list]", (el) => el.inert), true);
    for (let i = 0; i < 15; i++) await t.page.keyboard.press("Tab");
    assert.ok(await t.page.evaluate(() => document.querySelector("[data-record-detail]").contains(document.activeElement)), "focus escaped the sheet");
  } else {
    const pane = await rect(t.page, "[data-record-detail]");
    const list = await rect(t.page, "[data-record-list]");
    assert.ok(pane.left >= list.right - 1 && pane.width >= 320 && list.width >= 280);
    const separator = '[aria-label="Resize record detail"]';
    await t.page.focus(separator);
    await t.page.keyboard.press("ArrowLeft");
    assert.equal((await rect(t.page, "[data-record-detail]")).width, pane.width + 32);
    const grip = await rect(t.page, separator);
    await t.page.mouse.move(grip.left + grip.width / 2, grip.top + grip.height / 2);
    await t.page.mouse.down();
    await t.page.mouse.move(grip.left - 60, grip.top + grip.height / 2, { steps: 3 });
    await t.page.mouse.up();
    assert.ok((await rect(t.page, "[data-record-detail]")).width >= pane.width + 90, "drag did not resize the pane");
  }
  await expectLayout(t, { primary: [sheet ? '[aria-label="Back to list"]' : '[aria-label="Close detail"]', '[data-record-detail] a'] });
  await t.shot("knowledge-detail");
  if (sheet) await t.page.click('[aria-label="Back to list"]');
  else await t.page.keyboard.press("Escape");
  await t.page.waitForFunction(() => !document.querySelector("[data-record-detail]"));
  assert.ok(Math.abs(await t.page.$eval(scroller, (el) => el.scrollTop) - scrolled) <= 2, "closing moved the list scroll");
  assert.equal(new URL(t.page.url()).searchParams.get("q"), "Recovery");

  await t.page.$eval(scroller, (el) => { el.scrollTop = 0; });
  await t.page.click('[data-record-row="task-record"]');
  await t.page.waitForSelector('[data-record-detail] a[href="/tasks/fixture-task-2"]');
  assert.equal(new URL(t.page.url()).searchParams.get("task"), "fixture-task-2");
  const shared = t.page.url();
  await t.page.reload({ waitUntil: "networkidle0" });
  await t.page.waitForSelector('[data-record-detail] a[href="/tasks/fixture-task-2"]');
  assert.equal(t.page.url(), shared);
  await t.shot("task-detail");
  await t.page.goBack();
  await t.page.waitForFunction(() => !document.querySelector("[data-record-detail]"));
  if (!sheet) {
    await t.page.focus('[data-record-row="task-record"]');
    await t.page.keyboard.press("j");
    await t.page.waitForFunction(() => new URL(location.href).searchParams.get("record") === "finding-0");
    await t.page.keyboard.press("k");
    await t.page.waitForFunction(() => new URL(location.href).searchParams.get("task") === "fixture-task-2");
  }

  await t.page.goto(t.url(`/projects/${PROJECT}/tasks-knowledge?q=Recovery&record=finding-0&recordKind=knowledge&revision=revision-1`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[data-record-detail] article");
  await t.page.click('[data-record-detail] a');
  await t.page.waitForFunction(() => location.pathname.endsWith("/knowledge/finding-0"));
  assert.equal(new URL(t.page.url()).searchParams.get("revision"), "revision-1");
  await t.page.click('a::-p-text(Back to Tasks & Knowledge)');
  await t.page.waitForSelector("[data-record-detail] article");
  assert.equal(new URL(t.page.url()).searchParams.get("q"), "Recovery");
  assert.equal(new URL(t.page.url()).searchParams.get("record"), "finding-0");
  assert.deepEqual(t.sockets.terminal(), []);
}
