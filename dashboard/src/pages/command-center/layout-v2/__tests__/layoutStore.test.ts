import { describe, expect, it } from "vitest";
import type { TilesResponse } from "@aq/ts-client";
import { emptyStore, evictFar, mergeTiles, missingCells, nodeCount } from "../layoutStore";

const node = (id: string, x: number, y: number, w = 1, h = 1) => ({
  id, title: id, status: "READY", priority: 100, is_blocked: false, x, y, w, h, depth: 0,
  container_id: null, kind: "card", context_only: false,
  agg_children: 0, agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0,
});
const res = (nodes: ReturnType<typeof node>[], extra: Partial<TilesResponse> = {}): TilesResponse =>
  ({ nodes, edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1, ...extra } as TilesResponse);

describe("layoutStore", () => {
  it("assigns nodes to every intersecting cell", () => {
    const s = mergeTiles(emptyStore(), ["0:0", "1:0"], res([node("a", 1, 1), node("wide", 7, 0, 3, 1)]));
    expect([...s.cells.get("0:0")!]).toEqual(["a", "wide"]);
    expect([...s.cells.get("1:0")!]).toEqual(["wide"]);
    expect(missingCells(s, ["0:0", "2:0"])).toEqual(["2:0"]);
    expect(nodeCount(s)).toBe(2);
  });
  it("resets on version change", () => {
    const s1 = mergeTiles(emptyStore(), ["0:0"], res([node("a", 0, 0)]));
    const s2 = mergeTiles(s1, ["1:0"], res([node("b", 9, 0)], { layout_version: 2 }));
    expect(s2.version).toBe(2);
    expect(s2.nodes.has("a")).toBe(false);
    expect(s2.loaded.has("0:0")).toBe(false);
  });
  it("evicts far cells and orphaned entities", () => {
    const far = mergeTiles(emptyStore(), ["10:10"], res([node("far", 81, 81)]));
    const s = mergeTiles(far, ["0:0"], res([node("a", 0, 0)], {
      edges: [{ from: "a", to: "far", dep_type: "blocks", description: null, count: 1 }],
      stubs: [{ id: "far", project_id: "p", x: 81, y: 81, w: 1, h: 1, title: "far" }],
    }));
    const e = evictFar(s, ["0:0"]);
    expect(e.nodes.has("far")).toBe(false);
    expect(e.nodes.has("a")).toBe(true);
    // the edge survives because its stub target is still referenced via cell 0:0's request
    expect(e.edges.size).toBe(1);
    expect(e.stubs.has("far")).toBe(true);
    expect(e.loaded.has("10:10")).toBe(false);
  });
});
