import { useNodes, ViewportPortal } from "@xyflow/react";
import { NODE_WIDTH, type GraphWorker } from "./types";

interface Props {
  agents: GraphWorker[];
  visibleTaskById?: ReadonlyMap<string, string>;
}

/** Flow-space positioning follows pan/zoom without adding the browser's
 *  page offset twice. Workers on collapsed descendants dock at their parent. */
export default function AgentAvatarLayer({ agents, visibleTaskById }: Props) {
  const nodes = useNodes();
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const docked = new Map<string, GraphWorker[]>();
  for (const agent of agents) {
    if (!agent.current_task_id) continue;
    const target = visibleTaskById ? visibleTaskById.get(agent.current_task_id) : agent.current_task_id;
    if (target && nodesById.has(target)) docked.set(target, [...(docked.get(target) ?? []), agent]);
  }

  return (
    <ViewportPortal>
      {[...docked].map(([id, workers]) => {
        const node = nodesById.get(id)!;
        const names = workers.map((worker) => worker.name).join(", ");
        const inCollapsed = workers.some((worker) => worker.in_collapsed || worker.current_task_id !== id);
        const label = `${names}${inCollapsed ? " (working in collapsed tasks)" : ""}`;
        // Container cards are far wider than a task card, and the server-laid
        // out cards sit at zIndex 100+depth, so the badge needs its own band.
        const width = node.width ?? node.measured?.width ?? NODE_WIDTH;
        return (
          <div
            key={id}
            role="img"
            aria-label={label}
            title={label}
            className="pointer-events-none absolute flex items-center gap-1 rounded-full border-2 border-white bg-indigo-500 px-1.5 py-0.5 text-[10px] font-bold text-white shadow"
            style={{ left: node.position.x + width - 20, top: node.position.y - 12, zIndex: 1000 }}
          >
            {workers[0]!.name.slice(0, 2).toUpperCase()}
            {workers.length > 1 && <span>+{workers.length - 1}</span>}
          </div>
        );
      })}
    </ViewportPortal>
  );
}
