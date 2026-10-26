// Full task detail: a long Unicode title and a dotted child opened in-app.
import { expectLayout, waitForText } from "../probes.mjs";
import { CHILD_TASK_ID, TASK_IDS } from "../fixtures/base.mjs";
export const name = "task-detail";
export async function run(t) {
  await t.page.goto(t.url(`/tasks/${TASK_IDS[0]}`), { waitUntil: "networkidle0" });
  await waitForText(t.page, "Ünïcödé");
  await expectLayout(t);
  await t.shot("task");
  await t.page.click(`a[href="/tasks/${CHILD_TASK_ID}"]`);
  await t.page.waitForFunction((id) => location.pathname === `/tasks/${id}`, {}, CHILD_TASK_ID);
  await waitForText(t.page, "Dotted child task");
  await expectLayout(t);
  await t.shot("child");
}
