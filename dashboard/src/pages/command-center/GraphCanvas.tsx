import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  Background, Controls, Panel, ReactFlow, ReactFlowProvider, type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import TaskNode from "./TaskNode";
import AgentAvatarLayer from "./AgentAvatarLayer";
import { columnsForWidth, edgeStyleForType, layoutGraph } from "./layout";
import { useGraphHierarchy } from "./useGraphHierarchy";
import type { GraphViewProps, TaskNodeData } from "./types";

const nodeTypes = { task: TaskNode };
const initialViewport = { x: 0, y: 0, zoom: 1 };
const RELATION_LABELS: Record<string, string> = {
  blocks: "blocks",
  "parent-child": "parent-child",
  "waits-for": "waits-for",
  "conditional-blocks": "conditional-blocks",
  "discovered-from": "discovered-from",
};

function nearestIn(nodes: Node[], from: Node, dir: "up" | "down" | "left" | "right"): Node | null {
  let best: Node | null = null;
  let bestScore = Infinity;
  for (const node of nodes) {
    if (node.id === from.id) continue;
    const dx = node.position.x - from.position.x;
    const dy = node.position.y - from.position.y;
    const primary = dir === "up" ? -dy : dir === "down" ? dy : dir === "right" ? dx : -dx;
    if (primary <= 0) continue;
    const secondary = dir === "up" || dir === "down" ? Math.abs(dx) : Math.abs(dy);
    const score = primary + secondary * 2;
    if (score < bestScore) { bestScore = score; best = node; }
  }
  return best;
}

export default function GraphCanvas(props: GraphViewProps) {
  const { graph, onTaskClick, onBackgroundClick, selectedTaskId } = props;
  const { projection, toggleExpanded } = useGraphHierarchy(props);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [canvasWidth, setCanvasWidth] = useState(0);
  const [localSelectedId, setLocalSelectedId] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const selectedId = selectedTaskId === undefined ? localSelectedId : selectedTaskId;

  useEffect(() => {
    if (selectedTaskId !== undefined) setFocusId(selectedTaskId);
  }, [selectedTaskId]);

  useEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    setCanvasWidth(element.clientWidth);
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setCanvasWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const columns = columnsForWidth(canvasWidth);
  const { nodes, edges } = useMemo(
    () => layoutGraph(graph, { projection, columns }),
    [graph, projection, columns],
  );

  const openTask = useCallback((id: string) => {
    setFocusId(id);
    setLocalSelectedId(id);
    onTaskClick(id);
  }, [onTaskClick]);
  const clearSelection = useCallback(() => {
    setFocusId(null);
    setLocalSelectedId(null);
    onBackgroundClick?.();
  }, [onBackgroundClick]);

  const decorated = useMemo(() => nodes.map((node) => ({
    ...node,
    selected: node.id === selectedId,
    className: node.id === focusId ? "aq-focused" : undefined,
    data: { ...node.data, onOpenTask: openTask, onToggleChildren: toggleExpanded },
  })), [nodes, selectedId, focusId, openTask, toggleExpanded]);
  const relationTypes = useMemo(
    () => [...new Set(edges.map((edge) => String(edge.data?.depType)))].sort(),
    [edges],
  );

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, select, [contenteditable=true]")) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      clearSelection();
      wrapRef.current?.focus({ preventScroll: true });
      return;
    }
    const taskButton = target.closest<HTMLButtonElement>("button[data-task-id]");
    if (target.closest("button, a, summary") && !taskButton) return;
    const fromId = taskButton?.dataset.taskId ?? focusId ?? selectedId;
    const from = nodes.find((node) => node.id === fromId) ?? nodes[0];
    if (!from) return;
    const directions: Record<string, "up" | "down" | "left" | "right"> = {
      ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    };
    const dir = directions[event.key];
    if (dir) {
      const next = nearestIn(nodes, from, dir);
      event.preventDefault();
      if (next) {
        setFocusId(next.id);
        const button = [...(wrapRef.current?.querySelectorAll<HTMLButtonElement>("button[data-task-id]") ?? [])]
          .find((element) => element.dataset.taskId === next.id);
        button?.focus({ preventScroll: true });
      }
    } else if (!taskButton && (event.key === "Enter" || event.key === "o")) {
      event.preventDefault();
      openTask(from.id);
    }
  }

  return (
    <div ref={wrapRef} role="region" aria-label="Task graph" tabIndex={0} onKeyDown={onKeyDown} className="relative h-full min-h-0 w-full outline-none">
      <ReactFlowProvider>
        <ReactFlow
          nodes={decorated}
          edges={edges}
          nodeTypes={nodeTypes}
          colorMode="dark"
          defaultViewport={initialViewport}
          minZoom={0.15}
          maxZoom={2}
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
          elementsSelectable={false}
          deleteKeyCode={null}
          selectionKeyCode={null}
          disableKeyboardA11y
          nodeClickDistance={5}
          panOnScroll
          zoomOnScroll={false}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, node: Node<TaskNodeData>) => openTask(node.id)}
          onPaneClick={clearSelection}
        >
          <Background gap={24} color="#1f2937" />
          <Controls position="bottom-right" showInteractive={false} />
          <AgentAvatarLayer agents={graph.agents} visibleTaskById={projection.visibleTaskById} />
          {relationTypes.length > 0 && (
            <Panel position="bottom-left">
              <details className="max-w-xs rounded border border-gray-700 bg-gray-950/95 px-3 py-2 text-[10px] text-gray-300">
                <summary className="cursor-pointer">Dependencies · arrows point to dependent tasks</summary>
                <ul className="mt-2 space-y-1">
                  {relationTypes.map((type) => (
                    <li key={type} className="flex items-center gap-2">
                      <svg aria-hidden width="28" height="10">
                        <path d="M0 5h25m-4-3 4 3-4 3" fill="none" style={edgeStyleForType(type)} />
                      </svg>
                      {RELATION_LABELS[type] ?? type}
                    </li>
                  ))}
                </ul>
                <p className="mt-2 text-gray-500">Parent/origin → child. ×N combines links from collapsed tasks.</p>
              </details>
            </Panel>
          )}
        </ReactFlow>
      </ReactFlowProvider>
      {nodes.length === 0 && <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-gray-500">No tasks match these filters.</p>}
    </div>
  );
}
