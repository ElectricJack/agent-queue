import type { GraphEdge, GraphTaskNode, MergedGraph, TaskHierarchy } from "./types";

const INACTIVE_STATUSES = new Set(["COMPLETED", "FAILED", "CANCELED", "CANCELLED", "SKIPPED"]);

/** Completed/failed rows can retain a stale projection flag; their lifecycle state wins. */
export function isTaskBlocked(task: GraphTaskNode): boolean {
  return !INACTIVE_STATUSES.has(task.status) && Boolean(task.is_blocked || task.status === "BLOCKED");
}

export interface HierarchyOptions {
  expandedTaskIds?: ReadonlySet<string>;
  matchingTaskIds?: ReadonlySet<string>;
  filtering?: boolean;
  orderedTaskIds?: readonly string[];
}

export interface ProjectedEdge extends GraphEdge {
  count: number;
  remapped: boolean;
}

export interface HierarchyProjection {
  tasks: GraphTaskNode[];
  edges: ProjectedEdge[];
  details: Map<string, TaskHierarchy>;
  /** Includes collapsed descendants, but excludes tasks removed by filters. */
  visibleTaskById: Map<string, string>;
}

export function retainTaskOrder(previous: readonly string[], tasks: GraphTaskNode[]): string[] {
  const remaining = new Set(tasks.map((task) => task.id));
  const ordered = previous.filter((id) => remaining.delete(id));
  for (const task of tasks) {
    if (remaining.delete(task.id)) ordered.push(task.id);
  }
  return ordered;
}

/** Parent-child edges are stored child -> parent. Other edge types never
 *  establish containment, even when they point to the same task. */
export function projectHierarchy(
  graph: MergedGraph,
  options: HierarchyOptions = {},
): HierarchyProjection {
  const taskById = new Map(graph.tasks.map((task) => [task.id, task]));
  const order = retainTaskOrder(options.orderedTaskIds ?? [], graph.tasks);
  const parents = new Map<string, string>();
  for (const edge of graph.edges) {
    if (edge.dep_type !== "parent-child" || parents.has(edge.from)
      || !taskById.has(edge.from) || !taskById.has(edge.to)) continue;
    let ancestor: string | undefined = edge.to;
    const seen = new Set([edge.from]);
    while (ancestor && !seen.has(ancestor)) {
      seen.add(ancestor);
      ancestor = parents.get(ancestor);
    }
    // The backend rejects cycles; tolerate partial or malformed snapshots too.
    if (!ancestor) parents.set(edge.from, edge.to);
  }

  const children = new Map<string, string[]>();
  for (const id of order) {
    const parent = parents.get(id);
    if (parent) children.set(parent, [...(children.get(parent) ?? []), id]);
  }

  const eligible = new Set<string>();
  const autoExpanded = new Set<string>();
  for (const id of order) {
    if (options.matchingTaskIds && !options.matchingTaskIds.has(id)) continue;
    eligible.add(id);
    let ancestor = parents.get(id);
    while (ancestor) {
      eligible.add(ancestor);
      if (options.filtering) autoExpanded.add(ancestor);
      ancestor = parents.get(ancestor);
    }
  }

  type Counts = Pick<TaskHierarchy, "descendantCount" | "completedCount" | "runningCount" | "blockedCount">;
  const counts = new Map<string, Counts>();
  function countChildren(id: string): Counts {
    const existing = counts.get(id);
    if (existing) return existing;
    const result = { descendantCount: 0, completedCount: 0, runningCount: 0, blockedCount: 0 };
    for (const childId of children.get(id) ?? []) {
      const child = taskById.get(childId)!;
      const nested = countChildren(childId);
      result.descendantCount += 1 + nested.descendantCount;
      result.completedCount += Number(child.status === "COMPLETED") + nested.completedCount;
      result.runningCount += Number(child.status === "IN_PROGRESS") + nested.runningCount;
      result.blockedCount += Number(isTaskBlocked(child)) + nested.blockedCount;
    }
    counts.set(id, result);
    return result;
  }

  const tasks: GraphTaskNode[] = [];
  const details = new Map<string, TaskHierarchy>();
  function visit(id: string, depth: number) {
    if (!eligible.has(id)) return;
    const task = taskById.get(id)!;
    const childIds = children.get(id) ?? [];
    const parentId = parents.get(id) ?? null;
    const expanded = autoExpanded.has(id) || Boolean(options.expandedTaskIds?.has(id));
    tasks.push(task);
    details.set(id, {
      parentId,
      parentTitle: parentId ? taskById.get(parentId)?.title ?? null : null,
      depth,
      childCount: childIds.length,
      visibleChildCount: childIds.filter((childId) => eligible.has(childId)).length,
      ...countChildren(id),
      expanded,
      autoExpanded: autoExpanded.has(id),
      contextOnly: Boolean(options.matchingTaskIds && !options.matchingTaskIds.has(id)),
    });
    if (expanded) for (const childId of childIds) visit(childId, depth + 1);
  }
  for (const id of order) if (!parents.has(id)) visit(id, 0);

  const visible = new Set(tasks.map((task) => task.id));
  const visibleTaskById = new Map<string, string>();
  for (const id of eligible) {
    let ancestor: string | undefined = id;
    while (ancestor && !visible.has(ancestor)) ancestor = parents.get(ancestor);
    if (ancestor) visibleTaskById.set(id, ancestor);
  }

  const projectedEdges = new Map<string, ProjectedEdge>();
  for (const edge of graph.edges) {
    const from = visibleTaskById.get(edge.from);
    const to = visibleTaskById.get(edge.to);
    if (!from || !to || from === to) continue;
    const key = JSON.stringify([from, to, edge.dep_type]);
    const previous = projectedEdges.get(key);
    if (previous) {
      previous.count += 1;
      previous.remapped ||= from !== edge.from || to !== edge.to;
      previous.description ??= edge.description;
    } else {
      projectedEdges.set(key, {
        from, to, dep_type: edge.dep_type, count: 1,
        remapped: from !== edge.from || to !== edge.to,
        description: edge.description,
      });
    }
  }

  return { tasks, edges: [...projectedEdges.values()], details, visibleTaskById };
}
