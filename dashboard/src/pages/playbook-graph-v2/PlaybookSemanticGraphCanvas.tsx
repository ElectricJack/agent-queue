import {
  useCallback,
  useMemo,
  useRef,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { Background, Controls, Panel, ReactFlow, ReactFlowProvider } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import RuleClusterNode from "./RuleClusterNode";
import SemanticStepNode from "./StepNodeCard";
import { layoutSemanticGraph, type SemanticGraphInput } from "./layout";
import {
  EDGE_KIND_LABELS,
  EDGE_KIND_STYLES,
  NEUTRAL_EDGE_STYLE,
  RULE_CLUSTER_NODE_TYPE,
  SEMANTIC_NODE_TYPE,
} from "./types";

const nodeTypes = {
  [SEMANTIC_NODE_TYPE]: SemanticStepNode,
  [RULE_CLUSTER_NODE_TYPE]: RuleClusterNode,
};

export interface PlaybookSemanticGraphCanvasProps {
  graph: SemanticGraphInput | undefined;
  selectedNodeId?: string | null;
  /** Called with a step id on activation, and with `null` when selection clears. */
  onSelectNode: (nodeId: string | null) => void;
}

/** Read-only canvas for one playbook artifact.
 *
 *  Camera state belongs entirely to React Flow: the graph fits once on mount
 *  and a selection change never re-fits, because only `selected` changes on the
 *  node objects. Changing the event scope re-mounts nothing either — the node
 *  list changes, and React Flow keeps the viewport. */
export default function PlaybookSemanticGraphCanvas({
  graph,
  selectedNodeId = null,
  onSelectNode,
}: PlaybookSemanticGraphCanvasProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const { nodes, edges, droppedEdgeCount } = useMemo(() => layoutSemanticGraph(graph), [graph]);

  const select = useCallback((nodeId: string) => onSelectNode(nodeId), [onSelectNode]);
  /** React Flow only gives a node wrapper `pointer-events: all` when the node
   *  is selectable, draggable, or the flow has a node pointer handler. This
   *  canvas is none of the first two on purpose, so without `onNodeClick` every
   *  card would sit under `pointer-events: none`. Cluster nodes are not
   *  selectable, so a click on the cluster chrome clears instead of selecting. */
  const onNodeClick = useCallback(
    (_event: ReactMouseEvent, node: { id: string; type?: string }) =>
      onSelectNode(node.type === RULE_CLUSTER_NODE_TYPE ? null : node.id),
    [onSelectNode],
  );
  const clearSelection = useCallback(() => onSelectNode(null), [onSelectNode]);

  const decorated = useMemo(
    () =>
      nodes.map((node) =>
        node.type === RULE_CLUSTER_NODE_TYPE
          ? node
          : {
              ...node,
              selected: node.id === selectedNodeId,
              data: { ...node.data, onSelect: select },
            },
      ),
    [nodes, selectedNodeId, select],
  );

  const edgeKinds = useMemo(
    () => [...new Set(edges.map((edge) => String(edge.data?.edgeKind ?? "unknown")))],
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
      aria-label="Playbook semantic graph"
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
          minZoom={0.1}
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
          onNodeClick={onNodeClick}
          onPaneClick={clearSelection}
        >
          <Background gap={24} color="#1f2937" />
          <Controls position="bottom-right" showInteractive={false} />
          {edgeKinds.length > 0 && (
            <Panel position="bottom-left">
              <ul
                aria-label="Edge kinds"
                className="rounded border border-gray-700 bg-gray-950/95 px-3 py-2 text-[10px] text-gray-300"
              >
                {edgeKinds.map((kind) => (
                  <li key={kind} className="flex items-center gap-2">
                    <svg aria-hidden width="30" height="10">
                      <path
                        d="M0 5h26m-4-3 4 3-4 3"
                        fill="none"
                        style={EDGE_KIND_STYLES[kind] ?? NEUTRAL_EDGE_STYLE}
                      />
                    </svg>
                    {EDGE_KIND_LABELS[kind] ?? "transition"}
                  </li>
                ))}
              </ul>
            </Panel>
          )}
        </ReactFlow>
      </ReactFlowProvider>
      {nodes.length === 0 && (
        <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-gray-400">
          No rules match this event scope.
        </p>
      )}
      {droppedEdgeCount > 0 && (
        <p
          role="status"
          className="absolute left-2 top-2 rounded border border-amber-600 bg-amber-950/90 px-2 py-1 text-[10px] text-amber-100"
        >
          Incomplete graph data: {droppedEdgeCount} transition
          {droppedEdgeCount === 1 ? "" : "s"} reference a step that is not in this projection and{" "}
          {droppedEdgeCount === 1 ? "was" : "were"} not drawn.
        </p>
      )}
    </div>
  );
}
