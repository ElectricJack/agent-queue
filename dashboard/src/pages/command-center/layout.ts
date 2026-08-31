import type { PlaybookSummary } from "../../api/hooks";
import type { PlaybookNodeData } from "./types";
import type { CSSProperties } from "react";
import { MarkerType, type Edge, type Node } from "@xyflow/react";
import { projectHierarchy, type HierarchyOptions, type HierarchyProjection, type ProjectedEdge } from "./hierarchy";
import { NODE_WIDTH, NODE_HEIGHT, type GraphTaskNode, type MergedGraph, type TaskNodeData } from "./types";

const COLUMN_GAP = 48;
const ROW_GAP = 64;
const PADDING = 32;

export function columnsForWidth(width: number): number {
  if (width <= 0) return 3;
  return Math.max(1, Math.min(4, Math.floor((width - PADDING * 2 + COLUMN_GAP) / (NODE_WIDTH + COLUMN_GAP))));
}

const ORDERED_RELATIONS = new Set(["blocks", "parent-child", "waits-for", "conditional-blocks", "discovered-from"]);

/** Stable dependency traversal: metadata/status changes cannot reshuffle
 *  cards. Independent arrivals append in the server's creation/priority order. */
function dependencyOrder(tasks: GraphTaskNode[], edges: ProjectedEdge[]): GraphTaskNode[] {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const rank = new Map(tasks.map((task, i) => [task.id, i]));
  const prerequisites = new Map<string, Set<string>>();
  for (const edge of edges) {
    if (!ORDERED_RELATIONS.has(edge.dep_type)) continue;
    if (!prerequisites.has(edge.from)) prerequisites.set(edge.from, new Set());
    prerequisites.get(edge.from)!.add(edge.to);
  }
  const seen = new Set<string>();
  const result: GraphTaskNode[] = [];
  function visit(id: string) {
    if (seen.has(id)) return;
    seen.add(id);
    const dependencies = [...(prerequisites.get(id) ?? [])]
      .sort((a, b) => (rank.get(a) ?? 0) - (rank.get(b) ?? 0));
    for (const dependency of dependencies) visit(dependency);
    const task = byId.get(id);
    if (task) result.push(task);
  }
  for (const task of tasks) visit(task.id);
  return result;
}

interface LayoutOptions extends HierarchyOptions {
  columns?: number;
  projection?: HierarchyProjection;
}

export function layoutGraph(
  graph: MergedGraph,
  options: LayoutOptions = {},
): { nodes: Node<TaskNodeData>[]; edges: Edge[] } {
  const projection = options.projection ?? projectHierarchy(graph, options);
  const columns = Math.max(1, Math.min(4, Math.floor(options.columns ?? 3)));
  const orderedTasks = dependencyOrder(projection.tasks, projection.edges);
  const gatesByTask = new Map<string, MergedGraph["gates"]>();
  for (const gate of graph.gates) {
    for (const id of gate.task_ids ?? []) {
      gatesByTask.set(id, [...(gatesByTask.get(id) ?? []), gate]);
    }
  }
  const nodes: Node<TaskNodeData>[] = orderedTasks.map((task, index) => ({
    id: task.id,
    type: "task",
    position: {
      x: PADDING + (index % columns) * (NODE_WIDTH + COLUMN_GAP),
      y: PADDING + Math.floor(index / columns) * (NODE_HEIGHT + ROW_GAP),
    },
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    style: {
      transitionProperty: "transform",
      transitionDuration: "200ms",
      transitionTimingFunction: "ease-out",
    },
    draggable: false,
    connectable: false,
    ariaRole: "group",
    ariaLabel: task.title,
    data: {
      task, gates: gatesByTask.get(task.id) ?? [],
      projectId: graph.taskProject[task.id] ?? "",
      hierarchy: projection.details.get(task.id)!,
    },
  }));
  const positions = new Map(nodes.map((node) => [node.id, node.position]));
  const edges: Edge[] = projection.edges.map((edge) => {
    const sourcePosition = positions.get(edge.to)!;
    const targetPosition = positions.get(edge.from)!;
    const vertical = sourcePosition.y !== targetPosition.y;
    const style = edgeStyleForType(edge.dep_type);
    return {
      id: JSON.stringify([edge.to, edge.from, edge.dep_type]),
      source: edge.to,
      target: edge.from,
      sourceHandle: vertical ? "out-bottom" : "out-right",
      targetHandle: vertical ? "in-top" : "in-left",
      type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed, color: String(style.stroke), width: 18, height: 18 },
      label: edge.dep_type + (edge.count > 1 ? ` ×${edge.count}` : ""),
      labelStyle: { fill: "#d1d5db", fontSize: 9 },
      labelBgStyle: { fill: "#111827", fillOpacity: 0.95 },
      labelBgPadding: [4, 2],
      style,
      data: { depType: edge.dep_type, count: edge.count, remapped: edge.remapped },
      ariaLabel: `${edge.dep_type}: ${edge.to} to ${edge.from}${edge.remapped ? " (collapsed tasks)" : ""}`,
    };
  });
  return { nodes, edges };
}

export function edgeStyleForType(depType: string): CSSProperties {
  switch (depType) {
    case "blocks": return { stroke: "#818cf8", strokeWidth: 2 };
    case "parent-child": return { stroke: "#a3a3a3", strokeWidth: 1.5, strokeDasharray: "4 4" };
    case "waits-for": return { stroke: "#fbbf24", strokeWidth: 2 };
    case "conditional-blocks": return { stroke: "#fb923c", strokeWidth: 1.5, strokeDasharray: "6 3" };
    case "discovered-from": return { stroke: "#6b7280", strokeWidth: 1.5, strokeDasharray: "2 4" };
    default: return { stroke: "#9ca3af", strokeWidth: 1 };
  }
}

/** Keep recurring definitions above task rows; task filters never remove them. */
export function prependPlaybookRows(
  taskNodes: Node<TaskNodeData>[], playbooks: PlaybookSummary[], columns: number,
): Node<TaskNodeData | PlaybookNodeData>[] {
  const offset = Math.ceil(playbooks.length / columns) * (NODE_HEIGHT + ROW_GAP);
  return [
    ...playbooks.map((playbook, index) => ({
      id: `playbook:${playbook.id}`, type: "playbook",
      position: { x: PADDING + (index % columns) * (NODE_WIDTH + COLUMN_GAP), y: PADDING + Math.floor(index / columns) * (NODE_HEIGHT + ROW_GAP) },
      width: NODE_WIDTH, height: NODE_HEIGHT, data: { playbook },
      draggable: false, connectable: false,
    })),
    ...taskNodes.map(node => ({ ...node, position: { ...node.position, y: node.position.y + offset } })),
  ];
}
