// A cold load of a dotted child id serves the app; a missing asset is a 404.
import assert from "node:assert/strict";
import { waitForText } from "../probes.mjs";
import { CHILD_TASK_ID } from "../fixtures/base.mjs";

export const name = "deep-links";
export const profiles = ["phone-390", "desktop"];

export async function run(t) {
  const response = await t.page.goto(t.url(`/focus/tasks/${CHILD_TASK_ID}`), { waitUntil: "networkidle0" });
  assert.equal(response.status(), 200);
  await waitForText(t.page, "Dotted child task");
  const legacy = await t.page.goto(t.url(`/tasks/${CHILD_TASK_ID}`), { waitUntil: "networkidle0" });
  assert.equal(legacy.status(), 200);
  await waitForText(t.page, "Dotted child task");
  const missing = await fetch(t.url("/assets/missing-chunk.js"));
  assert.equal(missing.status, 404);
}
