import assert from "node:assert/strict";
import { expectLayout, waitForText } from "../probes.mjs";
import { CHILD_TASK_ID } from "../fixtures/base.mjs";
import { REPORT } from "../fixtures/report.mjs";

export const name = "focus-report";

export async function run(t) {
  await t.page.goto(t.url(`/focus/reports/${REPORT}`), { waitUntil: "networkidle0" });
  await waitForText(t.page, "Morning report · 2026-09-25");
  await expectLayout(t, { primary: ['header [aria-label="Back"]', 'header [aria-label="Open in full dashboard"]'] });
  assert.ok(await t.page.$(`a[href="/focus/tasks/${CHILD_TASK_ID}"]`), "task evidence left the focus tree");
  await t.page.goto(t.url("/focus/reports/missing"), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[role=alert]");
  await expectLayout(t, { primary: ['header [aria-label="Back"]'] });
  assert.deepEqual(t.stub.statePuts(), []);
}
