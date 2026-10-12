import assert from "node:assert/strict";
import { expectLayout } from "../probes.mjs";
import { terminalHeaderFixtures, TERMINAL_SELECTION, WORKER_NAME, SECOND_POOL_SESSION } from "../fixtures/terminal-headers.mjs";
import { terminalHeaderGeometry } from "../terminal-header-geometry.mjs";
import { POOL_SESSION, SESSION } from "../fixtures/base.mjs";

export const name = "terminal-headers";
const DETAILS = '[data-terminal-header] [aria-label^="Details for "]';
const DIALOG = "[data-terminal-details]";
const PICKER = '[data-terminal-header] select';

/**
 * One terminal at every width. Below 768 px its attach asks tmux for
 * scrollback and for the agent's own size back; at or above it asks for
 * neither, exactly like the host shell page's attach.
 */
function attaches(t, sessionId) {
  const found = t.stub.terminalUpgrades
    .map((raw) => new URL(raw, "http://stub.invalid"))
    .filter((url) => url.pathname === `/ws/terminal/${sessionId}`);
  assert.ok(found.length > 0, `no attach for ${sessionId}`);
  const wide = t.page.viewport().width >= 768;
  for (const url of found) {
    assert.equal(url.searchParams.get("history"), wide ? null : "2000", `wrong history in ${url}`);
    assert.equal(url.searchParams.get("restore_size"), wide ? null : "1", `wrong restore_size in ${url}`);
  }
}

async function oneRow(t) {
  const geometry = await terminalHeaderGeometry(t.page);
  assert.equal(geometry.length, 3);
  for (const pane of geometry) {
    assert.ok(pane.header <= 49, `${pane.name}: header grew to ${pane.header}px`);
    assert.ok(pane.terminal >= 200, `${pane.name}: only ${pane.terminal}px of terminal height`);
  }
  assert.equal(await t.page.$$eval("[data-terminal-header]", (headers) => headers.length), 3, "nested transport headers returned");
  const controls = await t.page.$$eval("[data-terminal-header] button, [data-terminal-header] a, [data-terminal-header] select", (buttons) => buttons.map((button) => {
    const r = button.getBoundingClientRect();
    const h = button.closest("header").getBoundingClientRect();
    return { width: r.width, height: r.height, inside: r.left >= h.left && r.right <= h.right && r.top >= h.top && r.bottom <= h.bottom, label: button.getAttribute("aria-label") || button.labels?.[0]?.textContent };
  }));
  for (const control of controls) {
    assert.ok(control.label && control.inside, `inaccessible/clipped control: ${JSON.stringify(control)}`);
    assert.ok(control.width >= 32 && control.height >= 32, `small target: ${JSON.stringify(control)}`);
  }
  await expectLayout(t, { primary: [PICKER] });
  return geometry;
}

