import assert from "node:assert/strict";
import { expectLayout, waitForText } from "../probes.mjs";
import { CHILD_TASK_ID } from "../fixtures/base.mjs";
import { REPORT } from "../fixtures/report.mjs";

export const name = "report-detail";

export async function run(t) {
  await t.page.goto(t.url(`/reports/${REPORT}`), { waitUntil: "networkidle0" });
  await waitForText(t.page, "Morning report · 2026-09-25");
  await expectLayout(t, { primary: [] });
  assert.ok(await t.page.$(`a[href="/tasks/${CHILD_TASK_ID}"]`), "task evidence uses a canonical link");
  await t.page.goto(t.url("/reports/missing"), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[role=alert]");
  await expectLayout(t, { primary: [] });
}
