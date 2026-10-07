// Graph redesign phase A.2 in a real browser (spec §3, §4.2, §7.1): one card
// per status, a review wait, an epic and two stubs, measured by axe in both themes, and
// the running stripes standing still under prefers-reduced-motion. jsdom
// cannot paint, so contrast and animation are only provable here.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { PROJECT, routes, stateDocuments } from "../fixtures/base.mjs";

export const name = "graph-cards";
export const profiles = ["desktop"];

const node = (id, title, status, col, row, extra = {}) => ({
  id, title, status, priority: 100, is_blocked: false, x: col * 1.2, y: row * 1.3, w: 1, h: 1,
  depth: 0, container_id: null, kind: "card", context_only: false, agg_children: 0,
  agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0,
  profile_id: "standard-high-claude", intelligence_class: "standard-high", ...extra,
});

/** @satisfies {import("@aq/ts-client").LayoutNode[]} */
const NODES = [
  node("cards-1", "Design the reason-line vocabulary", "IN_PROGRESS", 0, 0, {
    subtasks_total: 4, subtasks_settled: 1, assigned_agent_id: "worker-a",
  }),
  node("cards-2", "Wire the frontier rank", "READY", 1, 0),
  node("cards-3", "Hand the card to a worker", "ASSIGNED", 2, 0),
  node("cards-4", "Port the stripes", "DEFINED", 3, 0, { is_blocked: true }),
  node("cards-5", "Ship the edge legend", "FAILED", 0, 1),
  node("cards-6", "Hold for the operator", "PAUSED", 1, 1),
  node("cards-7", "Ask about the scope", "WAITING_INPUT", 2, 1),
  node("cards-8", "Write the card spec", "COMPLETED", 3, 1),
  node("cards-9", "Not started yet", "DEFINED", 0, 2),
  node("cards-10", "Dropped idea", "CANCELLED", 1, 2),
  node("cards-11", "Answer the review", "DEFINED", 2, 2, {
    review_waits: [{
      review_id: "r-7f3a", review_state: "changes_requested", review_kind: "spec",
      review_title: "Card spec", gate_id: "gate-1", gate_type: "review", gate_status: "open",
      blocking: true,
    }],
  }),
  node("cards-12", "Graph redesign", "IN_PROGRESS", 3, 2, {
    kind: "collapsed", agg_children: 5, agg_descendants: 9, agg_completed: 3, agg_running: 2,
    agg_blocked: 1, agg_active: 6,
  }),
];

/** One boundary stub in this project and one from another, both dimmed (§1). */
const STUBS = [
  { id: "cards-far", project_id: PROJECT, x: 4.8, y: 1.3, w: 1, h: 1, title: "Far follow-up" },
  { id: "second-1", project_id: "second", x: 0, y: 0, w: 1, h: 1, title: "Upstream contract" },
];

/** @satisfies {import("@aq/ts-client").LayoutEdge[]} */
const EDGES = [
  { from: "cards-1", to: "cards-4", dep_type: "blocks", description: null, count: 1 },
  { from: "cards-2", to: "cards-6", dep_type: "waits-for", description: null, count: 1 },
  { from: "cards-5", to: "cards-9", dep_type: "conditional-blocks", description: null, count: 1 },
  { from: "cards-3", to: "cards-far", dep_type: "blocks", description: null, count: 1 },
  { from: "second-1", to: "cards-2", dep_type: "blocks", description: null, count: 1 },
];

