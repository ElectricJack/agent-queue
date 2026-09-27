// The Tasks tab at every profile: cards below 768 px (phones, 200% zoom), the
// table above; a card opens a full-screen pane that Back closes without losing
// the list's scroll; no roaming writes while compact.
import assert from "node:assert/strict";
import { expectLayout } from "../probes.mjs";
import { PROJECT, TASK_IDS } from "../fixtures/base.mjs";

export const name = "tasks-tab";

export async function run(t) {
  await t.page.goto(t.url(`/projects/${PROJECT}/tasks`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[data-task-row]");
  const compact = t.page.viewport().width < 768;
  if (!compact) {
    assert.ok(await t.page.$("tr[data-task-row]"), "the desktop table is gone");
    await expectLayout(t);
    return;
  }
  assert.equal(await t.page.$("table"), null, "the wide table rendered below 768 px");
  await expectLayout(t, { primary: ["[data-task-row]", '[aria-label="Open navigation"]'] });
  await t.shot("cards");

  const list = '[role=region][aria-label="Task list"]';
  await t.page.$eval(list, (el) => { el.scrollTop = 900; el.dispatchEvent(new Event("scroll")); });
  await t.page.waitForFunction((sel) => document.querySelector(sel).scrollTop > 0, {}, list);
  const scrolled = await t.page.$eval(list, (el) => el.scrollTop);
  const card = await t.page.$$eval("[data-task-row]", (els) => {
    const top = document.querySelector('[aria-label="Task list"]').getBoundingClientRect().top;
    return els.find((el) => el.getBoundingClientRect().top > top + 40)?.getAttribute("data-task-row");
  });
  assert.ok(card && TASK_IDS.includes(card), `no card below the list's top edge (${card})`);
  await t.page.click(`[data-task-row="${card}"]`);
  await t.page.waitForSelector('[role=dialog][aria-label="Pane"]');
  await expectLayout(t, { primary: ['[aria-label="Close pane"]'] });
  await t.shot("pane");
  await t.page.goBack();
  await t.page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Pane"]'));
  const after = await t.page.$eval(list, (el) => el.scrollTop);
  assert.ok(Math.abs(after - scrolled) <= 2, `list scroll moved from ${scrolled} to ${after}`);

  assert.deepEqual(t.stub.statePuts(), [], "the compact Tasks tab wrote roaming preferences");
  assert.deepEqual(t.sockets.terminal(), []);
}
