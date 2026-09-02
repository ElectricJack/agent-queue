import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type { GraphGate, LayoutNode } from "@aq/ts-client";
import { edgeStyleForType } from "../layout";
import type { ContainerNodeData, SelectableTask, TaskNodeData } from "../types";
import type { LayoutStore } from "./layoutStore";
import { sizePx, toPx } from "./units";
import { DENSITY_SCALE, type LayoutDensity } from "./density";

/** Where a stub belonging to another project is docked, in world units. */
const STUB_PORT_X = -1.2;
/** The "+N more" pill: small enough to sit beside a card without covering it. */
const OVERFLOW_W = 0.4;
const OVERFLOW_H = 0.3;
const OVERFLOW_GAP = 0.05;

export interface FlowHandlers { onOpenTask: (id: string, task?: SelectableTask) => void; onToggleChildren: (id: string) => void; onFocus: (id: string) => void }
export interface FlowContext {
  projectId: string;
  offsetY: number;
  expanded: ReadonlySet<string>;
  handlers: FlowHandlers;
  /** Display names by project id, for stubs that live in another project. */
  projectNames?: ReadonlyMap<string, string>;
  density?: LayoutDensity;
}

/** The card payload for one layout node; shared with the flat mobile list. */
export function taskNodeData(n: LayoutNode, ctx: FlowContext, gates: GraphGate[]): TaskNodeData {
  const task = {
    id: n.id, title: n.title, status: n.status, priority: n.priority, is_blocked: n.is_blocked,
    profile_id: n.profile_id, intelligence_class: n.intelligence_class, assigned_agent_id: n.assigned_agent_id,
    branch_name: n.branch_name, pr_url: n.pr_url, playbook_run_id: n.playbook_run_id,
  };
  return {
    task, gates, projectId: ctx.projectId,
    hierarchy: {
      parentId: n.container_id ?? null, parentTitle: null, depth: n.depth, childCount: n.agg_children ?? 0,
      visibleChildCount: n.agg_children ?? 0, descendantCount: n.agg_descendants ?? 0, completedCount: n.agg_completed ?? 0,
      runningCount: n.agg_running ?? 0, blockedCount: n.agg_blocked ?? 0, expanded: n.kind !== "collapsed" && n.kind !== "stub",
      autoExpanded: false, contextOnly: n.context_only ?? false,
    },
    onOpenTask: ctx.handlers.onOpenTask, onToggleChildren: ctx.handlers.onToggleChildren, onFocus: ctx.handlers.onFocus,
    layoutScale: DENSITY_SCALE[ctx.density ?? "comfortable"],
  };
}

export function toFlowElements(store: LayoutStore, ctx: FlowContext): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const pos = new Map<string, { x: number; y: number }>();
  const gatesFor = (id: string) => store.gates.filter((g) => g.task_ids?.includes(id));
  for (const n of store.nodes.values()) {
    const position = toPx(n.x, n.y + ctx.offsetY, ctx.density);
    pos.set(n.id, { x: n.x, y: n.y });
    if (n.kind === "container") {
      const data: ContainerNodeData = { node: n, projectId: ctx.projectId, ...ctx.handlers, layoutScale: DENSITY_SCALE[ctx.density ?? "comfortable"] };
      nodes.push({ id: n.id, type: "container", position, ...sizePx(n.w, n.h, ctx.density), zIndex: n.depth, selectable: false, draggable: false, connectable: false, data });
    } else {
      nodes.push({ id: n.id, type: "task", position, ...sizePx(1, 1, ctx.density), zIndex: 100 + n.depth, draggable: false, connectable: false, data: taskNodeData(n, ctx, gatesFor(n.id)) });
    }
  }
  for (const s of store.stubs.values()) {
    if (store.nodes.has(s.id)) continue;
    // A stub from another project carries coordinates in that project's frame,
    // so it is drawn as a labelled port just outside this project's left edge.
    const foreign = s.project_id !== ctx.projectId;
    const x = foreign ? STUB_PORT_X : s.x;
    const title = foreign
      ? `${ctx.projectNames?.get(s.project_id) ?? s.project_id} · ${s.title ?? s.id}`
      : s.title ?? s.id;
    pos.set(s.id, { x, y: s.y });
    const stub: LayoutNode = { id: s.id, title, status: "PENDING", priority: 100, is_blocked: false, x, y: s.y, w: 1, h: 1, depth: 0,
      container_id: null, kind: "stub", context_only: true, agg_children: 0, agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0 } as LayoutNode;
    nodes.push({ id: s.id, type: "task", className: "aq-stub", position: toPx(x, s.y + ctx.offsetY, ctx.density), ...sizePx(1, 1, ctx.density), zIndex: 5, draggable: false, connectable: false, data: taskNodeData(stub, ctx, gatesFor(s.id)) });
  }
  // Boundary markers: a card with more far dependencies than the tile carries
  // stubs for gets one "+N more" pill on the side the links leave from. It is
  // its own node type — a marker is not a task, so it must not be clickable,
  // focusable or a keyboard destination.
  for (const [key, overflow] of store.stubOverflow) {
    const anchor = store.nodes.get(overflow.node_id);
    if (!anchor || overflow.more <= 0) continue;
    const outward = overflow.direction === "out";
    const x = outward ? anchor.x + anchor.w + OVERFLOW_GAP : anchor.x - OVERFLOW_W - OVERFLOW_GAP;
    const y = anchor.y + anchor.h / 2 - OVERFLOW_H / 2;
    nodes.push({
      id: `overflow:${key}`, type: "overflowMarker", className: "aq-stub-overflow",
      position: toPx(x, y + ctx.offsetY, ctx.density), ...sizePx(OVERFLOW_W, OVERFLOW_H, ctx.density),
      zIndex: 200 + anchor.depth, selectable: false, focusable: false, draggable: false,
      connectable: false,
      data: { label: `+${overflow.more} more`, direction: overflow.direction, anchorId: anchor.id },
    });
  }
  const edges: Edge[] = [];
  for (const e of store.edges.values()) {
    const from = pos.get(e.from), to = pos.get(e.to);
    if (!from || !to) continue;
    const vertical = Math.abs(from.y - to.y) > 0.5;
    // ``from`` is the dependent and ``to`` the blocker; the rendered edge
    // travels blocker → dependent, so compare them in that direction.
    const rightward = from.x >= to.x;
    edges.push({
      id: `${e.from}|${e.to}|${e.dep_type}`, source: e.to, target: e.from, type: "smoothstep",
      sourceHandle: vertical ? "out-bottom" : rightward ? "out-right" : "out-left",
      targetHandle: vertical ? "in-top" : rightward ? "in-left" : "in-right",
      label: (e.count ?? 1) > 1 ? `×${e.count}` : undefined, markerEnd: { type: MarkerType.ArrowClosed },
      style: edgeStyleForType(e.dep_type), data: { depType: e.dep_type },
    });
  }
  return { nodes, edges };
}
