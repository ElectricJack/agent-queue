// Real-browser contrast is required: jsdom/jest-axe cannot measure painted
// text. Exercise the canvas chrome and the pairs in the graph spec §4.2.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { PROJECT, routes, stateDocuments } from "../fixtures/base.mjs";

export const name = "graph-theme";
export const profiles = ["desktop"];

export async function run(t) {
  t.stub.override("POST /api/dashboard/state-list", () => stateDocuments({ theme: "system" }));
  t.stub.override("POST /api/dashboard/state-get", (body) => body.namespace === "shell_preferences"
    ? { success: true, document: stateDocuments({ theme: "system" }).documents[0] }
    : routes["POST /api/dashboard/state-get"](body));
  t.stub.override("POST /api/playbook/list", () => ({ playbooks: [], count: 0 }));
  t.stub.override(`GET /api/projects/${PROJECT}/graph/extent`, () => ({
    layout_version: 1, extent_w: 4, extent_h: 4, node_count: 0,
  }));
  t.stub.override(`GET /api/projects/${PROJECT}/graph/running-target`, () => null);
  t.stub.override(`POST /api/projects/${PROJECT}/graph/tiles`, () => ({
    layout_version: 1, nodes: [], edges: [], stubs: [], stub_overflow: [], workers: [], gates: [],
    variant_applied: "active",
  }));
  await t.page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
  await t.page.goto(t.url(`/projects/${PROJECT}/graph`), { waitUntil: "networkidle0" });
  await t.page.waitForSelector(".aq-task-graph .react-flow.dark");
  // Use the same axe version as our declared jest-axe dependency, rather than
  // an older copy hoisted by an unrelated package.
  const requireAxe = createRequire(createRequire(import.meta.url).resolve("jest-axe"));
  await t.page.addScriptTag({ path: requireAxe.resolve("axe-core/axe.min.js") });

  // Card markup is phase A.2. These samples test the new token contract
  // independently of the existing cards, using the built stylesheet.
  await t.page.evaluate(() => {
    const samples = document.createElement("section");
    samples.id = "graph-token-pairs";
    samples.setAttribute("aria-label", "Graph token contrast samples");
    samples.style.cssText = "position:fixed;top:220px;left:260px;width:780px;display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:16px;background:var(--g-ground);font:12px var(--g-font);z-index:10";
    const textPair = (label, ink, tint) => {
      const card = document.createElement("div");
      card.style.cssText = "background:var(--g-card);padding:12px";
      const text = document.createElement("span");
      text.textContent = label;
      text.style.cssText = `color:var(${ink});background:${tint ? `var(${tint})` : "transparent"};display:inline-block;padding:6px`;
      card.append(text);
      samples.append(card);
    };
    textPair("Text on card", "--g-text");
    textPair("Muted text on card", "--g-text-muted");
    textPair("Dim text on card", "--g-text-dim");
    textPair("Accent ink on card", "--g-accent-ink");
    textPair("Accent ink on pill", "--g-accent-ink", "--g-accent-soft");
    for (const status of ["done", "ready", "blocked", "paused", "failed", "call"]) {
      textPair(`${status} pill`, `--g-${status}`, `--g-${status}-soft`);
    }
    document.body.append(samples);
  });

  for (const theme of ["dark", "light"]) {
    // The real shell resolves "system" and publishes the changed attribute;
    // React Flow must update without navigating or remounting the page.
    await t.page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await t.page.waitForSelector(`.aq-task-graph .react-flow.${theme}`);
    const evidence = await t.page.evaluate(async () => {
      const flow = document.querySelector(".aq-task-graph .react-flow");
      const probe = document.createElement("span");
      document.body.append(probe);
      const tokenColor = (token) => {
        probe.style.color = `var(${token})`;
        return getComputedStyle(probe).color;
      };
      const button = document.querySelector(".react-flow__controls-button");
      const dots = document.querySelector(".react-flow__background-pattern");
      const chrome = {
        ground: getComputedStyle(flow).backgroundColor,
        expectedGround: tokenColor("--g-ground"),
        grid: getComputedStyle(dots).fill,
        expectedGrid: tokenColor("--g-grid"),
        buttonBackground: getComputedStyle(button).backgroundColor,
        expectedButtonBackground: tokenColor("--g-panel"),
        buttonText: getComputedStyle(button).color,
        expectedButtonText: tokenColor("--g-text"),
      };
      const color = window.axe.commons.color;
      const parse = (token) => {
        const parsed = new color.Color();
        parsed.parseString(tokenColor(token));
        return parsed;
      };
      const graphics = [
        ...["ready", "blocked", "failed", "accent"].map((status) => [`--g-${status}`, "--g-card"]),
        ["--g-edge", "--g-ground"], ["--g-edge-strong", "--g-ground"],
      ].map(([ink, background]) => ({ ink, ratio: color.getContrast(parse(background), parse(ink)) }));
      probe.remove();
      const results = await window.axe.run({ include: [["#graph-token-pairs"], [".react-flow__controls"]] }, {
        runOnly: { type: "rule", values: ["color-contrast"] },
      });
      return {
        chrome, graphics,
        violations: results.violations,
        incomplete: results.incomplete,
        passes: results.passes.map((pass) => ({ id: pass.id, nodes: pass.nodes.length })),
      };
    });
    const { chrome } = evidence;
    assert.equal(chrome.ground, chrome.expectedGround, `${theme} canvas background`);
    assert.equal(chrome.grid, chrome.expectedGrid, `${theme} background dots`);
    assert.equal(chrome.buttonBackground, chrome.expectedButtonBackground, `${theme} controls fill`);
    assert.equal(chrome.buttonText, chrome.expectedButtonText, `${theme} controls ink`);
    for (const pair of evidence.graphics) {
      assert.ok(pair.ratio >= 3, `${theme} ${pair.ink} contrast ${pair.ratio} < 3`);
    }
    assert.deepEqual(evidence.violations, [], `${theme} token text contrast violations`);
    assert.deepEqual(evidence.incomplete, [], `${theme} token text contrast could not be measured`);
    assert.ok(evidence.passes.some((pass) => pass.id === "color-contrast" && pass.nodes >= 11),
      `${theme} axe did not check every text sample`);
    console.log(`${theme}: axe text contrast passed; graphics ${evidence.graphics.map((p) => p.ratio.toFixed(2)).join(", ")}`);
    await t.shot(theme);
  }
  // Direct user preference changes use the same data-theme observer.
  await t.page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await t.page.waitForSelector(".aq-task-graph .react-flow.dark");
}
