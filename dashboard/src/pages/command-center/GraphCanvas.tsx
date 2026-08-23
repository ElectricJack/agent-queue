import { useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  ReactFlow,
  ReactFlowProvider,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import TaskNode from "./TaskNode";
import AgentAvatarLayer from "./AgentAvatarLayer";
import { layoutGraph } from "./layout";
import type { MergedGraph } from "./types";

const nodeTypes = { task: TaskNode };

interface Props {
  graph: MergedGraph;
  onTaskClick: (taskId: string) => void;
}

/** Pick the nearest node in the given cardinal direction from `from`. */
function nearestIn(
  nodes: Node[],
  from: Node,
  dir: "up" | "down" | "left" | "right",
): Node | null {
  const fx = from.position.x;
  const fy = from.position.y;
  let best: Node | null = null;
  let bestScore = Infinity;
  for (const n of nodes) {
    if (n.id === from.id) continue;
    const dx = n.position.x - fx;
    const dy = n.position.y - fy;
    const primary = dir === "up" ? -dy : dir === "down" ? dy : dir === "right" ? dx : -dx;
    if (primary <= 0) continue;
    const secondary = dir === "up" || dir === "down" ? Math.abs(dx) : Math.abs(dy);
    // weight secondary axis so we prefer nodes that align with `from`.
    const score = primary + secondary * 2;
    if (score < bestScore) {
      bestScore = score;
      best = n;
    }
  }
  return best;
}

export default function GraphCanvas({ graph, onTaskClick }: Props) {
  const { nodes, edges } = useMemo(() => layoutGraph(graph), [graph]);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);

  useEffect(() => {
    if (!focusId && nodes.length > 0 && nodes[0]) setFocusId(nodes[0].id);
  }, [nodes, focusId]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const onKey = (e: KeyboardEvent) => {
      const from = nodes.find((n) => n.id === focusId) ?? nodes[0];
      if (!from) return;
      let dir: "up" | "down" | "left" | "right" | null = null;
      if (e.key === "ArrowUp") dir = "up";
      else if (e.key === "ArrowDown") dir = "down";
      else if (e.key === "ArrowLeft") dir = "left";
      else if (e.key === "ArrowRight") dir = "right";
      else if (e.key === "Enter" || e.key === "o") {
        e.preventDefault();
        onTaskClick(from.id);
        return;
      } else return;
      const target = nearestIn(nodes, from, dir);
      if (target) {
        e.preventDefault();
        setFocusId(target.id);
      }
    };
    el.addEventListener("keydown", onKey);
    return () => el.removeEventListener("keydown", onKey);
  }, [nodes, focusId, onTaskClick]);

  const decorated = useMemo(
    () =>
      nodes.map((n) =>
        n.id === focusId
          ? { ...n, className: `${n.className ?? ""} aq-focused`.trim() }
          : n,
      ),
    [nodes, focusId],
  );

  return (
    <div ref={wrapRef} tabIndex={0} className="h-full w-full outline-none">
      <ReactFlowProvider>
        <ReactFlow
          nodes={decorated}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.15}
          maxZoom={2.5}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, n: Node) => {
            setFocusId(n.id);
            onTaskClick(n.id);
          }}
        >
          <Background gap={24} color="#1f2937" />
          <AgentAvatarLayer agents={graph.agents} />
        </ReactFlow>
      </ReactFlowProvider>
    </div>
  );
}