export async function run(t) {
  const sessions = terminalHeaderFixtures(t.stub);
  await t.page.goto(t.url(TERMINAL_SELECTION), { waitUntil: "domcontentloaded" });
  await t.page.waitForFunction(() => document.querySelectorAll('[data-terminal-header] [aria-label^="Details for "]').length === 3);
  if (t.page.viewport().width >= 768) {
    await t.page.waitForFunction(() => document.querySelector('[aria-label$="terminal connection"]')?.textContent.includes("connected"));
    await t.page.waitForFunction(() => document.querySelector('[aria-label="Supervisor terminal connection"]')?.textContent.includes("error"));
  } else {
    // A phone gets the same terminal, attached with the phone's transport.
    await t.page.waitForFunction(() => document.querySelector('[aria-label$="terminal connection"]')?.textContent === "connected");
    await t.page.waitForFunction(() => document.querySelector('[aria-label="Supervisor terminal connection"]')?.textContent.includes("error"));
    attaches(t, SESSION);
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

  // Click/touch disclosure on the third pane; its picker stays usable, and the
  // terminal/settings switch rides the header wherever that row has room for it.
  const poolDetails = '[aria-label="Details for deep-high-claude pool"]';
  const poolHeader = '[aria-label="deep-high-claude pool agent window"] [data-terminal-header]';
  const compact = t.page.viewport().width < 640;
  await t.page.$eval(poolDetails, (el) => el.scrollIntoView());
  if (t.isPhone) await t.page.tap(poolDetails); else await t.page.click(poolDetails);
  await t.page.waitForSelector(DIALOG);
  await expectLayout(t, { primary: compact ? [poolDetails] : [poolDetails, `${poolHeader} [role="tab"]`] });
  await t.shot("pool-details");
  // One switch, in the header on a row that has room and in the disclosure on a
  // compact row of 44 px targets that has none.
  assert.equal(await t.page.$$eval(`${DIALOG} [role="tab"]`, (tabs) => tabs.length), compact ? 2 : 0,
    "the view switch is not where this viewport puts it");
  assert.equal(await t.page.$$eval(`${poolHeader} [role="tab"]`, (tabs) => tabs.length), compact ? 0 : 2,
    "the view switch is duplicated or missing from this row");
  assert.equal(await t.page.$$eval(`${DIALOG} select option`, (options) => options.length), 2);
  const panel = await t.page.$eval(DIALOG, (el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
  });
  assert.ok(panel.left >= 0 && panel.right <= t.page.viewport().width && panel.top >= 0 && panel.bottom <= t.page.viewport().height);
  await t.page.keyboard.press("Escape");
  await oneRow(t);

  // Native keyboard selection switches without opening the disclosure.
  await t.page.focus(PICKER);
  assert.ok(await t.page.$eval(PICKER, (el) => el === document.activeElement));
  await t.page.keyboard.press("ArrowDown");
  await t.page.keyboard.press("Enter");
  await t.page.waitForFunction((id) => document.querySelector('[data-terminal-header] select')?.value === id, {}, SECOND_POOL_SESSION);
  assert.equal(await t.page.$(DIALOG), null);
  assert.ok(new URL(t.page.url()).searchParams.getAll("agent").includes("pool:deep-high-claude@" + SECOND_POOL_SESSION));
  if (t.page.viewport().width >= 768) {
    await t.page.waitForFunction(() => document.querySelector('[aria-label="Second worker terminal connection"]')?.textContent.includes("connected"));
    assert.equal(t.stub.terminalViewers.filter((row) => row.sessionId === SECOND_POOL_SESSION).length, 1);
  } else {
    await t.page.waitForFunction(() => document.querySelector('[aria-label="Second worker terminal connection"]')?.textContent === "connected");
    assert.equal(t.stub.terminalViewers.filter((row) => row.sessionId === SECOND_POOL_SESSION).length, 1);
    attaches(t, SECOND_POOL_SESSION);
  }
  await oneRow(t);
  await t.shot("pool-header-selection");

  // A live session-exited refresh drops the selected option and rebinds the
  // remaining worker, leaving the original single-instance header behavior.
  sessions.splice(1, 1);
  t.stub.pushEvent({ _event_type: "session.exited", session_id: SECOND_POOL_SESSION });
  await t.page.waitForFunction(() => document.querySelector('[data-terminal-header] select') === null);
  if (t.page.viewport().width >= 768) {
    await t.page.waitForFunction(() => document.querySelector('[aria-label="Pool worker with a long session name terminal connection"]')?.textContent.includes("connected"));
    assert.equal(t.stub.terminalViewers.filter((row) => row.sessionId === POOL_SESSION).length, 2);
  } else {
    await t.page.waitForFunction(() => document.querySelector('[aria-label="Pool worker with a long session name terminal connection"]')?.textContent === "connected");
    assert.equal(t.stub.terminalViewers.filter((row) => row.sessionId === POOL_SESSION).length, 2);
  }
  assert.equal(await t.page.$(DIALOG), null);
  // The one remaining instance still has its existing picker in details.
  await t.page.click(poolDetails);
  await t.page.waitForSelector(`${DIALOG} select`);
  assert.equal(await t.page.$eval(`${DIALOG} select`, (el) => el.value), POOL_SESSION);
  await t.page.keyboard.press("Escape");
  await t.shot("pool-header-removal");

  // Last, because switching views remounts the terminal: on a header that has
  // room, the switch changes the pane body with no disclosure at all.
  if (!compact) {
    await t.page.click(`${poolHeader} [role="tab"][aria-label="Settings"]`);
    await t.page.waitForSelector('[aria-label="deep-high-claude pool settings"]');
    assert.equal(await t.page.$(DIALOG), null, "changing views opened the disclosure");
    await expectLayout(t, { primary: [`${poolHeader} [role="tab"]`] });
    await t.shot("pool-header-settings");
    await t.page.click(`${poolHeader} [role="tab"][aria-label="Terminal"]`);
    await t.page.waitForSelector('[aria-label$="terminal connection"]');
    assert.equal(await t.page.$(DIALOG), null);
  }
}
