import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  Background, Controls, Panel, ReactFlow, ReactFlowProvider, useReactFlow, type Edge, type Node,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import TaskNode from "../TaskNode";
import PlaybookNode from "../PlaybookNode";
import AgentAvatarLayer from "../AgentAvatarLayer";
import ContainerNode from "./ContainerNode";
import Breadcrumbs from "./Breadcrumbs";
import GraphScopeNotice, { type EmptyReason } from "./GraphScopeNotice";
import { edgeStyleForType } from "./edgeStyle";
import { useGraphState } from "../useGraphHierarchy";
import {
  fetchRunningTarget, useHiddenFinishedCount, useLayoutExtents, useLayoutNode, type TilesParams, type Variant,
} from "../../../api/graphLayout";
import type { RunningTarget } from "../../../api/graphLayout";
import { clearRunningWorkJump, publishRunningWorkNotice } from "./runningWork";
import { useLayoutTiles } from "./useLayoutTiles";
import { publishAppliedVariant } from "./appliedVariant";
import { refetchLayout, registerLayoutRefetch } from "./liveRegistry";
import { enteredBounds, toFlowElements, type FlowCache, type FlowHandlers, type WorldRect } from "./flowNodes";
import { CELL, fromPx, sizePx, toPx, worldRectFromViewport, type Rect } from "./units";
import type { LayoutDensity } from "./density";
import { PLAYBOOK_POSITION_SCOPE } from "./manualPositions";
import {
  NODE_HEIGHT, NODE_WIDTH, type ContainerNodeData, type GraphViewProps, type GraphWorker,
  type SelectableTask, type TaskNodeData,
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

/** A boundary count, not a destination: no pointer events, no focus, no click. */
function OverflowMarkerNode({ data }: { data: { label: string } }) {
  return (
    <div role="note" title={`${data.label} outside this view`}
      className="pointer-events-none flex h-full w-full items-center justify-center whitespace-nowrap rounded-full border border-dashed border-gray-600 bg-gray-900/80 px-1 text-[10px] leading-none text-gray-400">
      {data.label}
    </div>
  );
}

const nodeTypes = {
  task: TaskNode, playbook: PlaybookNode, container: ContainerNode,
  projectHeader: ProjectHeaderNode, overflowMarker: OverflowMarkerNode,
};
const NO_PLAYBOOKS: NonNullable<GraphViewProps["playbooks"]> = [];
/** Stable identity for "nothing is expanded", which is now always the case. */
const NO_EXPANSION: string[] = [];
const PROJECT_GAP = 2;
const PLAYBOOKS_PER_ROW = 4;
const initialViewport = { x: 0, y: 0, zoom: 1 };
/**
 * Below this zoom a card is a few pixels tall, so the ×N labels are unreadable.
 * Edge paths and dependency styling stay the same at every zoom. Which nodes
 * are drawn is unaffected — zoom is paint here, never structure:
 * containers are compact tiles at every zoom and are opened by entering them.
 */
const EDGE_LABEL_ZOOM = 0.5;
const RELATION_LABELS: Record<string, string> = {
  blocks: "blocks",
  "parent-child": "parent-child",
  "waits-for": "waits-for",
  "conditional-blocks": "conditional-blocks",
  "discovered-from": "discovered-from",
};

interface Viewport { x: number; y: number; zoom: number }

function movable(node: Node): boolean {
  return node.type === "playbook";
}

function positionScope(node: Node): typeof PLAYBOOK_POSITION_SCOPE | null {
  if (node.type === "playbook") return PLAYBOOK_POSITION_SCOPE;
  return null;
}

const snapPosition = (value: number) => Math.round(value * 10) / 10;

export interface LayoutCanvasProps extends Pick<GraphViewProps,
  "onTaskClick" | "onBackgroundClick" | "selectedTaskId" | "playbooks" | "selectedPlaybookId" | "onPlaybookClick"> {
  projectIds: string[];
  projectNames: Map<string, string>;
  variant: Variant;
  filters: TaskFilters;
  focusId: string | null;
  setFocus: (id: string | null) => void;
  /** Wired to the empty state's "Show completed" button, when the canvas has
   * nothing to draw because finished work is hidden. */
  setShowCompleted: (show: boolean) => void;
  /** A located match the toolbar asked for; each request is a fresh object. */
  jumpTarget?: LocateHit | null;
  /** Live work selected by the toolbar, or the default root viewport. */
  runningTarget?: RunningTarget | null;
  /** Toolbar jumps are cancelled if the reader navigates before the frame lands. */
  manualRunningTarget?: boolean;
}

interface LayerElements {
  nodes: Node[];
  edges: Edge[];
  workers: GraphWorker[];
  pending: boolean;
  loaded: boolean;
  error: Error | null;
  /** The variant the daemon served this layer's last response from. */
  variantApplied: string | null;
  /** The entered container's cropped frame plus its docked stubs, in world
   *  units of this project's own frame; null outside a container. */
  enteredBounds: WorldRect | null;
}

interface LayerProps {
  projectId: string;
  projectNames: ReadonlyMap<string, string>;
  offsetY: number;
  params: TilesParams;
  viewport: Viewport | null;
  width: number;
  height: number;
  focusId: string | null;
  handlers: FlowHandlers;
  onElements: (projectId: string, elements: LayerElements) => void;
  density: LayoutDensity;
  hideEdgeLabels: boolean;
}

function nearestIn(nodes: Node[], from: Node, dir: "up" | "down" | "left" | "right"): Node | null {
  let best: Node | null = null;
  let bestScore = Infinity;
  for (const node of nodes) {
    // Project bands and "+N more" pills are labels, not destinations.
    if (node.id === from.id || node.type === "projectHeader" || node.type === "overflowMarker") continue;
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
  projectId, projectNames, offsetY, params, viewport, width, height, focusId, handlers,
  onElements, density, hideEdgeLabels,
}: LayerProps) {
  const rawRect = useMemo<Rect | null>(() => {
    if (!viewport || width === 0) return null;
    const world = worldRectFromViewport(viewport, width, height, density);
    return { x0: world.x0, y0: world.y0 - offsetY, x1: world.x1, y1: world.y1 - offsetY };
  }, [viewport, width, height, offsetY, density]);
  // A pan of a few pixels covers exactly the cells the last one did, so the
  // rect only gets a new identity when the tile coverage actually changes:
  // `useLayoutTiles` re-runs its viewport effect on every new rect, and
  // during a drag that is once per animation frame.
  const coverage = rawRect
    ? `${Math.floor(rawRect.x0 / CELL)}:${Math.floor(rawRect.y0 / CELL)}:${Math.ceil(rawRect.x1 / CELL)}:${Math.ceil(rawRect.y1 / CELL)}`
    : "";
  // `coverage` is the tile-grid identity of `rawRect`; holding the rect that
  // crossed into the new coverage keeps a real viewport rect (and so a real
  // centre cell) rather than a snapped-out one.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const rect = useMemo<Rect | null>(() => rawRect, [coverage]);

  const { store, pending, loaded, error, refetchVisible } = useLayoutTiles(projectId, params, rect);

  useEffect(
    () => registerLayoutRefetch(projectId, refetchVisible),
    [projectId, refetchVisible],
  );

  // The previous conversion, so a re-delivered layout hands back the very same
  // Node objects for the cards that did not change and React Flow re-renders
  // only the ones that did.
  const flowCache = useRef<FlowCache | undefined>(undefined);
  useEffect(() => {
    const { nodes, edges, cache } = toFlowElements(
      store, { projectId, offsetY, focusId, handlers, projectNames, density, hideEdgeLabels }, flowCache.current,
    );
    flowCache.current = cache;
    // Docking is resolved server-side, so a worker's `docked_at` is already a
    // visible node id.
    const workers: GraphWorker[] = store.workers.map((worker) => ({
      id: worker.agent_id, name: worker.name, current_task_id: worker.docked_at,
      in_collapsed: worker.in_collapsed, profile_id: null, session_id: null,
    }));
    onElements(projectId, {
      nodes, edges, workers, pending, loaded, error, variantApplied: store.variantApplied,
      enteredBounds: enteredBounds(store, focusId, projectId),
    });
  }, [store, pending, loaded, error, projectId, projectNames, offsetY, focusId, handlers, onElements, density, hideEdgeLabels]);

  return null;
}

function Inner(props: LayoutCanvasProps) {
  const {
    projectIds, projectNames, variant, filters, focusId, setFocus, setShowCompleted, jumpTarget,
    runningTarget, manualRunningTarget = false, onTaskClick,
    onBackgroundClick, selectedTaskId, playbooks = NO_PLAYBOOKS, selectedPlaybookId, onPlaybookClick,
  } = props;
  const { density, manualPositions, saveGraphPosition } = useGraphState();
  // Entering a container is the root view one level down: the same variant,
  // so "Show completed" still means what it says inside a container. The
  // daemon promotes a focused request to the full layout by itself when the
  // container entered is one the active layout dropped.
  const requestVariant: Variant = variant;
  // Only meaningful for the empty-graph caption: how many finished tasks are
  // hidden, read only from whatever the "all" variant's extent already sits
  // in the query cache -- never a request of its own.
  const hiddenFinishedCount = useHiddenFinishedCount(projectIds, requestVariant);
  const { fitBounds, setCenter } = useReactFlow();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [viewport, setViewport] = useState<Viewport | null>(initialViewport);
  const [layers, setLayers] = useState<ReadonlyMap<string, LayerElements>>(new Map());
  const [localSelectedId, setLocalSelectedId] = useState<string | null>(null);
  const [kbFocusId, setKbFocusId] = useState<string | null>(null);
  const [dragPositions, setDragPositions] = useState<Record<string, { x: number; y: number }>>({});
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

  // A boolean, not the zoom: it flips once on the way past the threshold, so
  // the label rebuild happens on that crossing and not on every frame of a
  // pinch.
  const hideEdgeLabels = (viewport?.zoom ?? 1) < EDGE_LABEL_ZOOM;

  // Projects stack vertically: each starts below the previous project's extent.
  const extents = useLayoutExtents(projectIds, requestVariant);
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

  // One scope per view: the project root, or the container entered. Nothing
  // is ever expanded in place, so `expanded` is always empty and no zoom
  // level changes which nodes are asked for (operator decision 2026-09-20).
  const params = useMemo<TilesParams>(() => ({
    variant: requestVariant,
    expanded: NO_EXPANSION,
    root: focusId,
    q: filters.query.trim(),
    status: filters.status,
  }), [requestVariant, focusId, filters.query, filters.status]);

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
    () => ({ onOpenTask: openTask, onFocus: setFocus }),
    [openTask, setFocus],
  );
  const onElements = useCallback(
    (pid: string, elements: LayerElements) => setLayers((prev) => new Map(prev).set(pid, elements)),
    [],
  );
  useEffect(() => setLayers((prev) => {
    const kept = new Map([...prev].filter(([pid]) => projectIds.includes(pid)));
    return kept.size === prev.size ? prev : kept;
  }), [projectIds]);

  const playbookNodes = useMemo<Node[]>(() => playbooks.map((playbook, i) => ({
    id: `playbook:${playbook.id}`,
    type: "playbook",
    position: toPx(i % PLAYBOOKS_PER_ROW, -1.5 - Math.floor(i / PLAYBOOKS_PER_ROW) * 1.3, density),
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    draggable: true,
    connectable: false,
    data: { playbook, onOpenPlaybook: openPlaybook },
  })), [playbooks, openPlaybook, density]);
  const headers = useMemo<Node[]>(() => projectIds.length > 1 ? projectIds.map((pid) => ({
    id: `project:${pid}`,
    type: "projectHeader",
    position: toPx(0, (offsets.get(pid) ?? 0) - 0.4, density),
    selectable: false,
    draggable: false,
    connectable: false,
    zIndex: 0,
    className: "aq-project-header",
    data: { label: projectNames.get(pid) ?? pid },
  })) : [], [projectIds, offsets, projectNames, density]);
  // Selection and keyboard focus decorate at most two cards, so only those get
  // a new object: every other node keeps the identity the layer handed over,
  // and React Flow re-renders nothing for them.
  const nodes = useMemo(() => {
    const all = [...playbookNodes, ...headers, ...projectIds.flatMap((pid) => layers.get(pid)?.nodes ?? [])];
    return all.map((node) => {
      const scope = positionScope(node);
      const saved = scope ? manualPositions[scope]?.[node.id] : undefined;
      const position = dragPositions[node.id]
        ?? (saved ? toPx(saved.x, saved.y, density) : node.position);
      const selected = node.id === selectedId;
      const focused = node.id === kbFocusId;
      const draggable = movable(node);
      if (!saved && !dragPositions[node.id] && !selected && !focused && node.draggable === draggable) return node;
      return {
        ...node,
        position,
        draggable,
        selected,
        className: focused ? [node.className, "aq-focused"].filter(Boolean).join(" ") : node.className,
      };
    });
  }, [playbookNodes, headers, projectIds, layers, selectedId, kbFocusId, manualPositions, dragPositions, density]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const allowed = new Set(nodes.filter(movable).map((node) => node.id));
    setDragPositions((current) => {
      let next = current;
      for (const change of changes) {
        if (change.type !== "position" || !change.position || !allowed.has(change.id)) continue;
        if (next === current) next = { ...current };
        next[change.id] = change.position;
      }
      return next;
    });
  }, [nodes]);

  const onNodeDragStop = useCallback((_: MouseEvent | TouchEvent, node: Node) => {
    if (!movable(node)) return;
    const scope = positionScope(node);
    if (!scope) return;
    const world = fromPx(node.position, density);
    const position = { x: snapPosition(world.x), y: snapPosition(world.y) };
    saveGraphPosition(scope, node.id, position);
    setDragPositions((current) => {
      if (!(node.id in current)) return current;
      const next = { ...current };
      delete next[node.id];
      return next;
    });
  }, [density, saveGraphPosition]);
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
  // A focused response ALWAYS carries the container entered, so "nothing
  // here" is "nothing but the container itself" -- gating on an empty node
  // list could never fire inside one.
  const nothingDrawn = nodes.every((node) => node.id === focusId);

  // A failed tiles request must never be reported as an empty graph.
  const layerError = projectIds.map((pid) => layers.get(pid)?.error).find(Boolean) ?? null;
  const retryLayers = useCallback(
    () => projectIds.forEach((pid) => refetchLayout(pid)),
    [projectIds],
  );
  const relationTypes = useMemo(
    () => [...new Set(edges.map((edge) => String(edge.data?.depType)))].sort(),
    [edges],
  );

  // Entering zooms to the container entered; its direct children arrive as
  // tiles (their own children collapsed into them) and dependencies leaving
  // the container arrive as stubs.
  const focusProject = projectIds[0];
  const focusOffset = offsets.get(focusProject ?? "") ?? 0;
  // A project whose layout is still building answers 202 for the focus node:
  // there is no box to fit and no title to show until it lands.
  const { data: focusData } = useLayoutNode(focusId ? focusProject : undefined, focusId);
  const focusNode = focusData && !("pending" in focusData) ? focusData : undefined;
  // What the daemon actually served, which is not always what was asked for:
  // entering a container the active layout stubbed or dropped is answered
  // from `all`. That condition is the LAYOUT's (a container every one of
  // whose descendants has finished is stubbed whatever its own status), so it
  // is read from the response and never inferred.
  const appliedVariant = layers.get(focusProject ?? "")?.variantApplied ?? null;
  const emptyReason: EmptyReason | null = !nothingDrawn ? null
    : filters.showCompleted ? "no_work"
      : (filters.query.trim() || filters.status) ? "no_matches" : "all_finished";
  useEffect(() => { publishAppliedVariant((appliedVariant as Variant | null) ?? null); }, [appliedVariant]);
  // Only on unmount: publishing null between values would make the toolbar
  // fall back to the filters' variant for a render and re-issue its locate.
  useEffect(() => () => publishAppliedVariant(null), []);
  // Two steps: the persisted box first, so the tiles covering the children are
  // the ones requested, then -- once, when those tiles have landed -- the
  // frame cropped to the children plus the stubs docked beside it. The
  // persisted box is sized for the fully expanded subtree and can be several
  // times the content, which left the children a speck in the middle. Each
  // step runs once per entry, so a refetch never yanks a viewport the
  // operator has since panned.
  const boxFitted = useRef<string | null>(null);
  const fittedEntry = useRef<string | null>(null);
  useEffect(() => {
    if (!focusId) { boxFitted.current = null; return; }
    if (!focusNode) return;
    const key = `${focusId}|${density}`;
    if (boxFitted.current === key) return;
    boxFitted.current = key;
    fittedEntry.current = null;
    const position = toPx(focusNode.node.x, focusNode.node.y + focusOffset, density);
    const box = sizePx(focusNode.node.w, focusNode.node.h, density);
    fitBounds({ x: position.x, y: position.y, width: box.width, height: box.height }, { padding: 0.1, duration: 0 });
  }, [focusId, focusNode, fitBounds, focusOffset, density]);
  const focusLayer = layers.get(focusProject ?? "");
  const entered = focusLayer?.loaded && !focusLayer.pending ? focusLayer.enteredBounds : null;
  useEffect(() => {
    if (!focusId || !focusNode || !entered) return;
    const key = `${focusId}|${density}`;
    if (boxFitted.current !== key || fittedEntry.current === key) return;
    fittedEntry.current = key;
    const position = toPx(entered.x, entered.y + focusOffset, density);
    const box = sizePx(entered.w, entered.h, density);
    fitBounds({ x: position.x, y: position.y, width: box.width, height: box.height }, { padding: 0.1, duration: 0 });
  }, [focusId, focusNode, entered, fitBounds, focusOffset, density]);

  // The URL has already entered the selected task's immediate parent.  Wait
  // for the node lookup that belongs to that scope, then pan once.  At the
  // root a nested leaf is collapsed, so pan to its outermost visible ancestor
  // instead; the target itself is used for root leaves.
  const runningHandled = useRef<RunningTarget | null>(null);
  const runningChecked = useRef<RunningTarget | null>(null);
  useEffect(() => {
    if (!runningTarget) {
      runningHandled.current = null;
      runningChecked.current = null;
      return;
    }
    if (manualRunningTarget && focusId !== (runningTarget.parent_task_id ?? null)) {
      clearRunningWorkJump();
      return;
    }
    if (runningHandled.current === runningTarget) return;
    // The node lookup uses the persisted "all" layout. The active canvas
    // compacts finished work away, so those coordinates can be far outside
    // the viewport. Fit the card that this scope actually drew instead.
    const visibleId = !focusId && runningTarget.ancestors?.length
      ? runningTarget.ancestors[0]!
      : runningTarget.task_id;
    const layer = layers.get(runningTarget.project_id);
    const visible = layer?.loaded && !layer.pending
      ? layer.nodes.find((node) => node.id === visibleId)
      : undefined;
    if (!visible) return;
    const box = {
      x: visible.position.x, y: visible.position.y,
      width: visible.width ?? NODE_WIDTH, height: visible.height ?? NODE_HEIGHT,
    };
    // Selection and the later tile load are necessarily separate reads. A
    // task may finish in that gap, so confirm the target once before moving
    // the viewport. A vanished or replaced target leaves this navigation
    // scope alone and gives the toolbar its normal empty-state notice.
    if (manualRunningTarget && runningChecked.current !== runningTarget) {
      runningChecked.current = runningTarget;
      let cancelled = false;
      void fetchRunningTarget(runningTarget.project_id).then((current) => {
        if (cancelled || runningHandled.current === runningTarget) return;
        if (current?.task_id !== runningTarget.task_id) {
          clearRunningWorkJump();
          publishRunningWorkNotice(current ? "Running work changed. Try again." : "No running work");
          return;
        }
        runningHandled.current = runningTarget;
        setKbFocusId(runningTarget.task_id);
        fitBounds(box, { padding: 0.4, duration: 300 });
      }).catch(() => {
        if (!cancelled) publishRunningWorkNotice("Could not find running work. Try again.");
      });
      return () => { cancelled = true; };
    }
    runningHandled.current = runningTarget;
    setKbFocusId(runningTarget.task_id);
    fitBounds(box, {
      padding: 0.2,
      duration: 0,
    });
  }, [runningTarget, manualRunningTarget, focusId, layers, fitBounds]);

  // Jumping to a search result in THIS scope only needs the hit's box: the
  // tiles covering it load from the viewport change like any other pan. A hit
  // that lives inside another container is reached by entering that container
  // -- there is no inline expansion to reveal it, and its coordinates belong
  // to that scope, so fitting them here would frame the wrong place.
  const jumpOffset = offsets.get(focusProject ?? "") ?? 0;
  // Acting on a hit can change the scope, which re-runs this effect: the hit
  // is spent once so the coordinates of the scope it was located in are never
  // applied to the scope it took us to.
  const jumpHandled = useRef<LocateHit | null>(null);
  useEffect(() => {
    if (!jumpTarget || jumpHandled.current === jumpTarget) return;
    jumpHandled.current = jumpTarget;
    setKbFocusId(jumpTarget.id);
    const container = jumpTarget.container_id ?? null;
    // A search force-opens the ancestors of its matches server-side -- the
    // one case where a container is drawn open -- so a hit can already be on
    // screen in another container's tile. Re-scoping then would throw the
    // operator's place away for nothing: pan to it instead.
    const drawn = nodes.some((node) => node.id === jumpTarget.id);
    if (!drawn && container !== focusId) {
      setFocus(container);
      return;
    }
    const position = toPx(jumpTarget.x, jumpTarget.y + jumpOffset, density);
    const box = sizePx(jumpTarget.w, jumpTarget.h, density);
    fitBounds({ x: position.x, y: position.y, width: box.width, height: box.height }, { padding: 0.4, duration: 300 });
    // `nodes` is deliberately not a dependency: the hit is spent on first
    // use, so re-running on every node delivery would do nothing but churn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jumpTarget, jumpOffset, fitBounds, density, focusId, setFocus]);

  /** Double-clicking a tile that can be entered goes into it; the enter
   *  control on the tile is the same action. */
  const enterNode = (node: Node) => {
    (node.data as { onFocus?: (id: string) => void }).onFocus?.(node.id);
  };

  const openNode = (node: Node) => {
    if (node.type === "playbook") openPlaybook(String((node.data.playbook as { id: string }).id));
    // A container's payload is its `node`; a card's is its `task`.
    else if (node.type === "container") openTask(node.id, (node.data as ContainerNodeData).node);
    // Project bands and overflow pills carry no task to open.
    else if (node.type !== "projectHeader" && node.type !== "overflowMarker") {
      openTask(node.id, (node.data as Partial<TaskNodeData>).task);
    }
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
        // `onlyRenderVisibleElements` means an off-screen target has no button
        // to focus: bring it into view first, then focus it once React Flow has
        // rendered it.
        const width = next.width ?? NODE_WIDTH;
        const height = next.height ?? NODE_HEIGHT;
        setCenter(next.position.x + width / 2, next.position.y + height / 2,
          { zoom: viewport?.zoom ?? 1, duration: 0 });
        requestAnimationFrame(() => {
          const button = [...(wrapRef.current?.querySelectorAll<HTMLButtonElement>("button[data-task-id], button[data-graph-node-id]") ?? [])]
            .find((element) => (element.dataset.graphNodeId ?? element.dataset.taskId) === next.id);
          button?.focus({ preventScroll: true });
        });
      }
    } else if (!taskButton && (event.key === "Enter" || event.key === "o")) {
      event.preventDefault();
      openNode(from);
    }
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      {layerError && <p role="alert" className="shrink-0 border-b border-amber-800/50 bg-amber-950/30 px-4 py-2 text-sm text-amber-200">
        Could not load the graph. {layerError.message}{" "}
        <button type="button" className="underline" onClick={retryLayers}>Retry</button>
      </p>}
      {focusId && <Breadcrumbs
        projectName={projectNames.get(focusProject ?? "") ?? "Project"}
        ancestors={focusNode?.ancestors?.map((ancestor) => ({ id: ancestor.id, title: ancestor.title })) ?? []}
        current={focusNode ? { id: focusNode.node.id, title: focusNode.node.title } : { id: focusId, title: focusId }}
        onSelect={setFocus} />}
      {/* A container the active layout does not carry is answered from the
        * full one, so finished children are on screen with "Show completed"
        * off. Say so, rather than leaving the reader to wonder. */}
      {focusId && <GraphScopeNotice requestedVariant={requestVariant}
        appliedVariant={(appliedVariant as Variant | null) ?? null} emptyReason={null}
        showCompleted={filters.showCompleted} onShowCompleted={setShowCompleted}
        showEmpty={false} loading={pending} error={layerError} />}
      <div ref={wrapRef} role="region" aria-label="Task graph" tabIndex={0} onKeyDown={onKeyDown}
        className="relative min-h-0 flex-1 outline-none">
        {projectIds.map((pid) => (
          <ProjectLayer key={pid} projectId={pid} projectNames={projectNames} offsetY={offsets.get(pid) ?? 0} params={params}
            viewport={viewport} width={size.w} height={size.h} focusId={focusId} handlers={handlers}
            onElements={onElements} density={density} hideEdgeLabels={hideEdgeLabels} />
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
          nodesDraggable={playbookNodes.length > 0}
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
          onNodeDoubleClick={(_, node) => enterNode(node)}
          onNodesChange={onNodesChange}
          onNodeDragStop={onNodeDragStop}
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
        {allLoaded && !pending && !layerError && nothingDrawn && <GraphScopeNotice
          requestedVariant={requestVariant} appliedVariant={(appliedVariant as Variant | null) ?? null}
          emptyReason={emptyReason} showCompleted={filters.showCompleted} onShowCompleted={setShowCompleted}
          hiddenFinishedCount={hiddenFinishedCount} showBanner={false}
          emptyClassName="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center text-sm text-gray-500" />}
      </div>
    </div>
  );
}

export default function LayoutCanvas(props: LayoutCanvasProps) {
  return <ReactFlowProvider><Inner {...props} /></ReactFlowProvider>;
}
