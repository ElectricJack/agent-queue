// /agents attaches for a fixed worker and for a pool instance. It is the host
// shell page's terminal at every width: below 768 px the attach asks tmux for
// history and for the agent's window size back, and the drawn ruler matches the
// size tmux was given. There is no phone-only terminal and no watch/type mode.
import assert from "node:assert/strict";
import { expectLayout } from "../probes.mjs";
import { POOL_SESSION, SESSION } from "../fixtures/base.mjs";

export const name = "agents-terminal";

const VIEWS = [
  { agent: "worker-a", session: SESSION },
  { agent: "pool:deep-high-claude", session: POOL_SESSION },
];
const HOST = "[data-interactive-terminal]";
const attachesFor = (t, session) => t.stub.terminalUpgrades
  .map((url) => new URL(url, "http://stub.invalid"))
  .filter((url) => url.pathname === `/ws/terminal/${session}`);

/** A live screen for the session, with a ruler that wraps at the column count. */
const SCREEN = `${"#".repeat(200)}\r\nclaimed: fixture-task-1\r\n$ `;

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
async function poll(read, ok, timeout, message) {
  for (const end = Date.now() + timeout; ;) {
    const value = await read();
    if (ok(value)) return value;
    if (Date.now() > end) throw new Error(`${message}: ${JSON.stringify(value)}`);
    await sleep(50);
  }
}

export async function run(t) {
  const compact = t.page.viewport().width < 768;
  for (const view of VIEWS) t.stub.allowTerminal(view.session, SCREEN);
  for (const view of VIEWS) {
    await t.page.goto(t.url(`/agents?agent=${encodeURIComponent(view.agent)}`), { waitUntil: "domcontentloaded" });
    await t.page.waitForSelector(HOST);
    await poll(() => attachesFor(t, view.session).length > 0, Boolean, 10_000, `no attach for ${view.agent}`);
    const attach = attachesFor(t, view.session)[0];
    const query = attach.searchParams;
    const size = Number(query.get("cols"));
    assert.ok(size > 0 && Number(query.get("rows")) > 0, `the attach carries no size: ${attach}`);
    if (compact) {
      assert.equal(query.get("history"), "2000", `a phone attach asks for no history: ${attach}`);
      assert.equal(query.get("restore_size"), "1", `a phone attach keeps the phone's size: ${attach}`);
    } else {
      assert.equal(query.get("history"), null, `a desktop attach asks for scrollback: ${attach}`);
      assert.equal(query.get("restore_size"), null, `a desktop attach restores a window size: ${attach}`);
    }
    // The drawn ruler wraps at the column count tmux was given.
    await poll(() => t.page.$eval(HOST, (host) => {
      const rows = [...host.querySelectorAll(".xterm-rows > div")].map((row) => (row.textContent ?? "").trimEnd());
      const ruler = rows.filter((row) => /^#+$/.test(row)).map((row) => row.length);
      return { cols: ruler.length ? Math.max(...ruler) : null, text: host.textContent ?? "" };
    }), (shown) => shown.text.includes("claimed: fixture-task-1") && shown.cols === size, 10_000,
      `${view.agent} never drew ${size} columns of the fixture screen`);
    assert.equal(await t.page.$("[data-phone-terminal]"), null, "a phone mounted the retired phone terminal");
    assert.equal(await t.page.$('[aria-label="Watch only"]'), null, `${view.agent} offers a watch/type mode`);
    await t.page.click('[aria-label^="Details for "]');
    // The rail is a drawer below 768 px, so its opener only exists there.
    await expectLayout(t, t.page.viewport().width < 768 ? { primary: ['[aria-label="Open navigation"]'] } : {});
    await t.page.keyboard.press("Escape");
    await t.shot(view.agent.startsWith("pool:") ? "pool" : "agent");
  }
  if (!compact) return;
  const sessions = new Set(t.stub.terminalUpgrades.map((url) => new URL(url, "http://stub.invalid").pathname));
  assert.deepEqual([...sessions].sort(), VIEWS.map((view) => `/ws/terminal/${view.session}`).sort(), "a phone attached another session");
}
