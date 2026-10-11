// Read-only fixture evidence for bright-nexus-99: every container retains
// IN_PROGRESS in storage while its displayed activity follows its leaves.
import assert from "node:assert/strict";
import { PROJECT, TASK_IDS } from "../fixtures/base.mjs";

export const name = "epic-activity";
export const profiles = ["desktop"];

const states = ["waiting", "active", "blocked", "collecting", "delivering"];
const reasons = ["Needs you: waiting on a decision", "1 descendant working",
  "All unfinished descendants are blocked or failed", "Collecting child work", "Train batch is testing"];
const titles = ["Foundation phase awaits approval", "Implementation worker is running",
  "All remaining work is blocked", "Collector is integrating child work", "Train batch is testing delivery"];
const nodes = states.map((state, index) => {
  const childState = state === "active" ? "active" : state === "blocked" ? "blocked" : "waiting";
  const descendants = state === "active" ? { COMPLETED: 1, IN_PROGRESS: 1, DEFINED: 1 }
    : state === "blocked" ? { COMPLETED: 1, BLOCKED: 1, FAILED: 1 } : { COMPLETED: 1, DEFINED: 2 };
  const delivery = {
    state: state === "collecting" || state === "delivering" ? "integrating" : "implementing",
    label: state === "collecting" ? "Collecting child work" : state === "delivering" ? "Integrating to main" : state,
    display_status: state[0].toUpperCase() + state.slice(1), display_state: state, display_reason: reasons[index],
    reason: reasons[index], evidence: "current", implementation_completed: 1, implementation_total: 3,
    child_state_counts: state === "active" ? { completed: 1, active: 1, waiting: 1 }
      : { completed: 1, [childState]: 2 }, descendant_state_counts: descendants, links: [],
  };
  return {
    id: TASK_IDS[index], title: titles[index], status: "IN_PROGRESS", priority: 100,
    display_state: state, display_reason: reasons[index], child_state_counts: delivery.child_state_counts,
    descendant_state_counts: descendants, delivery, delivery_status: delivery,
    x: (index % 3) * 1.25, y: Math.floor(index / 3) * 1.4, w: 1, h: 1, depth: 0,
    container_id: null, kind: "collapsed", context_only: false, agg_children: 3,
    agg_descendants: 5, agg_completed: 1, agg_running: 2, agg_blocked: 0, agg_active: 4,
  };
});

export async function run(t) {
  t.stub.override(`GET /api/projects/${PROJECT}/graph/extent`, () => ({
    layout_version: 1, extent_w: 3.8, extent_h: 2.5, node_count: nodes.length,
  }));
  t.stub.override(`GET /api/projects/${PROJECT}/graph/running-target`, () => null);
  t.stub.override(`POST /api/projects/${PROJECT}/graph/tiles`, () => ({
    layout_version: 1, nodes, edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], variant_applied: "active",
  }));
  t.stub.override(`GET /api/projects/${PROJECT}/graph`, () => ({ tasks: nodes, edges: [], gates: [], agents: [] }));
  t.stub.override("POST /api/task/get", (body) => {
    const node = nodes.find((node) => node.id === body.task_id);
    return { ...node, project_id: PROJECT, description: "A phase with no worker running. Stored lifecycle remains IN_PROGRESS.",
      children: [], depends_on: [], blocks: [], labels: [] };
  });
  t.stub.override("POST /api/playbook/list", () => ({ playbooks: [], count: 0 }));
  t.stub.override(`GET /api/tasks/${TASK_IDS[0]}/sessions`, () => ({ task_id: TASK_IDS[0], sessions: [] }));
  await t.page.goto(t.url(`/projects/${PROJECT}/graph`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector("[data-task-card][data-display-state=waiting]");
  await t.page.click(".aq-task-graph .react-flow__controls-fitview");
  const evidence = await t.page.$$eval("[data-task-card]", (cards) => cards.map((card) => {
    const box = card.getBoundingClientRect();
    const title = card.querySelector("p[title]");
    return {
      state: card.dataset.displayState, striped: card.classList.contains("aq-stripe"),
      pulse: Boolean(card.querySelector(".aq-pulse")), counts: card.querySelector("[data-child-state-counts]")?.textContent,
      overflow: [...card.querySelectorAll("p, button, [data-display-reason]")].some((el) => el.getBoundingClientRect().bottom > box.bottom + 1),
      clippedTitle: title.clientHeight + 1 < Math.min(title.scrollHeight, parseFloat(getComputedStyle(title).lineHeight) * 2),
    };
  }));
  assert.equal(evidence.length, states.length);
  for (const card of evidence) {
    const working = ["active", "collecting", "delivering"].includes(card.state);
    assert.equal(card.striped, working, `${card.state} stripes`);
    assert.equal(card.pulse, working, `${card.state} pulse`);
    assert.ok(card.counts?.includes("1 completed"), `${card.state} child counts`);
    assert.equal(card.overflow, false, `${card.state} card content overflows`);
    assert.equal(card.clippedTitle, false, `${card.state} second title line clipped`);
  }
  await t.shot("graph");

  await t.page.goto(t.url(`/projects/${PROJECT}/tasks`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector(`[data-task-row="${TASK_IDS[0]}"] [data-child-state-counts]`);
  assert.ok(await t.page.$('[aria-label="Waiting: 1 task"]'), "idle epic is in the waiting count");
  assert.ok(await t.page.$('[aria-label="In progress: 3 tasks"]'), "only real activity is in the running count");
  await t.shot("list");

  await t.page.goto(t.url(`/tasks/${TASK_IDS[0]}`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector('[data-testid="epic-delivery"]');
  const text = await t.page.$eval('[data-testid="epic-delivery"]', (el) => el.textContent);
  assert.ok(text.includes("2 waiting"), "detail shows waiting child counts");
  assert.ok(text.includes("Needs you"), "detail explains why work waits");
  await t.shot("detail");
}