export async function run(t) {
  t.stub.override("POST /api/dashboard/state-list", () => stateDocuments({ theme: "system" }));
  t.stub.override("POST /api/dashboard/state-get", (body) => body.namespace === "shell_preferences"
    ? { success: true, document: stateDocuments({ theme: "system" }).documents[0] }
    : routes["POST /api/dashboard/state-get"](body));
  t.stub.override("POST /api/playbook/list", () => ({ playbooks: [], count: 0 }));
  t.stub.override(`GET /api/projects/${PROJECT}/graph/extent`, () => ({
    layout_version: 1, extent_w: 6, extent_h: 4, node_count: NODES.length,
  }));
  t.stub.override(`GET /api/projects/${PROJECT}/graph/running-target`, () => null);
  t.stub.override(`POST /api/projects/${PROJECT}/graph/tiles`, () => ({
    layout_version: 1, nodes: NODES, edges: EDGES, stubs: STUBS, stub_overflow: [], workers: [],
    gates: [], variant_applied: "active",
  }));
  await t.page.emulateMediaFeatures([
    { name: "prefers-color-scheme", value: "dark" },
    { name: "prefers-reduced-motion", value: "no-preference" },
  ]);
  await t.page.goto(t.url(`/projects/${PROJECT}/graph`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector(`.aq-task-graph [data-task-card]`);
  // React Flow renders only what is on screen; fit the stubs into the view.
  await t.page.click(".aq-task-graph .react-flow__controls-fitview");
  await t.page.waitForFunction(
    (count) => document.querySelectorAll(".aq-task-graph [data-task-card]").length === count,
    { timeout: 5000 }, NODES.length + STUBS.length,
  );
  const requireAxe = createRequire(createRequire(import.meta.url).resolve("jest-axe"));
  await t.page.addScriptTag({ path: requireAxe.resolve("axe-core/axe.min.js") });

  const cardCount = await t.page.$$eval(".aq-task-graph [data-task-card]", (cards) => cards.length);
  assert.equal(cardCount, NODES.length + STUBS.length, "every fixture card and stub renders");

  for (const theme of ["dark", "light"]) {
    await t.page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await t.page.waitForSelector(`.aq-task-graph .react-flow.${theme}`);
    const evidence = await t.page.evaluate(async () => {
      const results = await window.axe.run({ include: [[".aq-task-graph"]] });
      const brief = (entry) => ({
        id: entry.id,
        nodes: entry.nodes.map((n) => ({ target: n.target.join(" "), summary: n.failureSummary ?? "" })),
      });
      // axe cannot see through the stripes' gradient, so measure that text
      // here: the card fill over the ground, then the stripe tint (the worst
      // case), then any pill tint, against the text colour.
      const rgba = (css) => {
        const probe = document.createElement("span");
        probe.style.color = css;
        document.body.append(probe);
        const [r, g, b, a = 1] = getComputedStyle(probe).color.match(/[\d.]+/g).map(Number);
        probe.remove();
        return { r, g, b, a };
      };
      const over = (top, under) => {
        const a = top.a + under.a * (1 - top.a);
        const mix = (k) => (top[k] * top.a + under[k] * under.a * (1 - top.a)) / a;
        return { r: mix("r"), g: mix("g"), b: mix("b"), a };
      };
      const toAxe = ({ r, g, b }) => new window.axe.commons.color.Color(r, g, b, 1);
      const ground = rgba("var(--g-ground)");
      const stripe = rgba("var(--g-stripe)");
      const striped = results.incomplete
        .filter((entry) => entry.id === "color-contrast")
        .flatMap((entry) => entry.nodes.map((n) => n.target.join(" ")))
        .map((target) => {
          const el = document.querySelector(target);
          const card = el.closest("[data-task-card].aq-stripe");
          if (!card) return { target, ratio: 0, reason: "not on a striped card" };
          const layers = [];
          for (let node = el; node && node !== card; node = node.parentElement) {
            layers.unshift(rgba(getComputedStyle(node).backgroundColor));
          }
          let bg = over(over(stripe, over(rgba(getComputedStyle(card).backgroundColor), ground)), ground);
          for (const layer of layers) bg = over(layer, bg);
          const ratio = window.axe.commons.color.getContrast(toAxe(bg), toAxe(over(rgba(getComputedStyle(el).color), bg)));
          return { target, ratio };
        });
      return {
        striped,
        violations: results.violations.map(brief),
        incomplete: results.incomplete.map(brief),
        contrastChecked: results.passes.find((pass) => pass.id === "color-contrast")?.nodes.length ?? 0,
      };
    });
    assert.deepEqual(evidence.violations, [], `${theme} axe violations in the graph region`);
    // Text over the running stripes is a background image to axe; anything
    // else it could not measure is a gap in the evidence.
    const incompleteContrast = evidence.incomplete
      .filter((entry) => entry.id === "color-contrast")
      .flatMap((entry) => entry.nodes);
    const unmeasured = incompleteContrast
      .filter((n) => !/background image|background gradient/i.test(n.summary));
    assert.deepEqual(unmeasured, [], `${theme} text contrast axe could not measure`);
    for (const node of evidence.striped) {
      assert.ok(node.ratio >= 4.5, `${theme} ${node.target} over the stripes: ${node.reason ?? node.ratio.toFixed(2)} < 4.5`);
    }
    const lowest = Math.min(...evidence.striped.map((node) => node.ratio));
    assert.ok(evidence.contrastChecked >= (NODES.length + STUBS.length) * 3, `${theme} axe measured too little text`);
    console.log(`${theme}: axe clean; ${evidence.contrastChecked} text nodes passed contrast, ${evidence.striped.length} over the stripes measured here (lowest ${lowest.toFixed(2)})`);
    await t.shot(theme);
  }

  // §3.3: the stripes drift, and stop under prefers-reduced-motion.
  const stripeAnimation = () => t.page.$eval(
    ".aq-task-graph [data-task-card].aq-stripe", (card) => getComputedStyle(card).animationName,
  );
  assert.equal(await stripeAnimation(), "aq-stripe-drift", "running stripes drift by default");
  await t.page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
  assert.equal(await stripeAnimation(), "none", "reduced motion stops the stripes");
  const pulse = await t.page.$eval(".aq-task-graph .aq-pulse", (dot) => getComputedStyle(dot).animationName);
  assert.equal(pulse, "none", "reduced motion stops the running pulse");
  await t.shot("reduced-motion");
}
