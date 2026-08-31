import { useCallback, useMemo, useRef, type KeyboardEvent } from "react";
import { Background, Controls, Panel, ReactFlow, ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { PlaybookGraphLayout, PlaybookGraphNodesEdges } from "../../api/client";
import PlaybookStepNode from "./PlaybookStepNode";
import { layoutPlaybookGraph } from "./layout";
import { EDGE_KIND_LABELS, EDGE_KIND_STYLES, PLAYBOOK_NODE_TYPE } from "./types";

const nodeTypes = { [PLAYBOOK_NODE_TYPE]: PlaybookStepNode };
const EDGE_KINDS = ["goto", "condition", "otherwise", "timeout"] as const;

export interface PlaybookGraphCanvasProps {
  graph: PlaybookGraphNodesEdges | undefined;
  layout: PlaybookGraphLayout | undefined;
  selectedNodeId?: string | null;
  /** Called with a node id on activation, and with `null` when selection clears. */
  onSelectNode: (nodeId: string | null) => void;
}

/** Read-only canvas for one compiled playbook.
 *
 *  Deliberately separate from the Command Center task graph: a compiled step is
 *  not a task, and neither view should grow the other's vocabulary. Camera state
 *  is left entirely to React Flow — the graph fits once on mount and a selection
 *  change never re-fits, because only `selected` changes on the node objects. */
export default function PlaybookGraphCanvas({
  graph,
  layout,
  selectedNodeId = null,
  onSelectNode,
}: PlaybookGraphCanvasProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const { nodes, edges, droppedEdgeCount } = useMemo(
    () => layoutPlaybookGraph(graph, layout),
    [graph, layout],
  );

  const select = useCallback((nodeId: string) => onSelectNode(nodeId), [onSelectNode]);
  const clearSelection = useCallback(() => onSelectNode(null), [onSelectNode]);

  const decorated = useMemo(
    () => nodes.map((node) => ({
      ...node,
      selected: node.id === selectedNodeId,
      data: { ...node.data, onSelect: select },
    })),
    [nodes, selectedNodeId, select],
  );

  const edgeKinds = useMemo(
    () => EDGE_KINDS.filter((kind) => edges.some((edge) => edge.data?.edgeType === kind)),
    [edges],
  );

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    clearSelection();
    wrapRef.current?.focus({ preventScroll: true });
  }

  return (
    <div
      ref={wrapRef}
      role="region"
      aria-label="Playbook graph"
      tabIndex={0}
      onKeyDown={onKeyDown}
      className="relative h-full min-h-0 w-full outline-none"
    >
      <ReactFlowProvider>
        <ReactFlow
          nodes={decorated}
          edges={edges}
          nodeTypes={nodeTypes}
          colorMode="dark"
          fitView
          fitViewOptions={{ padding: 0.2 }}
          minZoom={0.15}
          maxZoom={2}
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
          edgesReconnectable={false}
          elementsSelectable={false}
          deleteKeyCode={null}
          selectionKeyCode={null}
          disableKeyboardA11y
          nodeClickDistance={5}
          panOnScroll
          zoomOnScroll={false}
          proOptions={{ hideAttribution: true }}
          onPaneClick={clearSelection}
        >
          <Background gap={24} color="#1f2937" />
          <Controls position="bottom-right" showInteractive={false} />
          {edgeKinds.length > 0 && (
            <Panel position="bottom-left">
              <ul className="rounded border border-gray-700 bg-gray-950/95 px-3 py-2 text-[10px] text-gray-300">
                {edgeKinds.map((kind) => (
                  <li key={kind} className="flex items-center gap-2">
                    <svg aria-hidden width="30" height="10">
                      <path d="M0 5h26m-4-3 4 3-4 3" fill="none" style={EDGE_KIND_STYLES[kind]} />
                    </svg>
                    {EDGE_KIND_LABELS[kind]}
                  </li>
                ))}
              </ul>
            </Panel>
          )}
        </ReactFlow>
      </ReactFlowProvider>
      {nodes.length === 0 && (
        <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-gray-400">
          This compiled playbook has no nodes.
        </p>
      )}
      {droppedEdgeCount > 0 && (
        <p role="status" className="absolute left-2 top-2 rounded border border-amber-600 bg-amber-950/90 px-2 py-1 text-[10px] text-amber-100">
          Incomplete graph data: {droppedEdgeCount} edge{droppedEdgeCount === 1 ? "" : "s"} {droppedEdgeCount === 1 ? "references" : "reference"} a missing node and {droppedEdgeCount === 1 ? "was" : "were"} not drawn.
        </p>
      )}
    </div>
  );
}
