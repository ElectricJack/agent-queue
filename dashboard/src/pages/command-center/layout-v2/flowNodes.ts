import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type { GraphGate, LayoutNode } from "@aq/ts-client";
import { edgeStyleForType } from "./edgeStyle";
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
  /**
   * Visual level of detail: at far zoom the smoothstep router and the ×N
   * labels are invisible detail that still costs a path solve and a label
   * component per edge, so the edges are drawn straight and unlabelled.
   */
  simpleEdges?: boolean;
}

/**
 * The previous conversion's output, keyed by element id with the content
 * signature it was built from. Handing it back to `toFlowElements` lets an
 * unchanged element keep its object identity, which is what stops React Flow
 * re-rendering its card: `adoptUserNodes` reuses the internal node whenever
 * the user node is `===` the previous one, and `NodeWrapper` is memoised on
 * that internal node.
 */
export interface FlowCache {
  ctx: FlowContext | null;
  nodes: Map<string, { sig: string; node: Node }>;
  edges: Map<string, { sig: string; edge: Edge }>;
}

export interface FlowElements { nodes: Node[]; edges: Edge[]; cache: FlowCache }

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

const SEP = "\u0001";

/**
 * Everything a card or container renders from. A field read by `taskNodeData`,
 * `TaskCard` or `ContainerNode` and missing here would stop updating on the
 * canvas, so extend this whenever one of those grows a field.
 */
function nodeSignature(n: LayoutNode, gates: GraphGate[]): string {
  return [
    n.x, n.y, n.w, n.h, n.depth, n.kind, n.status, n.title, n.priority, n.is_blocked,
    n.container_id, n.context_only, n.agg_children, n.agg_descendants, n.agg_completed,
    n.agg_running, n.agg_blocked, n.profile_id, n.intelligence_class, n.assigned_agent_id,
    n.branch_name, n.pr_url, n.playbook_run_id,
    gates.map((g) => `${g.id}:${g.status}:${g.gate_type}`).join(","),
  ].join(SEP);
}

/** A cached element is only reusable while the surrounding context is the same. */
function sameContext(a: FlowContext | null, b: FlowContext): boolean {
  return !!a && a.projectId === b.projectId && a.offsetY === b.offsetY && a.density === b.density
    && a.handlers === b.handlers && a.projectNames === b.projectNames
    && a.expanded === b.expanded && !!a.simpleEdges === !!b.simpleEdges;
}

export function toFlowElements(store: LayoutStore, ctx: FlowContext, previous?: FlowCache): FlowElements {
  const reusable = previous && sameContext(previous.ctx, ctx) ? previous : null;
  const cache: FlowCache = { ctx, nodes: new Map(), edges: new Map() };
  const nodes: Node[] = [];
  const pos = new Map<string, { x: number; y: number }>();
  // One pass over the gates instead of one scan per node: with a container
  // expanded this was the O(nodes × gates) part of every conversion.
  const gatesByTask = new Map<string, GraphGate[]>();
  for (const gate of store.gates) {
    for (const id of gate.task_ids ?? []) {
      const list = gatesByTask.get(id);
      if (list) list.push(gate); else gatesByTask.set(id, [gate]);
    }
  }
  const NO_GATES: GraphGate[] = [];
  const gatesFor = (id: string) => gatesByTask.get(id) ?? NO_GATES;

  /** Reuse the previous object when nothing this element draws from moved. */
  const push = (id: string, sig: string, build: () => Node) => {
    const hit = reusable?.nodes.get(id);
    const node = hit && hit.sig === sig ? hit.node : build();
    cache.nodes.set(id, { sig, node });
    nodes.push(node);
  };

  for (const n of store.nodes.values()) {
    pos.set(n.id, { x: n.x, y: n.y });
    const gates = gatesFor(n.id);
    const sig = nodeSignature(n, gates);
    if (n.kind === "container") {
      push(n.id, sig, () => {
        const data: ContainerNodeData = { node: n, projectId: ctx.projectId, ...ctx.handlers, layoutScale: DENSITY_SCALE[ctx.density ?? "comfortable"] };
        return { id: n.id, type: "container", position: toPx(n.x, n.y + ctx.offsetY, ctx.density), ...sizePx(n.w, n.h, ctx.density), zIndex: n.depth, selectable: false, draggable: false, connectable: false, data };
      });
    } else {
      push(n.id, sig, () => ({
        id: n.id, type: "task", position: toPx(n.x, n.y + ctx.offsetY, ctx.density), ...sizePx(1, 1, ctx.density),
        zIndex: 100 + n.depth, draggable: false, connectable: false, data: taskNodeData(n, ctx, gates),
      }));
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
    const gates = gatesFor(s.id);
    push(s.id, nodeSignature(stub, gates), () => ({
      id: s.id, type: "task", className: "aq-stub", position: toPx(x, s.y + ctx.offsetY, ctx.density),
      ...sizePx(1, 1, ctx.density), zIndex: 5, draggable: false, connectable: false, data: taskNodeData(stub, ctx, gates),
    }));
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
    push(`overflow:${key}`, [overflow.more, overflow.direction, anchor.id, anchor.depth, x, y].join(SEP), () => ({
      id: `overflow:${key}`, type: "overflowMarker", className: "aq-stub-overflow",
      position: toPx(x, y + ctx.offsetY, ctx.density), ...sizePx(OVERFLOW_W, OVERFLOW_H, ctx.density),
      zIndex: 200 + anchor.depth, selectable: false, focusable: false, draggable: false,
      connectable: false,
      data: { label: `+${overflow.more} more`, direction: overflow.direction, anchorId: anchor.id },
    }));
  }
  const edges: Edge[] = [];
  for (const e of store.edges.values()) {
    const from = pos.get(e.from), to = pos.get(e.to);
    if (!from || !to) continue;
    const id = `${e.from}|${e.to}|${e.dep_type}`;
    const sig = [e.count ?? 1, from.x, from.y, to.x, to.y].join(SEP);
    const hit = reusable?.edges.get(id);
    if (hit && hit.sig === sig) {
      cache.edges.set(id, hit);
      edges.push(hit.edge);
      continue;
    }
    const vertical = Math.abs(from.y - to.y) > 0.5;
    // ``from`` is the dependent and ``to`` the blocker; the rendered edge
    // travels blocker → dependent, so compare them in that direction.
    const rightward = from.x >= to.x;
    const edge: Edge = {
      id, source: e.to, target: e.from, type: ctx.simpleEdges ? "straight" : "smoothstep",
      sourceHandle: vertical ? "out-bottom" : rightward ? "out-right" : "out-left",
      targetHandle: vertical ? "in-top" : rightward ? "in-left" : "in-right",
      label: ctx.simpleEdges || (e.count ?? 1) <= 1 ? undefined : `×${e.count}`,
      markerEnd: { type: MarkerType.ArrowClosed },
      style: edgeStyleForType(e.dep_type), data: { depType: e.dep_type },
    };
    cache.edges.set(id, { sig, edge });
    edges.push(edge);
  }
  return { nodes, edges, cache };
}
