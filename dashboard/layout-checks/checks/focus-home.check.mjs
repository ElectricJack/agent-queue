// The focus home: three sections at every profile, 44 px targets, the long
// Unicode title, pagination, and Back restoring filters, page and scroll.
import assert from "node:assert/strict";
import { expectLayout } from "../probes.mjs";
import { TASK_IDS } from "../fixtures/base.mjs";

export const name = "focus-home";

const PRIMARY = [
  "a[href^='/focus/sessions/']", "[data-task-row]",
  "input[aria-label='Search tasks']", "select[aria-label='Status']", "select[aria-label='Project']",
  "button[aria-label='Next page']",
];
const rowIds = (t) => t.page.$$eval("[data-task-row]", (els) => els.map((el) => el.getAttribute("data-task-row")));
// The router commits a URL edit in a transition: the address moves before the
// list renders, so wait for the rendered pager ("2 / 3"), not the URL alone.
const onPage = (t, search, pager) => t.page.waitForFunction((search, pager) =>
  location.search.includes(search)
  && document.querySelector("nav[aria-label='Task pages'] span")?.textContent === pager, {}, search, pager);

export async function run(t) {
  await t.page.goto(t.url("/focus"), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[data-task-row]");
  assert.equal((await rowIds(t)).length, 50);
  assert.equal((await rowIds(t))[0], TASK_IDS[0]); // the long Unicode title is on page 1
  await expectLayout(t, { primary: PRIMARY });

  // Pagination: 50 / 50 / 20, in order.
  await t.page.click("button[aria-label='Next page']");
  await onPage(t, "page=2", "2 / 3");
  assert.equal((await rowIds(t))[0], TASK_IDS[50]);
  await t.page.click("button[aria-label='Next page']");
  await onPage(t, "page=3", "3 / 3");
  assert.equal((await rowIds(t)).length, 20);
  assert.ok(await t.page.$eval("button[aria-label='Next page']", (b) => b.disabled));

  // Back restores filter, project, page and scroll.
  await t.page.select("select[aria-label='Project']", "fixture");
  await onPage(t, "project=fixture", "1 / 3");
  await t.page.type("input[aria-label='Search tasks']", "Fixture task");
  await onPage(t, "q=Fixture+task", "1 / 3");
  await t.page.click("button[aria-label='Next page']");
  await onPage(t, "page=2", "2 / 3");
  const before = new URL(t.page.url()).search;
  await t.page.$eval("main", (main) => { main.scrollTop = 600; main.dispatchEvent(new Event("scroll")); });
  const scrolled = await t.page.$eval("main", (main) => main.scrollTop);
  assert.ok(scrolled > 0, "the list is not tall enough to scroll");
  // The first card in view after scrolling — the one a thumb would tap.
  const visible = await t.page.$$eval("[data-task-row]", (els) => {
    const main = document.querySelector("main").getBoundingClientRect();
    return els.find((el) => el.getBoundingClientRect().top > main.top + 10)?.getAttribute("href");
  });
  assert.ok(visible, "no card in view");
  await t.page.click(`a[href='${visible}']`);
  await t.page.waitForFunction(() => location.pathname.startsWith("/focus/tasks/"));
  await t.page.goBack();
  await t.page.waitForFunction(() => location.pathname === "/focus");
  assert.equal(new URL(t.page.url()).search, before, "Back lost the list's filters or page");
  await t.page.waitForFunction((y) => Math.abs(document.querySelector("main").scrollTop - y) <= 2, { timeout: 5_000 }, scrolled);

  assert.deepEqual(t.stub.statePuts(), []);
  assert.deepEqual(t.sockets.terminal(), []);
}
