import { useMemo } from "react";
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

export default function GraphCanvas({ graph, onTaskClick }: Props) {
  const { nodes, edges } = useMemo(() => layoutGraph(graph), [graph]);

  return (
    <div className="h-full w-full">
      <ReactFlowProvider>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.15}
          maxZoom={2.5}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, n: Node) => onTaskClick(n.id)}
        >
          <Background gap={24} color="#1f2937" />
          <AgentAvatarLayer agents={graph.agents} />
        </ReactFlow>
      </ReactFlowProvider>
    </div>
  );
}
