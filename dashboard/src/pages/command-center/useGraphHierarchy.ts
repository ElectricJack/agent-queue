import { useCallback, useEffect, useMemo, useState } from "react";
import { projectHierarchy, retainTaskOrder } from "./hierarchy";
import type { GraphViewProps } from "./types";

const EXPANDED_TASKS_KEY = "aq:command-center:expanded-task-ids:v1";
// The subset of the above that is finished (COMPLETED / CANCELED / SKIPPED).
// The `active` layout variant carries no rows under a finished container, so
// knowing one is open is what tells the canvas to ask for `all` instead.
const EXPANDED_FINISHED_KEY = "aq:command-center:expanded-finished-task-ids:v1";

function readIds(key: string): ReadonlySet<string> {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(key) ?? "[]");
    if (!Array.isArray(stored)) return new Set();
    return new Set(stored.filter((id): id is string => typeof id === "string"));
  } catch {
    return new Set();
  }
}

function persistIds(key: string, ids: ReadonlySet<string>) {
  try {
    localStorage.setItem(key, JSON.stringify([...ids]));
  } catch {
    // The graph remains usable when storage is disabled or full.
  }
}

/** The expanded-task set is shared by both canvases through one storage key. */
export function useExpandedTaskIds() {
  const [expandedTaskIds, setExpandedTaskIds] = useState<ReadonlySet<string>>(
    () => readIds(EXPANDED_TASKS_KEY),
  );
  const [expandedFinishedIds, setExpandedFinishedIds] = useState<ReadonlySet<string>>(
    () => readIds(EXPANDED_FINISHED_KEY),
  );

  useEffect(() => persistIds(EXPANDED_TASKS_KEY, expandedTaskIds), [expandedTaskIds]);
  useEffect(() => persistIds(EXPANDED_FINISHED_KEY, expandedFinishedIds), [expandedFinishedIds]);

  // `finished` is the toggled node's own state, passed by the card that owns
  // the chevron. A finished container is expandable like any other; the flag
  // only records that this open container's children live in the `all`
  // variant, so the canvas can ask for the variant that has them.
  const toggleExpanded = useCallback((id: string, finished = false) => {
    const opened = !expandedTaskIds.has(id);
    const next = new Set(expandedTaskIds);
    if (opened) next.add(id); else next.delete(id);
    setExpandedTaskIds(next);
    if (opened ? finished : expandedFinishedIds.has(id)) {
      const nextFinished = new Set(expandedFinishedIds);
      if (opened) nextFinished.add(id); else nextFinished.delete(id);
      setExpandedFinishedIds(nextFinished);
    }
  }, [expandedTaskIds, expandedFinishedIds]);

  return { expandedTaskIds, expandedFinishedIds, toggleExpanded };
}

export function useGraphHierarchy({
  graph, matchingTaskIds, filtering,
}: Pick<GraphViewProps, "graph" | "matchingTaskIds" | "filtering">) {
  const { expandedTaskIds, toggleExpanded } = useExpandedTaskIds();
  const [knownOrder, setKnownOrder] = useState<string[]>(() => graph.tasks.map((task) => task.id));
  const order = useMemo(() => retainTaskOrder(knownOrder, graph.tasks), [knownOrder, graph.tasks]);

  useEffect(() => {
    if (order.length !== knownOrder.length || order.some((id, i) => id !== knownOrder[i])) {
      setKnownOrder(order);
    }
  }, [order, knownOrder]);

  const projection = useMemo(
    () => projectHierarchy(graph, { expandedTaskIds, matchingTaskIds, filtering, orderedTaskIds: order }),
    [graph, expandedTaskIds, matchingTaskIds, filtering, order],
  );
  return { projection, toggleExpanded };
}
