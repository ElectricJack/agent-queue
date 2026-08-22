import { useEffect, useRef, useState } from "react";
import { useReactFlow, useStore } from "@xyflow/react";
import type { GraphAgent } from "./types";

interface Props {
  agents: GraphAgent[];
}

/** Docks each agent avatar to its current task node in screen space.
 *  On current_task_id change, the div's `transform` transitions to the new
 *  projected coordinate — CSS handles the motion. */
export default function AgentAvatarLayer({ agents }: Props) {
  const rf = useReactFlow();
  // Re-render on viewport (pan/zoom) changes so avatars follow their node.
  const viewport = useStore((s) => s.transform);
  const [, force] = useState(0);
  const raf = useRef<number | null>(null);
  useEffect(() => {
    // Also re-project after node position updates (layout changes).
    raf.current = requestAnimationFrame(() => force((x) => x + 1));
    return () => {
      if (raf.current) cancelAnimationFrame(raf.current);
    };
  }, [viewport, agents]);

  return (
    <div className="pointer-events-none absolute inset-0 z-30">
      {agents.map((a) => {
        if (!a.current_task_id) return null;
        const node = rf.getNode(a.current_task_id);
        if (!node) return null;
        const screen = rf.flowToScreenPosition({
          x: node.position.x + 220 - 8, // NODE_WIDTH - inset
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
