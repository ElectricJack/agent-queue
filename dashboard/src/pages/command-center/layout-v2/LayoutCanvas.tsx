import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  Background, Controls, Panel, ReactFlow, ReactFlowProvider, useReactFlow, type Edge, type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import TaskNode from "../TaskNode";
import PlaybookNode from "../PlaybookNode";
import AgentAvatarLayer from "../AgentAvatarLayer";
import ContainerNode from "./ContainerNode";
import Breadcrumbs from "./Breadcrumbs";
import { edgeStyleForType } from "../layout";
import { useExpandedTaskIds } from "../useGraphHierarchy";
import { useLayoutExtents, useLayoutNode, type TilesParams, type Variant } from "../../../api/graphLayout";
import { useLayoutTiles } from "./useLayoutTiles";
import { refetchLayout, registerLayoutRefetch } from "./liveRegistry";
import { toFlowElements, type FlowHandlers } from "./flowNodes";
import { maxDepthForZoom, sizePx, toPx, worldRectFromViewport, type Rect } from "./units";
import {
  NODE_HEIGHT, NODE_WIDTH, type GraphViewProps, type GraphWorker, type SelectableTask, type TaskNodeData,
} from "../types";
import type { TaskFilters } from "../taskFilters";
import type { LocateHit } from "@aq/ts-client";

/** A project band's label: a plain marker, not a card, so it never steals clicks. */
function ProjectHeaderNode({ data }: { data: { label: string } }) {
  return (
    <div className="pointer-events-none whitespace-nowrap text-xs font-semibold uppercase tracking-wide text-gray-400">
      {data.label}
    </div>
  );
}

const nodeTypes = { task: TaskNode, playbook: PlaybookNode, container: ContainerNode, projectHeader: ProjectHeaderNode };
const NO_PLAYBOOKS: NonNullable<GraphViewProps["playbooks"]> = [];
const PROJECT_GAP = 2;
const PLAYBOOKS_PER_ROW = 4;
const initialViewport = { x: 0, y: 0, zoom: 1 };
const RELATION_LABELS: Record<string, string> = {
  blocks: "blocks",
  "parent-child": "parent-child",
  "waits-for": "waits-for",
  "conditional-blocks": "conditional-blocks",
  "discovered-from": "discovered-from",
};

interface Viewport { x: number; y: number; zoom: number }

export interface LayoutCanvasProps extends Pick<GraphViewProps,
  "onTaskClick" | "onBackgroundClick" | "selectedTaskId" | "playbooks" | "selectedPlaybookId" | "onPlaybookClick"> {
  projectIds: string[];
  projectNames: Map<string, string>;
  variant: Variant;
  filters: TaskFilters;
  focusId: string | null;
  setFocus: (id: string | null) => void;
  /** A located match the toolbar asked for; each request is a fresh object. */
  jumpTarget?: LocateHit | null;
}

interface LayerElements {
  nodes: Node[];
  edges: Edge[];
  workers: GraphWorker[];
  pending: boolean;
  loaded: boolean;
}

interface LayerProps {
  projectId: string;
  projectNames: ReadonlyMap<string, string>;
  offsetY: number;
  params: TilesParams;
  viewport: Viewport | null;
  width: number;
  height: number;
  expanded: ReadonlySet<string>;
  handlers: FlowHandlers;
  onBudgetExceeded: () => void;
  onElements: (projectId: string, elements: LayerElements) => void;
}

