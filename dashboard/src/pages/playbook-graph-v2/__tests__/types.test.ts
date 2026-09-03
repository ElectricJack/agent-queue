import { describe, expect, it } from "vitest";
import {
  EDGE_KIND_LABELS,
  EDGE_KIND_STYLES,
  NEUTRAL_EDGE_STYLE,
  STEP_KIND_LABELS,
  STEP_KIND_TONES,
  UNTRAVERSED_EDGE_OPACITY,
  selectedEdgeStyle,
  type SemanticEdgeKind,
  type SemanticStepKind,
} from "../types";

const EDGE_KINDS: SemanticEdgeKind[] = [
  "success",
  "failure",
  "decision_case",
  "decision_default",
  "loop_body",
  "loop_exit",
  "loop_back",
  "timeout",
  "wait_matched",
  "runtime_error",
  "cancelled",
  "terminal",
];

const STEP_KINDS: SemanticStepKind[] = [
  "command",
  "llm",
  "agent_task",
  "decision",
  "wait",
  "foreach",
  "terminal",
];

describe("semantic graph style maps", () => {
  it("keeps every edge kind visually distinct without relying on colour", () => {
    const dashes = EDGE_KINDS.map((kind) => String(EDGE_KIND_STYLES[kind]!.strokeDasharray));
    expect(new Set(dashes).size).toBe(EDGE_KINDS.length);
    expect(dashes).not.toContain(String(NEUTRAL_EDGE_STYLE.strokeDasharray));
  });

  it("emphasises a selected edge without dropping the kind it encodes", () => {
    for (const kind of [...EDGE_KINDS, "unknown"]) {
      const base = EDGE_KIND_STYLES[kind] ?? NEUTRAL_EDGE_STYLE;
      const selected = selectedEdgeStyle(base);
      expect(selected.strokeWidth).toBeGreaterThan(base.strokeWidth as number);
      // Dash and colour are what say which kind this is; selection may not
      // repaint an edge into looking like a different transition.
      expect(selected.strokeDasharray).toBe(base.strokeDasharray);
      expect(selected.stroke).toBe(base.stroke);
      expect(String(selected.filter)).toContain(String(base.stroke));
    }
  });

  it("un-dims a selected edge the run overlay had faded", () => {
    const untraversed = { ...EDGE_KIND_STYLES.success, strokeOpacity: UNTRAVERSED_EDGE_OPACITY };
    const selected = selectedEdgeStyle(untraversed);
    // The overlay fades an edge this run never took; selecting it has to be
    // visible anyway, and the dashes and colour still carry "not traversed".
    expect(selected.strokeOpacity).toBe(1);
    expect(selected.strokeDasharray).toBe(untraversed.strokeDasharray);
    expect(selected.stroke).toBe(untraversed.stroke);
  });

  it("labels every edge kind and every step kind", () => {
    for (const kind of EDGE_KINDS) expect(EDGE_KIND_LABELS[kind]).toBeTruthy();
    for (const kind of STEP_KINDS) {
      expect(STEP_KIND_LABELS[kind]).toBeTruthy();
      expect(STEP_KIND_TONES[kind]).toBeTruthy();
    }
  });
});
