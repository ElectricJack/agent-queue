// The focus shell at every profile: header controls, safe areas, toolbars,
// the cold-deep-link Back, and no roaming-preference traffic even when a
// desktop pane is stored.
import assert from "node:assert/strict";
import { expectLayout, rect, setSafeArea } from "../probes.mjs";
import { stateDocuments } from "../fixtures/base.mjs";

export const name = "focus-shell";

const PRIMARY = ['header [aria-label="Back"]', 'header [aria-label="Focus home"]', 'header [aria-label="Open in full dashboard"]'];

export async function run(t) {
  t.stub.override("POST /api/dashboard/state-list", () => stateDocuments({
    right_surface: { width: 760, kind: "pane", activity_tab: "gates", pane: { view: "task-detail", args: { taskId: "fixture-task-2" } } },
  }));
  await t.page.goto(t.url("/focus/tasks/fixture-task-1"), { waitUntil: "networkidle0" });
  await t.page.waitForSelector('header [aria-label="Back"]');
  await expectLayout(t, { primary: PRIMARY });
  const title = await rect(t.page, "header h1");
  assert.ok(title.width >= 40 && title.top >= 0, `the title is squeezed out (${title.width}px wide)`);
  const labels = await t.page.$$eval("header a, header button", (els) => els.map((el) => el.textContent.trim()));
  assert.ok(labels.every((text) => text.length > 0), "an icon-only header control has no visible label");

  // A notch and a home indicator: the header clears the top inset, the page the bottom one.
  await setSafeArea(t.page, { top: 47, bottom: 34 });
  const back = await rect(t.page, 'header [aria-label="Back"]');
  assert.ok(back.top >= 47, `Back sits under the notch (top ${back.top})`);
  await expectLayout(t, { primary: PRIMARY });

  // Browser toolbars take height: the shell tracks the dynamic viewport.
  const { width, height } = t.page.viewport();
  await t.page.setViewport({ ...t.page.viewport(), height: height - 80 });
  const shell = await rect(t.page, "[data-focus-shell]");
  assert.ok(shell.bottom <= height - 80 + 1, `shell overflows the shrunken viewport (${shell.bottom})`);
  await t.page.setViewport({ ...t.page.viewport(), width, height });

  // Cold deep link: Back goes to the focus home, not off the app.
  await t.page.click('header [aria-label="Back"]');
  await t.page.waitForFunction(() => location.pathname === "/focus");

  assert.deepEqual(t.stub.statePuts(), [], "a focus route wrote roaming preferences");
  assert.equal(await t.page.$("aside"), null, "the desktop rail or right surface rendered in focus mode");
  assert.deepEqual(t.sockets.terminal(), []);
}
