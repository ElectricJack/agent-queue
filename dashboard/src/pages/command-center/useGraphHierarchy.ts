import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
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

/** Expand/collapse is the user's state, not the viewport's.
 *
 *  The set changes only through `toggleExpanded` (the container toggle on a
 *  task card) and is persisted so the choice survives zooms, live graph
 *  refreshes, tab switches and reloads. Zoom, pan and resize deliberately
 *  reach none of this: a viewport that is too small or too busy to draw every
 *  child should reduce visual detail, never restructure the graph. Semantic
 *  zoom, if it is ever wanted, has to arrive as an explicit opt-in toggle that
 *  defaults to off. `__tests__/expandedState.test.tsx` guards this.
 *
 *  The one non-click change is filter-driven: an active filter temporarily
 *  auto-expands the ancestors of a match so it can be seen, flagged as
 *  `autoExpanded` and reverted when the filter clears. It never writes to the
 *  persisted set. The expanded-task set is shared by both canvases through
 *  one storage key. */
// One live set, not one per consumer: the canvas toggles it and the toolbar
// has to send the SAME set to `locate`, or a jump would be computed against a
// layout nobody is looking at. Storage alone only synchronised them on mount.
let expandedIds: ReadonlySet<string> = readExpandedTaskIds();
const expandedListeners = new Set<() => void>();

/** Replace the expanded set outright (persisted, and broadcast to every consumer). */
export function setExpandedTaskIds(next: ReadonlySet<string>) {
  expandedIds = next;
  persistExpandedTaskIds(next);
  for (const listener of [...expandedListeners]) listener();
}

function toggleExpandedId(id: string) {
  const next = new Set(expandedIds);
  if (!next.delete(id)) next.add(id);
  setExpandedTaskIds(next);
}

export function useExpandedTaskIds() {
  const expandedTaskIds = useSyncExternalStore(
    (listener) => { expandedListeners.add(listener); return () => expandedListeners.delete(listener); },
    () => expandedIds,
  );
  const toggleExpanded = useCallback((id: string) => toggleExpandedId(id), []);
  return { expandedTaskIds, toggleExpanded };
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
