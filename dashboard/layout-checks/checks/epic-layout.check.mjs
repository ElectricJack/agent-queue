// Real React Flow geometry and hit-testing: jsdom cannot prove that a selected
// opaque container still paints underneath its children.
import assert from "node:assert/strict";
import { PROJECT, TASK_IDS } from "../fixtures/base.mjs";

export const name = "epic-layout";
export const profiles = ["desktop"];

const epicId = TASK_IDS[0];
const children = TASK_IDS.slice(1, 10).map((id, i) => ({
  id, title: `Execution step ${i + 1}`, status: i < 4 ? "COMPLETED" : "READY",
  priority: 100, is_blocked: false,
  x: 0.1 + (i % 4) * 1.15, y: 0.45 + Math.floor(i / 4) * 1.22,
  w: 1, h: 1, depth: 1, container_id: epicId, kind: "card", context_only: false,
  agg_children: 0, agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0,
}));
// Intentionally undersized old geometry: the entered frame must grow to fit.
const epic = {
  ...children[0], id: epicId, title: "Execution epic", status: "IN_PROGRESS",
  x: 0, y: 0, w: 3, h: 2, depth: 0, container_id: null, kind: "container",
  agg_children: children.length, agg_descendants: children.length, agg_completed: 4, agg_active: 5,
};

export async function run(t) {
  t.stub.override("POST /api/playbook/list", () => ({ playbooks: [], count: 0 }));
  t.stub.override(`GET /api/projects/${PROJECT}/graph/extent`, () => ({
    layout_version: 1, extent_w: 6, extent_h: 6, node_count: children.length + 1,
  }));
  t.stub.override(`GET /api/projects/${PROJECT}/graph/running-target`, () => null);
  t.stub.override(`GET /api/projects/${PROJECT}/graph/node/${epicId}`, () => ({
    layout_version: 1, node: epic, ancestors: [],
  }));
  t.stub.override(`POST /api/projects/${PROJECT}/graph/tiles`, () => ({
    layout_version: 1, nodes: [epic, ...children], edges: [], stubs: [], stub_overflow: [],
    workers: [], gates: [], variant_applied: "all",
  }));
  const url = `/projects/${PROJECT}/graph?focus=${epicId}&completed=1`;
  await t.page.goto(t.url(url), { waitUntil: "networkidle0" });
  await t.page.waitForSelector(`[data-container-id="${epicId}"]`);
  await t.page.click(".aq-task-graph .react-flow__controls-fitview");
  await t.page.waitForFunction(
    (count) => document.querySelectorAll(".aq-task-graph [data-task-card]").length === count,
    { timeout: 5000 }, children.length,
  );

  const measure = () => t.page.evaluate((id) => {
    const frame = document.querySelector(`[data-container-id="${id}"]`);
    const box = frame.getBoundingClientRect();
    const wrapper = frame.closest(".react-flow__node");
    const cards = [...document.querySelectorAll(".aq-task-graph [data-task-card]")];
    return {
      selected: wrapper.classList.contains("selected"),
      cards: cards.map((card) => {
        const bounds = card.getBoundingClientRect();
        const taskId = card.querySelector("[data-task-id]").dataset.taskId;
        const cardWrapper = card.closest(".react-flow__node");
        const hit = document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
        return {
          id: taskId,
          inside: bounds.left >= box.left && bounds.right <= box.right + 1
            && bounds.top >= box.top && bounds.bottom <= box.bottom + 1,
          covered: !card.contains(hit),
          aboveFrame: Number(getComputedStyle(cardWrapper).zIndex) > Number(getComputedStyle(wrapper).zIndex),
          hitFrame: Boolean(hit && frame.contains(hit)),
        };
      }),
    };
  }, epicId);
  const assertCards = (evidence) => {
    assert.equal(evidence.cards.length, children.length);
    for (const card of evidence.cards) {
      assert.ok(card.inside, `${card.id} must fit inside the epic frame`);
      assert.ok(card.aboveFrame, `${card.id} must paint above the epic frame`);
      assert.equal(card.covered, false, `${card.id} must remain visible and clickable`);
    }
  };
  assertCards(await measure());
  // Opening the epic selects its frame as well as the task-detail pane.
  await t.page.click(`[data-container-id="${epicId}"] [data-task-id="${epicId}"]`);
  await t.page.waitForFunction((id) => {
    const frame = document.querySelector(`[data-container-id="${id}"]`);
    return frame?.closest(".react-flow__node").classList.contains("selected");
  }, { timeout: 5000 }, epicId);
  // The detail pane reduces the graph viewport and can cull offscreen cards.
  // Every rendered child must still fit and paint above the selected frame.
  const selected = await measure();
  assert.equal(selected.selected, true);
  assert.ok(selected.cards.length > 0);
  for (const card of selected.cards) {
    assert.ok(card.inside, `${card.id} must remain inside the selected epic`);
    assert.ok(card.aboveFrame, `${card.id} must paint above the selected epic`);
    assert.equal(card.hitFrame, false, `${card.id} must not be hidden by the selected epic`);
  }
  await t.shot("selected");
}
