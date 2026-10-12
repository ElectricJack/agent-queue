// The focus task view: long Unicode title, primary controls, a dotted child id
// through in-app navigation, and terminal links that stay in focus mode.
import assert from "node:assert/strict";
import { expectLayout, waitForText } from "../probes.mjs";
import { CHILD_TASK_ID, TASK_IDS } from "../fixtures/base.mjs";

export const name = "focus-task";

const PRIMARY = ['header [aria-label="Back"]', 'header [aria-label="Focus home"]', 'header [aria-label="Open in full dashboard"]'];

export async function run(t) {
  await t.page.goto(t.url(`/focus/tasks/${TASK_IDS[0]}`), { waitUntil: "networkidle0" });
  await waitForText(t.page, "Ünïcödé");
  await expectLayout(t, { primary: PRIMARY });
  await t.shot("task");

  // The terminal link leads to the focus session, never /agents.
  const watch = await t.page.$eval("a[href^='/focus/sessions/']", (a) => a.getAttribute("href"));
  assert.match(watch, /^\/focus\/sessions\//);
  assert.equal(await t.page.$("a[href^='/agents']"), null);

  // A dotted child id, opened in-app, renders its own detail.
  await t.page.click(`button[title="Dotted child task"]`);
  await t.page.waitForFunction((id) => location.pathname === `/focus/tasks/${id}`, {}, CHILD_TASK_ID);
  await waitForText(t.page, "Dotted child task");
  await expectLayout(t, { primary: PRIMARY });
  await t.shot("child");

  // Back returns to the parent.
  await t.page.click('header [aria-label="Back"]');
  await t.page.waitForFunction((id) => location.pathname === `/focus/tasks/${id}`, {}, TASK_IDS[0]);

  // A missing task is a scoped error with a way back.
  await t.page.goto(t.url("/focus/tasks/no-such-task"), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[role=alert]");
  await expectLayout(t, { primary: [...PRIMARY.slice(0, 2), "[role=alert] button"] });

  assert.deepEqual(t.stub.statePuts(), []);
  assert.deepEqual(t.sockets.terminal(), []);
}
