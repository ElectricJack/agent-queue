import { useCallback, useEffect, useMemo, useState } from "react";
import { projectHierarchy, retainTaskOrder } from "./hierarchy";
import type { GraphViewProps } from "./types";

export function useGraphHierarchy({
  graph, matchingTaskIds, filtering,
}: Pick<GraphViewProps, "graph" | "matchingTaskIds" | "filtering">) {
  const [expandedTaskIds, setExpandedTaskIds] = useState<ReadonlySet<string>>(() => new Set());
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
  const toggleExpanded = useCallback((id: string) => {
    setExpandedTaskIds((previous) => {
      const next = new Set(previous);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }, []);

  return { projection, toggleExpanded };
}
