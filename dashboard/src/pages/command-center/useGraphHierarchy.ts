import { useCallback, useEffect, useMemo, useState } from "react";
import { projectHierarchy, retainTaskOrder } from "./hierarchy";
import type { GraphViewProps } from "./types";

const EXPANDED_TASKS_KEY = "aq:command-center:expanded-task-ids:v1";

function readExpandedTaskIds(): ReadonlySet<string> {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(EXPANDED_TASKS_KEY) ?? "[]");
    if (!Array.isArray(stored)) return new Set();
    return new Set(stored.filter((id): id is string => typeof id === "string"));
  } catch {
    return new Set();
  }
}

function persistExpandedTaskIds(ids: ReadonlySet<string>) {
  try {
    localStorage.setItem(EXPANDED_TASKS_KEY, JSON.stringify([...ids]));
  } catch {
    // The graph remains usable when storage is disabled or full.
  }
}

export function useGraphHierarchy({
  graph, matchingTaskIds, filtering,
}: Pick<GraphViewProps, "graph" | "matchingTaskIds" | "filtering">) {
  const [expandedTaskIds, setExpandedTaskIds] = useState<ReadonlySet<string>>(readExpandedTaskIds);
  const [knownOrder, setKnownOrder] = useState<string[]>(() => graph.tasks.map((task) => task.id));
  const order = useMemo(() => retainTaskOrder(knownOrder, graph.tasks), [knownOrder, graph.tasks]);

  useEffect(() => {
    if (order.length !== knownOrder.length || order.some((id, i) => id !== knownOrder[i])) {
      setKnownOrder(order);
    }
  }, [order, knownOrder]);

  useEffect(() => persistExpandedTaskIds(expandedTaskIds), [expandedTaskIds]);

  const projection = useMemo(
    () => projectHierarchy(graph, { expandedTaskIds, matchingTaskIds, filtering, orderedTaskIds: order }),
    [graph, expandedTaskIds, matchingTaskIds, filtering, order],
  );
  const toggleExpanded = useCallback((id: string) => {
    setExpandedTaskIds((previous) => {
      const next = new Set(previous);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }, []);

  return { projection, toggleExpanded };
}