function nearestIn(nodes: Node[], from: Node, dir: "up" | "down" | "left" | "right"): Node | null {
  let best: Node | null = null;
  let bestScore = Infinity;
  for (const node of nodes) {
    // Project bands are labels, not destinations.
    if (node.id === from.id || node.type === "projectHeader") continue;
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

/**
 * One project's tiles. Rendering nothing keeps the fetch/convert cost of each
 * project isolated: only the layer whose store changed re-runs its conversion.
 */
function ProjectLayer({
  projectId, projectNames, offsetY, params, viewport, width, height, expanded, handlers, onBudgetExceeded, onElements,
}: LayerProps) {
  // Memoised per layer: `useLayoutTiles` re-runs its viewport effect on every
  // new rect identity, so a fresh object per render would refetch needlessly.
  const rect = useMemo<Rect | null>(() => {
    if (!viewport || width === 0) return null;
    const world = worldRectFromViewport(viewport, width, height);
    return { x0: world.x0, y0: world.y0 - offsetY, x1: world.x1, y1: world.y1 - offsetY };
  }, [viewport, width, height, offsetY]);

  const budget = useRef(onBudgetExceeded);
  budget.current = onBudgetExceeded;
  const options = useMemo(() => ({ onBudgetExceeded: () => budget.current() }), []);
  const { store, pending, loaded, refetchVisible } = useLayoutTiles(projectId, params, rect, options);

  useEffect(
    () => registerLayoutRefetch(projectId, refetchVisible),
    [projectId, refetchVisible],
  );

  useEffect(() => {
    const { nodes, edges } = toFlowElements(store, { projectId, offsetY, expanded, handlers, projectNames });
    // Docking is resolved server-side, so a worker's `docked_at` is already a
    // visible node id.
    const workers: GraphWorker[] = store.workers.map((worker) => ({
      id: worker.agent_id, name: worker.name, current_task_id: worker.docked_at,
      in_collapsed: worker.in_collapsed, profile_id: null, session_id: null,
    }));
    onElements(projectId, { nodes, edges, workers, pending, loaded });
  }, [store, pending, loaded, projectId, projectNames, offsetY, expanded, handlers, onElements]);

  return null;
}

function Inner(props: LayoutCanvasProps) {
  const {
    projectIds, projectNames, variant, filters, focusId, setFocus, jumpTarget, onTaskClick, onBackgroundClick,
    selectedTaskId, playbooks = NO_PLAYBOOKS, selectedPlaybookId, onPlaybookClick,
  } = props;
  const { expandedTaskIds, toggleExpanded } = useExpandedTaskIds();
  const { fitBounds } = useReactFlow();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [viewport, setViewport] = useState<Viewport | null>(initialViewport);
  const [depthOverride, setDepthOverride] = useState<number | null>(null);
  const [layers, setLayers] = useState<ReadonlyMap<string, LayerElements>>(new Map());
  const [localSelectedId, setLocalSelectedId] = useState<string | null>(null);
  const [kbFocusId, setKbFocusId] = useState<string | null>(null);
  const frame = useRef<number | null>(null);
  const trailing = useRef<Viewport | null>(null);

  useEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    setSize({ w: element.clientWidth, h: element.clientHeight });
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setSize({ w: entry.contentRect.width, h: entry.contentRect.height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => () => { if (frame.current !== null) cancelAnimationFrame(frame.current); }, []);

  // Leading-edge rAF throttle: the first move of a gesture lands immediately
  // (so the level of detail reacts at once) and the rest coalesce into the
  // trailing frame.
  const onMove = useCallback((_: unknown, next: Viewport) => {
    if (frame.current !== null) { trailing.current = next; return; }
    setViewport(next);
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      if (trailing.current) { setViewport(trailing.current); trailing.current = null; }
    });
  }, []);

  const zoomDepth = maxDepthForZoom(viewport?.zoom ?? 1);
  const maxDepth = depthOverride === null ? zoomDepth : Math.min(depthOverride, zoomDepth ?? Infinity);
  const paramsSignature = `${focusId ?? ""}|${variant}|${filters.query.trim()}|${filters.status}|${[...expandedTaskIds].sort().join(",")}`;
  // A new query means a new node population: the previous budget cut no longer
  // describes it.
  useEffect(() => { setDepthOverride(null); }, [viewport?.zoom, paramsSignature]);
  const onBudgetExceeded = useCallback(
    () => setDepthOverride((current) => Math.max(0, (current ?? zoomDepth ?? 2) - 1)),
    [zoomDepth],
  );

  // Projects stack vertically: each starts below the previous project's extent.
  const extents = useLayoutExtents(projectIds, focusId ? "all" : variant);
  const heights = projectIds.map((_, i) => {
    const extent = extents[i];
    return extent && !("pending" in extent) ? extent.extent_h : 0;
  });
  const heightsKey = heights.join(",");
  const offsets = useMemo(() => {
    const out = new Map<string, number>();
    let y = 0;
    projectIds.forEach((pid, i) => {
      out.set(pid, y);
      y += (heights[i] ?? 0) + (projectIds.length > 1 ? PROJECT_GAP : 0);
    });
    return out;
    // heightsKey is the structural identity of `heights`, which useQueries
    // rebuilds on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectIds, heightsKey]);

  // A layout that was still building answered 202 for its tiles too, and
  // nothing re-fires when the job finishes: ask the layers to load once the
  // extent arrives.
  const pendingExtents = projectIds.map((_, i) => {
    const extent = extents[i];
    return !extent || "pending" in extent;
  }).join(",");
  const previousPendingRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = previousPendingRef.current?.split(",");
    previousPendingRef.current = pendingExtents;
    if (!previous) return;
    pendingExtents.split(",").forEach((value, i) => {
      const pid = projectIds[i];
      if (pid && previous[i] === "true" && value === "false") refetchLayout(pid);
    });
    // projectIds is covered by pendingExtents' positional identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingExtents]);

  const params = useMemo<TilesParams>(() => ({
    variant: focusId ? "all" : variant,
    expanded: [...expandedTaskIds].sort(),
    root: focusId,
    maxDepth: maxDepth === null || maxDepth === Infinity ? null : maxDepth,
    q: filters.query.trim(),
    status: filters.status,
  }), [variant, focusId, expandedTaskIds, maxDepth, filters.query, filters.status]);

  const selectedId = selectedPlaybookId
    ? `playbook:${selectedPlaybookId}`
    : selectedTaskId === undefined ? localSelectedId : selectedTaskId;
  useEffect(() => {
    if (selectedTaskId !== undefined || selectedPlaybookId !== undefined) setKbFocusId(selectedId);
  }, [selectedTaskId, selectedPlaybookId, selectedId]);

  const openTask = useCallback((id: string, task?: SelectableTask) => {
    setKbFocusId(id);
    setLocalSelectedId(id);
    // The card's own payload rides along so a task that belongs to a playbook
    // run still opens the run inspector, as it does in the legacy graph.
    onTaskClick(id, task);
  }, [onTaskClick]);
  const openPlaybook = useCallback((id: string) => {
    setKbFocusId(`playbook:${id}`);
    setLocalSelectedId(`playbook:${id}`);
    onPlaybookClick?.(id);
  }, [onPlaybookClick]);
  const clearSelection = useCallback(() => {
    setKbFocusId(null);
    setLocalSelectedId(null);
    onBackgroundClick?.();
  }, [onBackgroundClick]);

  const handlers = useMemo<FlowHandlers>(
    () => ({ onOpenTask: openTask, onToggleChildren: toggleExpanded, onFocus: setFocus }),
    [openTask, toggleExpanded, setFocus],
  );
  const onElements = useCallback(
    (pid: string, elements: LayerElements) => setLayers((prev) => new Map(prev).set(pid, elements)),
    [],
  );
  useEffect(() => setLayers((prev) => {
    const kept = new Map([...prev].filter(([pid]) => projectIds.includes(pid)));
    return kept.size === prev.size ? prev : kept;
  }), [projectIds]);

  const nodes = useMemo(() => {
    const playbookNodes: Node[] = playbooks.map((playbook, i) => ({
      id: `playbook:${playbook.id}`,
      type: "playbook",
      position: toPx(i % PLAYBOOKS_PER_ROW, -1.5 - Math.floor(i / PLAYBOOKS_PER_ROW) * 1.3),
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      draggable: false,
      connectable: false,
      data: { playbook, onOpenPlaybook: openPlaybook },
    }));
    const headers: Node[] = projectIds.length > 1 ? projectIds.map((pid) => ({
      id: `project:${pid}`,
      type: "projectHeader",
      position: toPx(0, (offsets.get(pid) ?? 0) - 0.4),
      selectable: false,
      draggable: false,
      connectable: false,
      zIndex: 0,
      className: "aq-project-header",
      data: { label: projectNames.get(pid) ?? pid },
    })) : [];
    const all = [...playbookNodes, ...headers, ...projectIds.flatMap((pid) => layers.get(pid)?.nodes ?? [])];
    return all.map((node) => ({
      ...node,
      selected: node.id === selectedId,
      className: node.id === kbFocusId ? [node.className, "aq-focused"].filter(Boolean).join(" ") : node.className,
    }));
  }, [playbooks, projectIds, offsets, layers, selectedId, kbFocusId, openPlaybook, projectNames]);
  const edges = useMemo(
    () => projectIds.flatMap((pid) => layers.get(pid)?.edges ?? []),
    [projectIds, layers],
  );
  const workers = useMemo(
    () => projectIds.flatMap((pid) => layers.get(pid)?.workers ?? []),
    [projectIds, layers],
  );
  const pending = projectIds.some((pid) => layers.get(pid)?.pending ?? true);
  const allLoaded = projectIds.every((pid) => layers.get(pid)?.loaded);
  const relationTypes = useMemo(
    () => [...new Set(edges.map((edge) => String(edge.data?.depType)))].sort(),
    [edges],
  );

  // Focus zooms to the focused container; its subtree already arrives whole,
  // and dependencies leaving it arrive as stubs.
  const focusProject = projectIds[0];
  const focusOffset = offsets.get(focusProject ?? "") ?? 0;
  const { data: focusNode } = useLayoutNode(focusId ? focusProject : undefined, focusId);
  useEffect(() => {
    if (!focusId || !focusNode) return;
    const position = toPx(focusNode.node.x, focusNode.node.y + focusOffset);
    const box = sizePx(focusNode.node.w, focusNode.node.h);
    fitBounds({ x: position.x, y: position.y, width: box.width, height: box.height }, { padding: 0.1, duration: 0 });
  }, [focusId, focusNode, fitBounds, focusOffset]);

  // Jumping to a search result only needs the hit's box: the tiles covering it
  // load from the viewport change like any other pan.
  const jumpOffset = offsets.get(focusProject ?? "") ?? 0;
  useEffect(() => {
    if (!jumpTarget) return;
    const position = toPx(jumpTarget.x, jumpTarget.y + jumpOffset);
    const box = sizePx(jumpTarget.w, jumpTarget.h);
    fitBounds({ x: position.x, y: position.y, width: box.width, height: box.height }, { padding: 0.4, duration: 300 });
    setKbFocusId(jumpTarget.id);
  }, [jumpTarget, jumpOffset, fitBounds]);

  const openNode = (node: Node) => {
    if (node.type === "playbook") openPlaybook(String((node.data.playbook as { id: string }).id));
    else if (node.type !== "projectHeader") openTask(node.id, (node.data as Partial<TaskNodeData>).task);
  };

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
    const taskButton = target.closest<HTMLButtonElement>("button[data-task-id], button[data-graph-node-id]");
    if (target.closest("button, a, summary") && !taskButton) return;
    const fromId = taskButton?.dataset.graphNodeId ?? taskButton?.dataset.taskId ?? kbFocusId ?? selectedId;
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
        setKbFocusId(next.id);
        const button = [...(wrapRef.current?.querySelectorAll<HTMLButtonElement>("button[data-task-id], button[data-graph-node-id]") ?? [])]
          .find((element) => (element.dataset.graphNodeId ?? element.dataset.taskId) === next.id);
        button?.focus({ preventScroll: true });
      }
    } else if (!taskButton && (event.key === "Enter" || event.key === "o")) {
      event.preventDefault();
      openNode(from);
    }
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      {focusId && <Breadcrumbs
        projectName={projectNames.get(focusProject ?? "") ?? "Project"}
        ancestors={focusNode?.ancestors?.map((ancestor) => ({ id: ancestor.id, title: ancestor.title })) ?? []}
        current={focusNode ? { id: focusNode.node.id, title: focusNode.node.title } : { id: focusId, title: focusId }}
        onSelect={setFocus} />}
      <div ref={wrapRef} role="region" aria-label="Task graph" tabIndex={0} onKeyDown={onKeyDown}
        className="relative min-h-0 flex-1 outline-none">
        {projectIds.map((pid) => (
          <ProjectLayer key={pid} projectId={pid} projectNames={projectNames} offsetY={offsets.get(pid) ?? 0} params={params}
            viewport={viewport} width={size.w} height={size.h} expanded={expandedTaskIds} handlers={handlers}
            onBudgetExceeded={onBudgetExceeded} onElements={onElements} />
        ))}
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          colorMode="dark"
          onlyRenderVisibleElements
          defaultViewport={initialViewport}
          minZoom={0.15}
          maxZoom={2}
          onMove={onMove}
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
          onNodeClick={(_, node) => openNode(node)}
          onPaneClick={clearSelection}
        >
          <Background gap={24} color="#1f2937" />
          <Controls position="bottom-right" showInteractive={false} />
          <AgentAvatarLayer agents={workers} />
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
        {pending && <div role="status" className="pointer-events-none absolute inset-0 flex items-center justify-center bg-gray-950/70 text-sm text-gray-300">Laying out…</div>}
        {allLoaded && !pending && nodes.length === 0 && <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-gray-500">No tasks or playbooks match these filters.</p>}
      </div>
    </div>
  );
}

export default function LayoutCanvas(props: LayoutCanvasProps) {
  return <ReactFlowProvider><Inner {...props} /></ReactFlowProvider>;
}
