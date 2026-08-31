import { describe, expect, it } from "vitest";
import { projectHierarchy, retainTaskOrder } from "../hierarchy";
import { edge, graph, task } from "./fixtures";

const family = graph(
  [
    task("parent"),
    task("child", { status: "COMPLETED", is_blocked: true }),
    task("grandchild", { status: "IN_PROGRESS" }),
    task("sibling", { status: "READY", is_blocked: true }),
    task("other"),
  ],
  [edge("child", "parent"), edge("grandchild", "child"), edge("sibling", "parent")],
);

describe("task hierarchy projection", () => {
  it("starts groups collapsed and counts every descendant regardless of visibility", () => {
    const projected = projectHierarchy(family);
    expect(projected.tasks.map((t) => t.id)).toEqual(["parent", "other"]);
    expect(projected.details.get("parent")).toMatchObject({
      childCount: 2, descendantCount: 3, completedCount: 1,
      runningCount: 1, blockedCount: 1, expanded: false, depth: 0,
    });
    expect(projected.visibleTaskById.get("grandchild")).toBe("parent");
  });

  it("expands one level at a time and restores nested expansion after its parent is reopened", () => {
    const expanded = new Set(["parent"]);
    expect(projectHierarchy(family, { expandedTaskIds: expanded }).tasks.map((t) => t.id))
      .toEqual(["parent", "child", "sibling", "other"]);
    expanded.add("child");
    expect(projectHierarchy(family, { expandedTaskIds: expanded }).tasks.map((t) => t.id))
      .toEqual(["parent", "child", "grandchild", "sibling", "other"]);
    expanded.delete("parent");
    expect(projectHierarchy(family, { expandedTaskIds: expanded }).tasks.map((t) => t.id))
      .toEqual(["parent", "other"]);
    expanded.add("parent");
    expect(projectHierarchy(family, { expandedTaskIds: expanded }).details.get("grandchild")?.depth).toBe(2);
  });

  it("preserves ancestors and automatically reveals matching descendants while filtering", () => {
    const projected = projectHierarchy(family, {
      matchingTaskIds: new Set(["grandchild"]), filtering: true,
    });
    expect(projected.tasks.map((t) => t.id)).toEqual(["parent", "child", "grandchild"]);
    expect(projected.details.get("parent")).toMatchObject({ autoExpanded: true, contextOnly: true });
    expect(projected.details.get("grandchild")?.contextOnly).toBe(false);
    expect(projected.details.get("parent")?.descendantCount).toBe(3);
    expect(projectHierarchy(family).tasks.map((t) => t.id)).toEqual(["parent", "other"]);
  });

  it("does not force expansion for the completed toggle alone", () => {
    const projected = projectHierarchy(family, {
      matchingTaskIds: new Set(["grandchild", "sibling"]), filtering: false,
    });
    expect(projected.tasks.map((t) => t.id)).toEqual(["parent"]);
    expect(projected.details.get("parent")?.expanded).toBe(false);
    expect(projectHierarchy(family, { matchingTaskIds: new Set(), filtering: true }).tasks).toEqual([]);
  });

  it("remaps collapsed dependency endpoints, combines duplicate relations, and omits self-edges", () => {
    const projected = projectHierarchy(graph(
      [task("a"), task("a1"), task("a2"), task("b"), task("b1")],
      [
        edge("a1", "a"), edge("a2", "a"), edge("b1", "b"),
        edge("b1", "a1", "blocks"), edge("b1", "a2", "blocks"),
        edge("a2", "a1", "blocks"),
      ],
    ));
    expect(projected.edges).toEqual([
      { from: "b", to: "a", dep_type: "blocks", count: 2, remapped: true },
    ]);
  });

  it("uses only canonical parent-child edges for hierarchy and tolerates missing endpoints or cycles", () => {
    const projected = projectHierarchy(graph(
      [task("a"), task("b"), task("c"), task("orphan")],
      [
        edge("b", "a"), edge("a", "b"), edge("c", "a", "related"),
        edge("orphan", "unloaded"), edge("a", "missing", "blocks"),
      ],
    ));
    expect(projected.tasks.map((t) => t.id)).toEqual(["a", "c", "orphan"]);
    expect(projected.edges.some((e) => e.to === "missing")).toBe(false);
  });

  it("keeps existing task order and appends arrivals without sorting on changing metadata", () => {
    expect(retainTaskOrder(["a", "b", "deleted"], [task("new"), task("b"), task("a")]))
      .toEqual(["a", "b", "new"]);
  });
});
