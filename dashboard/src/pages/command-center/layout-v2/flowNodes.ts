import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type { GraphGate, LayoutNode } from "@aq/ts-client";
import { edgeStyleForType } from "../layout";
import type { ContainerNodeData, TaskNodeData } from "../types";
import type { LayoutStore } from "./layoutStore";
import { sizePx, toPx } from "./units";

export interface FlowHandlers { onOpenTask: (id: string) => void; onToggleChildren: (id: string) => void; onFocus: (id: string) => void }
export interface FlowContext { projectId: string; offsetY: number; expanded: ReadonlySet<string>; handlers: FlowHandlers }

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
  };
}

export function toFlowElements(store: LayoutStore, ctx: FlowContext): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const pos = new Map<string, { x: number; y: number }>();
  const gatesFor = (id: string) => store.gates.filter((g) => g.task_ids?.includes(id));
  for (const n of store.nodes.values()) {
    const position = toPx(n.x, n.y + ctx.offsetY);
    pos.set(n.id, { x: n.x, y: n.y });
    if (n.kind === "container") {
      const data: ContainerNodeData = { node: n, projectId: ctx.projectId, ...ctx.handlers };
      nodes.push({ id: n.id, type: "container", position, ...sizePx(n.w, n.h), zIndex: n.depth, selectable: false, draggable: false, connectable: false, data });
    } else {
      nodes.push({ id: n.id, type: "task", position, ...sizePx(1, 1), zIndex: 100 + n.depth, draggable: false, connectable: false, data: taskNodeData(n, ctx, gatesFor(n.id)) });
    }
  }
  for (const s of store.stubs.values()) {
    if (store.nodes.has(s.id)) continue;
    pos.set(s.id, { x: s.x, y: s.y });
    const stub: LayoutNode = { id: s.id, title: s.title ?? s.id, status: "PENDING", priority: 100, is_blocked: false, x: s.x, y: s.y, w: 1, h: 1, depth: 0,
      container_id: null, kind: "stub", context_only: true, agg_children: 0, agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0 } as LayoutNode;
    nodes.push({ id: s.id, type: "task", className: "aq-stub", position: toPx(s.x, s.y + ctx.offsetY), ...sizePx(1, 1), zIndex: 5, draggable: false, connectable: false, data: taskNodeData(stub, ctx, gatesFor(s.id)) });
  }
  const edges: Edge[] = [];
  for (const e of store.edges.values()) {
    const from = pos.get(e.from), to = pos.get(e.to);
    if (!from || !to) continue;
    const vertical = from.y > to.y + 0.5;
    edges.push({
      id: `${e.from}|${e.to}|${e.dep_type}`, source: e.to, target: e.from, type: "smoothstep",
      sourceHandle: vertical ? "out-bottom" : "out-right", targetHandle: vertical ? "in-top" : "in-left",
      label: (e.count ?? 1) > 1 ? `×${e.count}` : undefined, markerEnd: { type: MarkerType.ArrowClosed },
      style: edgeStyleForType(e.dep_type), data: { depType: e.dep_type },
    });
  }
  return { nodes, edges };
}
