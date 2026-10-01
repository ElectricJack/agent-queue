import assert from "node:assert/strict";
import { expectLayout } from "../probes.mjs";
import { terminalHeaderFixtures, TERMINAL_SELECTION, WORKER_NAME } from "../fixtures/terminal-headers.mjs";
import { terminalHeaderGeometry } from "../terminal-header-geometry.mjs";
import { SESSION } from "../fixtures/base.mjs";

export const name = "terminal-headers";
const DETAILS = '[data-terminal-header] [aria-label^="Details for "]';
const DIALOG = "[data-terminal-details]";

async function oneRow(t) {
  const geometry = await terminalHeaderGeometry(t.page);
  assert.equal(geometry.length, 3);
  for (const pane of geometry) {
    assert.ok(pane.header <= 49, `${pane.name}: header grew to ${pane.header}px`);
    assert.ok(pane.terminal >= 200, `${pane.name}: only ${pane.terminal}px of terminal height`);
  }
  assert.equal(await t.page.$$eval("[data-terminal-header]", (headers) => headers.length), 3, "nested transport headers returned");
  const controls = await t.page.$$eval("[data-terminal-header] button, [data-terminal-header] a", (buttons) => buttons.map((button) => {
    const r = button.getBoundingClientRect();
    const h = button.closest("header").getBoundingClientRect();
    return { width: r.width, height: r.height, inside: r.left >= h.left && r.right <= h.right && r.top >= h.top && r.bottom <= h.bottom, label: button.getAttribute("aria-label") };
  }));
  for (const control of controls) {
    assert.ok(control.label && control.inside, `inaccessible/clipped control: ${JSON.stringify(control)}`);
    assert.ok(control.width >= 32 && control.height >= 32, `small target: ${JSON.stringify(control)}`);
  }
  await expectLayout(t);
  return geometry;
}

export async function run(t) {
  terminalHeaderFixtures(t.stub);
  await t.page.goto(t.url(TERMINAL_SELECTION), { waitUntil: "domcontentloaded" });
  await t.page.waitForFunction(() => document.querySelectorAll('[data-terminal-header] [aria-label^="Details for "]').length === 3);
  if (t.page.viewport().width >= 768) {
    await t.page.waitForFunction(() => document.querySelector('[aria-label$="terminal connection"]')?.textContent.includes("connected"));
    await t.page.waitForFunction(() => document.querySelector('[aria-label="Supervisor terminal connection"]')?.textContent.includes("error"));
  } else {
    await t.page.waitForFunction(() => document.querySelector('[aria-label$="terminal status"]')?.textContent === "Live");
    assert.deepEqual(t.stub.terminalUpgrades, [], "a phone attached a PTY");
  }
  await oneRow(t);
  await t.shot("three-panes");
  if (t.profile === "desktop") {
    await t.page.setViewport({ width: 1100, height: 700 });
    const geometry = await oneRow(t);
    // Recorded source baseline: same three panes had 119, 58, 19 px of rendered terminal.
    for (const [index, pane] of geometry.entries()) assert.ok(pane.terminal > [119, 58, 19][index] + 100);
    await t.shot("small-three-panes");
  }

  // Keyboard activation, complete long metadata, and Escape returning focus.
  await t.page.focus(DETAILS);
  await t.page.keyboard.press("Enter");
  await t.page.waitForSelector(DIALOG);
  assert.ok(await t.page.$eval(DIALOG, (panel) => panel === document.activeElement));
  assert.ok(await t.page.$eval(DIALOG, (panel, title) => panel.innerText.includes(title) && panel.innerText.includes("Profile:"), WORKER_NAME));
  await expectLayout(t);
  await t.shot("keyboard-details");
  await t.page.keyboard.press("Escape");
  assert.equal(await t.page.$(DIALOG), null);
  assert.ok(await t.page.$eval(DETAILS, (button) => button === document.activeElement));

  if (t.page.viewport().width >= 768) {
    // Disclosure never resizes/remounts the renderer or types into its session.
    const viewer = t.stub.terminalViewers.find((row) => row.sessionId === SESSION);
    assert.deepEqual(viewer.frames.filter((frame) => typeof frame === "string"), []);
    await t.page.click('[data-terminal-header] [aria-label^="Focus Builder"]');
    assert.ok(await t.page.$eval("[data-interactive-terminal] textarea", (input) => input === document.activeElement));
    await t.page.keyboard.down("Control");
    await t.page.keyboard.press("m");
    await t.page.keyboard.up("Control");
    assert.ok(await t.page.$eval('[aria-label^="Focus Builder"]', (button) => button === document.activeElement));
    assert.deepEqual(viewer.frames.filter((frame) => typeof frame === "string"), []);
    assert.equal(t.stub.terminalViewers.filter((row) => row.sessionId === SESSION).length, 1);
  }

  // Click/touch disclosure on the third pane; its picker and settings remain usable.
  const poolDetails = '[aria-label="Details for deep-high-claude pool"]';
  await t.page.$eval(poolDetails, (el) => el.scrollIntoView());
  if (t.isPhone) await t.page.tap(poolDetails); else await t.page.click(poolDetails);
  await t.page.waitForSelector(DIALOG);
  await expectLayout(t, { primary: [poolDetails, `${DIALOG} [role="tab"]`] });
  await t.shot("pool-details");
  assert.equal(await t.page.$$eval(`${DIALOG} select option`, (options) => options.length), 2);
  const panel = await t.page.$eval(DIALOG, (el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
  });
  assert.ok(panel.left >= 0 && panel.right <= t.page.viewport().width && panel.top >= 0 && panel.bottom <= t.page.viewport().height);
  await t.page.keyboard.press("Escape");
  await oneRow(t);
}
