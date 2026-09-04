import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  Background,
  Controls,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  type EdgeChange,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import RuleClusterNode from "./RuleClusterNode";
import SemanticStepNode from "./StepNodeCard";
import { layoutSemanticGraph, toGrid, type SemanticGraphInput } from "./layout";
import {
  EDGE_KIND_LABELS,
  EDGE_KIND_STYLES,
  NEUTRAL_EDGE_STYLE,
  RULE_CLUSTER_NODE_TYPE,
  SEMANTIC_NODE_TYPE,
  TRAVERSED_EDGE_WIDTH,
  UNTRAVERSED_EDGE_OPACITY,
  selectedEdgeStyle,
  type RunOverlayInput,
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
  /** Run state to draw over the graph. It is drawn only when the run pinned
   *  this exact artifact; otherwise the canvas says why it is not. */
  overlay?: RunOverlayInput;
  onSaveLayout?: (positions: Record<string, { x: number; y: number }>) => void;
}

/** Interactive canvas for one playbook artifact.
 *
 *  Camera state belongs entirely to React Flow: the graph fits once on mount
 *  and a selection change never re-fits, because only `selected` changes on the
 *  node objects. Changing the event scope re-mounts nothing either — the node
 *  list changes, and React Flow keeps the viewport. */
export default function PlaybookSemanticGraphCanvas({
  graph,
  selectedNodeId = null,
  onSelectNode,
  overlay,
  onSaveLayout,
}: PlaybookSemanticGraphCanvasProps) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const { nodes, edges, droppedEdgeCount, overlayApplied, overlayMismatch } = useMemo(
    () => layoutSemanticGraph(graph, overlay),
    [graph, overlay],
  );
  const [positionedNodes, setPositionedNodes] = useState(nodes);
  useEffect(() => setPositionedNodes(nodes), [nodes]);
  const onNodesChange = useCallback(
    (changes: NodeChange[]) =>
      setPositionedNodes(
        (current) => applyNodeChanges(changes, current) as typeof current,
      ),
    [],
  );
  const onNodeDragStop = useCallback(
    (_event: MouseEvent | TouchEvent, dragged: Node) => {
      if (!onSaveLayout || dragged.type !== SEMANTIC_NODE_TYPE) return;
      const parent = positionedNodes.find((node) => node.id === dragged.parentId);
      const absolute = {
        x: dragged.position.x + (parent?.position.x ?? 0),
        y: dragged.position.y + (parent?.position.y ?? 0),
      };
      onSaveLayout({ [dragged.id]: toGrid(absolute) });
    },
    [onSaveLayout, positionedNodes],
  );

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

  /** Edge selection lives here rather than in the view: nothing outside the
   *  canvas consumes it, and React Flow will not keep it on its own — the
   *  `edges` prop is controlled, so a click's `select` change is dropped unless
   *  `onEdgesChange` records it. */
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const clearSelection = useCallback(() => {
    setSelectedEdgeId(null);
    onSelectNode(null);
  }, [onSelectNode]);

  /** One selection at a time, the way React Flow itself treats it: picking an
   *  edge (by click or by Enter on a focused one) drops the step selection, and
   *  the effect below does the reverse for every path that picks a step —
   *  card, pane, and the diagnostics banner. */
  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      for (const change of changes) {
        if (change.type !== "select") continue;
        if (change.selected) {
          setSelectedEdgeId(change.id);
          onSelectNode(null);
        } else {
          setSelectedEdgeId((current) => (current === change.id ? null : current));
        }
      }
    },
    [onSelectNode],
  );
  useEffect(() => {
    if (selectedNodeId !== null) setSelectedEdgeId(null);
  }, [selectedNodeId]);
  // Narrowing the event scope can drop the selected edge; don't keep a stale id
  // that would light the edge up again when the scope widens.
  useEffect(() => {
    setSelectedEdgeId((current) =>
      current !== null && !edges.some((edge) => edge.id === current) ? null : current,
    );
  }, [edges]);

  const decorated = useMemo(
    () =>
      positionedNodes.map((node) =>
        node.type === RULE_CLUSTER_NODE_TYPE
          ? node
          : {
              ...node,
              draggable: Boolean(onSaveLayout),
              selected: node.id === selectedNodeId,
              data: { ...node.data, onSelect: select },
            },
      ),
    [positionedNodes, selectedNodeId, select, onSaveLayout],
  );

  const decoratedEdges = useMemo(
    () =>
      edges.map((edge) => {
        const selected = edge.id === selectedEdgeId;
        return {
          ...edge,
          selected,
          // `aria-pressed` on the button role React Flow gives a focusable edge:
          // a screen reader announces which transition is picked, not just that
          // one can be.
          domAttributes: { ...edge.domAttributes, "aria-pressed": selected },
          ...(selected
            ? { style: selectedEdgeStyle(edge.style ?? {}), zIndex: (edge.zIndex ?? 0) + 1 }
            : {}),
        };
      }),
    [edges, selectedEdgeId],
  );

  const edgeKinds = useMemo(
    () => [...new Set(edges.map((edge) => String(edge.data?.edgeKind ?? "unknown")))],
    [edges],
  );

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    // Escape from anywhere in the region clears both selections, including one
    // made on an edge that currently holds focus.
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
          edges={decoratedEdges}
          nodeTypes={nodeTypes}
          colorMode="dark"
          fitView
          fitViewOptions={{ padding: 0.2 }}
          minZoom={0.1}
          maxZoom={2}
          nodesDraggable={Boolean(onSaveLayout)}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable
          edgesReconnectable={false}
          // Off so drag-select never competes with panning. Edges opt in per element (see
          // `layout.ts`), which is what xyflow's `edge.selectable` is for.
          elementsSelectable={false}
          deleteKeyCode={null}
          selectionKeyCode={null}
          nodeClickDistance={5}
          panOnScroll
          zoomOnScroll={false}
          proOptions={{ hideAttribution: true }}
          onNodeClick={onNodeClick}
          onNodesChange={onNodesChange}
          onNodeDragStop={onNodeDragStop}
          onEdgesChange={onEdgesChange}
          onPaneClick={clearSelection}
        >
          <Background gap={24} color="#1f2937" />
          <Controls position="bottom-right" showInteractive={false} />
          {overlayApplied && (
            <Panel position="top-right">
              <section
                aria-label="Run overlay"
                className="rounded border border-gray-700 bg-gray-950/95 px-3 py-2 text-[10px] text-gray-300"
              >
                <dl>
                <div className="flex items-center gap-2">
                  <dt className="text-gray-500">run</dt>
                  <dd className="font-mono text-gray-200">{overlay?.run_id}</dd>
                </div>
                <div className="flex items-center gap-2">
                  <dt className="text-gray-500">lifecycle</dt>
                  <dd className="text-gray-200">{overlay?.lifecycle}</dd>
                </div>
                <div className="flex items-center gap-2">
                  <dt className="text-gray-500">artifact</dt>
                  <dd className="text-gray-200">
                    {overlay?.artifact_is_active ? "active" : "older than the active one"}
                  </dd>
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <dt className="sr-only">key</dt>
                  <dd className="flex items-center gap-2">
                    <svg aria-hidden width="30" height="10">
                      <path
                        d="M0 5h26m-4-3 4 3-4 3"
                        fill="none"
                        stroke="#cbd5e1"
                        strokeWidth={TRAVERSED_EDGE_WIDTH}
                      />
                    </svg>
                    traversed
                  </dd>
                </div>
                <div className="flex items-center gap-2">
                  <dt className="sr-only">key</dt>
                  <dd className="flex items-center gap-2">
                    <svg aria-hidden width="30" height="10">
                      <path
                        d="M0 5h26m-4-3 4 3-4 3"
                        fill="none"
                        stroke="#cbd5e1"
                        strokeWidth={1.5}
                        strokeOpacity={UNTRAVERSED_EDGE_OPACITY}
                      />
                    </svg>
                    not traversed
                  </dd>
                </div>
                </dl>
              </section>
            </Panel>
          )}
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
      {overlayMismatch && (
        <p
          role="status"
          className="absolute right-2 top-2 max-w-[22rem] rounded border border-amber-600 bg-amber-950/90 px-2 py-1 text-[10px] text-amber-100"
        >
          Run state is not shown: this run executed artifact{" "}
          <span className="font-mono">{overlay?.artifact?.artifact_sha256}</span>, which is not the
          artifact projected here.
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
