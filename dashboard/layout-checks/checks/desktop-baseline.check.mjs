// The existing Tasks tab at desktop sizes: the harness runs the real app, and the
// fixtures cover everything the shell asks for. Phone profiles join once the
// compact shell lands (Task 7/8 add their own checks).
import assert from "node:assert/strict";
import { expectLayout } from "../probes.mjs";
import { PROJECT } from "../fixtures/base.mjs";

export const name = "desktop-baseline";
export const profiles = ["desktop"];

export async function run(t) {
  await t.page.goto(t.url(`/projects/${PROJECT}/tasks`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[data-task-row]");
  await expectLayout(t);
  assert.deepEqual(t.stub.terminalUpgrades, []);
}
