import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type {
  ClusterBoundsDTO,
  GraphEdgeDTO,
  GraphLayoutDTO,
  GraphNodeDTO,
  GridPositionDTO,
  RuleClusterDTO,
} from "../../api/client";
import {
  CLUSTER_HEADER,
  CLUSTER_PADDING,
  COLUMN_GAP,
  EDGE_KIND_LABELS,
  EDGE_KIND_STYLES,
  NEUTRAL_EDGE_STYLE,
  NODE_HEIGHT,
  NODE_WIDTH,
  PADDING,
  RULE_CLUSTER_NODE_TYPE,
  ROW_GAP,
  SEMANTIC_NODE_TYPE,
  type RuleClusterNodeData,
  type SemanticGraphNodeData,
} from "./types";

export type SemanticFlowNode = Node<SemanticGraphNodeData> | Node<RuleClusterNodeData>;

export interface SemanticGraphLayoutResult {
  /** Cluster group nodes first, then their steps — xyflow requires a parent to
   *  precede its children. */
  nodes: SemanticFlowNode[];
  edges: Edge[];
  /** Edges omitted because one endpoint is absent from the node list. */
  droppedEdgeCount: number;
}

export interface SemanticGraphInput {
  nodes?: GraphNodeDTO[];
  edges?: GraphEdgeDTO[];
  rules?: RuleClusterDTO[];
  layout?: GraphLayoutDTO;
  diagnostics?: { rule_id?: string | null; step_id?: string | null }[];
}

const EMPTY: SemanticGraphLayoutResult = { nodes: [], edges: [], droppedEdgeCount: 0 };

export function toPixels(grid: GridPositionDTO): { x: number; y: number } {
  return {
    x: PADDING + (grid.x ?? 0) * (NODE_WIDTH + COLUMN_GAP),
    y: PADDING + (grid.y ?? 0) * (NODE_HEIGHT + ROW_GAP),
  };
}

/** The pixel box of one rule cluster: the grid cells its steps occupy, grown
 *  by the cluster padding and a header strip for the rule's name. */
export function clusterPixelBounds(bounds: ClusterBoundsDTO): {
  x: number;
  y: number;
  width: number;
  height: number;
} {
  const origin = toPixels({ x: bounds.x, y: bounds.y });
  return {
    x: origin.x - CLUSTER_PADDING,
    y: origin.y - CLUSTER_HEADER,
    width: Math.max(1, bounds.width) * (NODE_WIDTH + COLUMN_GAP) - COLUMN_GAP + CLUSTER_PADDING * 2,
    height:
      Math.max(1, bounds.height) * (NODE_HEIGHT + ROW_GAP) -
      ROW_GAP +
      CLUSTER_HEADER +
      CLUSTER_PADDING,
  };
}

/** Pure layout: scale the backend's stable grid coordinates into pixels and
 *  parent every step to its rule cluster.
 *
 *  The backend owns rank and order. There is deliberately no dagre here — a
 *  client-side re-layout would make the rendered graph a second interpretation
 *  of the artifact, and the graph must be the artifact.
 *
 *  Edge ids are the DTO's artifact-derived ids, verbatim. That is what keeps
 *  two transitions between the same pair (a decision's case and its default)
 *  independently addressable, which V1's positional ids could not do. */
export function layoutSemanticGraph(
  graph: SemanticGraphInput | undefined,
): SemanticGraphLayoutResult {
  const apiNodes = graph?.nodes ?? [];
  if (apiNodes.length === 0) return EMPTY;

  const grid = graph?.layout?.grid_positions ?? {};
  const clusterBounds = graph?.layout?.cluster_bounds ?? {};
  const diagnostics = graph?.diagnostics ?? [];

  const stepRule = new Map(apiNodes.map((node) => [node.id, node.rule_id]));
  const clusters: Node<RuleClusterNodeData>[] = [];
  const clusterOrigin = new Map<string, { x: number; y: number }>();

  for (const rule of graph?.rules ?? []) {
    const bounds = clusterBounds[rule.rule_id];
    if (!bounds) continue;
    const box = clusterPixelBounds(bounds);
    clusterOrigin.set(rule.rule_id, { x: box.x, y: box.y });
    clusters.push({
      id: rule.rule_id,
      type: RULE_CLUSTER_NODE_TYPE,
      position: { x: box.x, y: box.y },
      width: box.width,
      height: box.height,
      draggable: false,
      connectable: false,
      deletable: false,
      selectable: false,
      focusable: false,
      zIndex: 0,
      data: {
        rule,
        diagnosticCount:
          (rule.diagnostics ?? []).length +
          diagnostics.filter(
            (d) =>
              (d.step_id != null && stepRule.get(d.step_id) === rule.rule_id) ||
              (d.step_id == null && d.rule_id === rule.rule_id),
          ).length,
      },
    });
  }

  const steps: Node<SemanticGraphNodeData>[] = apiNodes.map((node, index) => {
    const absolute = toPixels(grid[node.id] ?? node.position ?? { x: 0, y: index });
    const origin = clusterOrigin.get(node.rule_id);
    return {
      id: node.id,
      type: SEMANTIC_NODE_TYPE,
      ...(origin ? { parentId: node.rule_id } : {}),
      position: origin ? { x: absolute.x - origin.x, y: absolute.y - origin.y } : absolute,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      draggable: false,
      connectable: false,
      deletable: false,
      zIndex: 1,
      data: { node },
    };
  });

  const known = new Set(steps.map((step) => step.id));
  const edges: Edge[] = [];
  let droppedEdgeCount = 0;
  for (const apiEdge of graph?.edges ?? []) {
    if (!known.has(apiEdge.source) || !known.has(apiEdge.target)) {
      droppedEdgeCount += 1;
      continue;
    }
    const kindLabel = EDGE_KIND_LABELS[apiEdge.kind] ?? "transition";
    const style = EDGE_KIND_STYLES[apiEdge.kind] ?? NEUTRAL_EDGE_STYLE;
    edges.push({
      id: apiEdge.id,
      source: apiEdge.source,
      target: apiEdge.target,
      sourceHandle: `out-${apiEdge.source_port}`,
      targetHandle: "in",
      label: apiEdge.label || apiEdge.outcome,
      ariaLabel: `${kindLabel} edge from ${apiEdge.source} to ${apiEdge.target} on outcome ${apiEdge.outcome}`,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        width: 16,
        height: 16,
        color: style.stroke as string,
      },
      style,
      labelBgPadding: [4, 2],
      labelBgBorderRadius: 3,
      labelBgStyle: { fill: "#0b1220", fillOpacity: 0.92 },
      labelStyle: { fill: "#e2e8f0", fontSize: 10 },
      selectable: false,
      focusable: false,
      deletable: false,
      reconnectable: false,
      zIndex: 2,
      data: { edgeKind: apiEdge.kind, outcome: apiEdge.outcome, condition: apiEdge.condition ?? null },
    });
  }

  return { nodes: [...clusters, ...steps], edges, droppedEdgeCount };
}
