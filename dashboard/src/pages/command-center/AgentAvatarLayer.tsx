import { useReactFlow, useStore } from "@xyflow/react";
import { NODE_WIDTH, type GraphAgent } from "./types";

interface Props {
  agents: GraphAgent[];
}

/** Docks each agent avatar to its current task node in screen space.
 *  On current_task_id change, the div's `transform` transitions to the new
 *  projected coordinate — CSS handles the motion. */
export default function AgentAvatarLayer({ agents }: Props) {
  const rf = useReactFlow();
  // Subscribing to the store's transform already triggers a rerender on
  // every pan/zoom, which recomputes screen coords below from the fresh
  // transform — no separate RAF/force-render mechanism needed.
  useStore((s) => s.transform);

  return (
    <div className="pointer-events-none absolute inset-0 z-30">
      {agents.map((a) => {
        if (!a.current_task_id) return null;
        const node = rf.getNode(a.current_task_id);
        if (!node) return null;
        const screen = rf.flowToScreenPosition({
          x: node.position.x + NODE_WIDTH - 8, // NODE_WIDTH - inset
          y: node.position.y - 8,
        });
        return (
          <div
            key={a.id}
            className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-indigo-500 px-1 text-[10px] font-bold text-white shadow"
            style={{
              left: 0,
              top: 0,
              transform: `translate(${screen.x}px, ${screen.y}px)`,
              transition: "transform 600ms cubic-bezier(0.4, 0, 0.2, 1)",
            }}
            title={`${a.name} — ${a.profile_id ?? ""}`}
          >
            {a.name.slice(0, 2).toUpperCase()}
          </div>
        );
      })}
    </div>
  );
}
