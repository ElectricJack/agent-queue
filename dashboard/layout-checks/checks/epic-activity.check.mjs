// Read-only fixture evidence for bright-nexus-99: every container retains
// IN_PROGRESS in storage while its displayed activity follows its leaves.
import assert from "node:assert/strict";
import { PROJECT, TASK_IDS } from "../fixtures/base.mjs";

export const name = "epic-activity";
export const profiles = ["desktop"];

// Each scenario: the rolled-up activity on the node, and the epic batch state.
// A batch in flight (collecting, delivering) keeps the delivery headline; its
// children are finished, so the rollup sets no display_state.
const scenarios = [
  { name: "waiting", display_state: "waiting", reason: "Needs you: waiting on a decision",
    title: "Foundation phase awaits approval", counts: { completed: 1, waiting: 2 },
    descendants: { COMPLETED: 1, DEFINED: 2 }, batch: "implementing", working: false },
  { name: "active", display_state: "active", reason: "1 descendant working",
    title: "Implementation worker is running", counts: { completed: 1, active: 1, waiting: 1 },
    descendants: { COMPLETED: 1, IN_PROGRESS: 1, DEFINED: 1 }, batch: "implementing", working: true },
  { name: "blocked", display_state: "blocked", reason: "All unfinished descendants are blocked or failed",
    title: "All remaining work is blocked", counts: { completed: 1, blocked: 2 },
    descendants: { COMPLETED: 1, BLOCKED: 1, FAILED: 1 }, batch: "implementing", working: false },
  { name: "collecting", display_state: null, reason: "All descendants finished",
    title: "Collector is integrating child work", counts: { completed: 3 },
    descendants: { COMPLETED: 3 }, batch: "integrating", label: "Collecting child work", working: true },
  { name: "delivering", display_state: null, reason: "All descendants finished",
    title: "Train batch is testing delivery", counts: { completed: 3 },
    descendants: { COMPLETED: 3 }, batch: "integrating", label: "Integrating to main", working: true },
];
const nodes = scenarios.map((scenario, index) => {
  const delivery = {
    state: scenario.batch, label: scenario.label ?? "Implementing", hold: null, reason: null,
    remedy: null, responsible: null, since: null, evidence: "current", links: [],
    implementation_completed: scenario.descendants.COMPLETED, implementation_total: 3,
  };
  return {
    id: TASK_IDS[index], title: scenario.title, status: "IN_PROGRESS", priority: 100,
    display_state: scenario.display_state, display_reason: scenario.reason,
    child_state_counts: scenario.counts, descendant_state_counts: scenario.descendants,
    delivery, delivery_status: delivery,
    x: (index % 3) * 1.25, y: Math.floor(index / 3) * 1.4, w: 1, h: 1, depth: 0,
    container_id: null, kind: "collapsed", context_only: false, agg_children: 3,
    agg_descendants: 5, agg_completed: 1, agg_running: 2, agg_blocked: 0, agg_active: 4,
  };
});
const scenarioFor = Object.fromEntries(nodes.map((node, index) => [node.id, scenarios[index]]));

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
      id: card.querySelector("[data-task-id]")?.dataset.taskId,
      state: card.dataset.displayState, striped: card.classList.contains("aq-stripe"),
      pulse: Boolean(card.querySelector(".aq-pulse")), counts: card.querySelector("[data-child-state-counts]")?.textContent,
      overflow: [...card.querySelectorAll("p, button, [data-display-reason]")].some((el) => el.getBoundingClientRect().bottom > box.bottom + 1),
      clippedTitle: title.clientHeight + 1 < Math.min(title.scrollHeight, parseFloat(getComputedStyle(title).lineHeight) * 2),
    };
  }));
  assert.equal(evidence.length, scenarios.length);
  for (const card of evidence) {
    const scenario = scenarioFor[card.id];
    assert.ok(scenario, `card ${card.id} is a fixture epic`);
    assert.equal(card.state, scenario.display_state ?? undefined, `${scenario.name} display state`);
    assert.equal(card.striped, scenario.working, `${scenario.name} stripes`);
    assert.equal(card.pulse, scenario.working, `${scenario.name} pulse`);
    assert.ok(card.counts?.includes(`${scenario.counts.completed} completed`), `${scenario.name} child counts`);
    assert.equal(card.overflow, false, `${scenario.name} card content overflows`);
    assert.equal(card.clippedTitle, false, `${scenario.name} second title line clipped`);
  }
  await t.shot("graph");

  await t.page.goto(t.url(`/projects/${PROJECT}/tasks`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector(`[data-task-row="${TASK_IDS[0]}"] [data-child-state-counts]`);
  assert.ok(await t.page.$('[aria-label="Waiting: 1 task"]'), "idle epic is in the waiting count");
  assert.ok(await t.page.$('[aria-label="In progress: 3 tasks"]'), "only real activity is in the running count");
  await t.shot("list");

  await t.page.goto(t.url(`/tasks/${TASK_IDS[0]}`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector('[data-testid="epic-delivery"]');
  const detail = await t.page.evaluate(() => {
    const texts = (selector) => [...document.querySelectorAll(selector)].map((el) => el.textContent).join(" | ");
    return { counts: texts("[data-child-state-counts]"), reason: texts("[data-display-reason]"), text: document.body.textContent };
  });
  assert.ok(detail.counts?.includes("2 waiting"), "detail shows waiting child counts");
  assert.ok(detail.reason?.includes("Needs you"), "detail explains why work waits");
  assert.ok(detail.text.includes("Waiting"), "detail heads the idle epic as waiting");
  await t.shot("detail");
}
