import { memo, useCallback, useMemo } from "react";
import { useStore, ViewportPortal, type ReactFlowState } from "@xyflow/react";
import { NODE_WIDTH, type GraphWorker } from "./types";

interface Props {
  agents: GraphWorker[];
  visibleTaskById?: ReadonlyMap<string, string>;
}

interface Dock { id: string; workers: GraphWorker[] }

/**
 * The badges are positioned from the nodes they dock at, but they only need
 * those nodes — reading the whole node array (`useNodes`) re-ran this layer,
 * and its per-render Map of every node, on every store change and every pan
 * frame. The selector below is O(docked agents) and its result is compared as
 * a string, so a store change that did not move a docked card re-renders
 * nothing. Workers on collapsed descendants dock at their parent.
 */
function AgentAvatarLayer({ agents, visibleTaskById }: Props) {
  const docks = useMemo<Dock[]>(() => {
    const byTarget = new Map<string, GraphWorker[]>();
    for (const agent of agents) {
      if (!agent.current_task_id) continue;
      const target = visibleTaskById ? visibleTaskById.get(agent.current_task_id) : agent.current_task_id;
      if (!target) continue;
      const list = byTarget.get(target);
      if (list) list.push(agent); else byTarget.set(target, [agent]);
    }
    return [...byTarget].map(([id, workers]) => ({ id, workers }));
  }, [agents, visibleTaskById]);

  // A string so the default strict-equality comparison is the right one: the
  // selector re-runs on every store change but only re-renders when one of
  // these boxes actually moved.
  const boxes = useStore(useCallback((state: ReactFlowState) => docks.map((dock) => {
    const node = state.nodeLookup.get(dock.id);
    if (!node) return "";
    const width = node.width ?? node.measured?.width ?? NODE_WIDTH;
    return `${node.position.x},${node.position.y},${width}`;
  }).join("|"), [docks]));

  const placed = boxes.split("|");
  return (
    <ViewportPortal>
      {docks.map((dock, i) => {
        const box = placed[i];
        if (!box) return null;
        const [x, y, width] = box.split(",").map(Number) as [number, number, number];
        const { workers } = dock;
        const names = workers.map((worker) => worker.name).join(", ");
        const inCollapsed = workers.some((worker) => worker.in_collapsed || worker.current_task_id !== dock.id);
        const label = `${names}${inCollapsed ? " (working in collapsed tasks)" : ""}`;
        // Container cards are far wider than a task card, and the server-laid
        // out cards sit at zIndex 100+depth, so the badge needs its own band.
        return (
          <div
            key={dock.id}
            role="img"
            aria-label={label}
            title={label}
            className="pointer-events-none absolute flex items-center gap-1 rounded-full border-2 border-white bg-indigo-500 px-1.5 py-0.5 text-[10px] font-bold text-white shadow"
            style={{ left: x + width - 20, top: y - 12, zIndex: 1000 }}
          >
            {workers[0]!.name.slice(0, 2).toUpperCase()}
            {workers.length > 1 && <span>+{workers.length - 1}</span>}
          </div>
        );
      })}
    </ViewportPortal>
  );
}

export default memo(AgentAvatarLayer);
