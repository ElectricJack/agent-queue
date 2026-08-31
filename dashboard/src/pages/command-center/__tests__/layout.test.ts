import { describe, expect, it } from "vitest";
import { MarkerType } from "@xyflow/react";
import { columnsForWidth, layoutGraph } from "../layout";
import { NODE_HEIGHT, NODE_WIDTH } from "../types";
import { edge, graph, task } from "./fixtures";

describe("bounded task layout", () => {
  it("flows left to right before wrapping down, for isolated nodes and long chains", () => {
    const tasks = Array.from({ length: 40 }, (_, i) => task(`t${i}`));
    for (const edges of [[], tasks.slice(1).map((t, i) => edge(t.id, tasks[i]!.id, "blocks"))]) {
      const result = layoutGraph(graph(tasks, edges), { columns: 4 });
      const positions = result.nodes.map((n) => n.position);
      expect(new Set(positions.map((p) => p.x)).size).toBe(4);
      expect(positions[1]!.x).toBeGreaterThan(positions[0]!.x + NODE_WIDTH);
      expect(positions[3]!.y).toBe(positions[0]!.y);
      expect(positions[4]!.x).toBe(positions[0]!.x);
      expect(positions[4]!.y).toBeGreaterThan(positions[0]!.y + NODE_HEIGHT);
      expect(Math.max(...positions.map((p) => p.x)) + NODE_WIDTH).toBeLessThan(1200);
      for (let i = 0; i < positions.length; i += 1) {
        for (let j = i + 1; j < positions.length; j += 1) {
          expect(
            Math.abs(positions[i]!.x - positions[j]!.x) >= NODE_WIDTH
            || Math.abs(positions[i]!.y - positions[j]!.y) >= NODE_HEIGHT,
          ).toBe(true);
        }
      }
    }
  });

  it("adapts to the canvas width, with at most four columns", () => {
    expect(columnsForWidth(320)).toBe(1);
    expect(columnsForWidth(800)).toBe(2);
    expect(columnsForWidth(1050)).toBe(3);
    expect(columnsForWidth(2000)).toBe(4);
  });

  it("does not move existing cards on progress or priority updates and appends new tasks", () => {
    const before = layoutGraph(graph([task("a"), task("b"), task("c")]), { columns: 3 });
    const after = layoutGraph(graph([
      task("new", { priority: 1 }),
      task("c", { status: "FAILED" }),
      task("b", { status: "COMPLETED" }),
      task("a", { status: "IN_PROGRESS", priority: 5 }),
    ]), { columns: 3, orderedTaskIds: ["a", "b", "c"] });
    for (const old of before.nodes) {
      expect(after.nodes.find((n) => n.id === old.id)?.position).toEqual(old.position);
    }
    expect(after.nodes[3]!.id).toBe("new");
  });

  it("orders prerequisites first and renders canonical dependency arrows and distinct types", () => {
    const result = layoutGraph(graph(
      [task("dependent"), task("prerequisite"), task("child"), task("waiter"), task("failure"), task("discovery")],
      [
        edge("dependent", "prerequisite", "blocks"),
        edge("child", "prerequisite", "parent-child"),
        edge("waiter", "child", "waits-for"),
        edge("failure", "prerequisite", "conditional-blocks"),
        edge("discovery", "failure", "discovered-from"),
      ],
    ), { columns: 3, expandedTaskIds: new Set(["prerequisite"]) });
    expect(result.nodes.findIndex((n) => n.id === "prerequisite"))
      .toBeLessThan(result.nodes.findIndex((n) => n.id === "dependent"));
    const blocks = result.edges.find((e) => e.data?.depType === "blocks");
    expect(blocks).toMatchObject({
      source: "prerequisite", target: "dependent",
      markerEnd: { type: MarkerType.ArrowClosed },
    });
    expect(result.edges.find((e) => e.data?.depType === "parent-child")?.style?.strokeDasharray).toBeTruthy();
    expect(result.edges.find((e) => e.data?.depType === "waits-for")?.style?.stroke).toBe("#fbbf24");
    expect(result.edges.find((e) => e.data?.depType === "conditional-blocks")?.style?.stroke).toBe("#fb923c");
    expect(result.edges.find((e) => e.data?.depType === "discovered-from")?.label).toBe("discovered-from");
  });

  it("keeps every node even when dependency data contains a cycle", () => {
    const result = layoutGraph(graph(
      [task("a"), task("b"), task("c")],
      [edge("a", "b", "blocks"), edge("b", "a", "blocks")],
    ));
    expect(new Set(result.nodes.map((n) => n.id))).toEqual(new Set(["a", "b", "c"]));
  });
});
