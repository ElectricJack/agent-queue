import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type {
  PlaybookGraphLayout,
  PlaybookGraphNodesEdges,
  PlaybookGraphPosition,
} from "../../api/client";
import {
  COLUMN_GAP,
  EDGE_KIND_LABELS,
  EDGE_KIND_STYLES,
  NEUTRAL_EDGE_STYLE,
  NODE_HEIGHT,
  NODE_WIDTH,
  PADDING,
  PLAYBOOK_NODE_TYPE,
  ROW_GAP,
  type PlaybookGraphNodeData,
} from "./types";

export interface PlaybookGraphLayoutResult {
  nodes: Node<PlaybookGraphNodeData>[];
  edges: Edge[];
  /** Edges omitted because one endpoint is absent from the node list. */
  droppedEdgeCount: number;
}

const EMPTY: PlaybookGraphLayoutResult = { nodes: [], edges: [], droppedEdgeCount: 0 };

function toPixels(grid: PlaybookGraphPosition): { x: number; y: number } {
  return {
    x: PADDING + (grid.x ?? 0) * (NODE_WIDTH + COLUMN_GAP),
    y: PADDING + (grid.y ?? 0) * (NODE_HEIGHT + ROW_GAP),
  };
}

/** Pure layout: scale the backend's stable grid coordinates into pixels.
 *
 *  The backend remains the source of rank and order — this helper never
 *  reorders nodes or derives flow from prompt text or run state. Nodes without
 *  a grid entry fall back to their own `position`, then to a stable row keyed
 *  by their index in the backend-ordered list. */
export function layoutPlaybookGraph(
  graph: PlaybookGraphNodesEdges | undefined,
  layout: PlaybookGraphLayout | undefined,
): PlaybookGraphLayoutResult {
  const apiNodes = graph?.nodes ?? [];
  if (apiNodes.length === 0) return EMPTY;

  const grid = layout?.grid_positions ?? {};
  const nodes: Node<PlaybookGraphNodeData>[] = apiNodes.map((node, index) => ({
    id: node.id,
    type: PLAYBOOK_NODE_TYPE,
    position: toPixels(grid[node.id] ?? node.position ?? { x: 0, y: index }),
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    draggable: false,
    connectable: false,
    deletable: false,
    data: { node },
  }));

  const known = new Set(nodes.map((node) => node.id));
  const edges: Edge[] = [];
  let droppedEdgeCount = 0;
  (graph?.edges ?? []).forEach((apiEdge, index) => {
    if (!known.has(apiEdge.source) || !known.has(apiEdge.target)) {
      droppedEdgeCount += 1;
      return;
    }
    const kind = apiEdge.edge_type;
    const kindLabel = EDGE_KIND_LABELS[kind] ?? "transition";
    const style = EDGE_KIND_STYLES[kind] ?? NEUTRAL_EDGE_STYLE;
    const label = kind === "success" || kind === "failure"
      ? kindLabel
      : apiEdge.label || (kind === "goto" ? undefined : kindLabel);
    edges.push({
      id: `${index}:${apiEdge.source}->${apiEdge.target}:${kind}`,
      source: apiEdge.source,
      target: apiEdge.target,
      label,
      ariaLabel: `${kindLabel} edge from ${apiEdge.source} to ${apiEdge.target}`,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: style.stroke as string },
      style,
      labelBgPadding: [4, 2],
      labelBgBorderRadius: 3,
      labelBgStyle: { fill: "#0b1220", fillOpacity: 0.92 },
      labelStyle: { fill: "#e2e8f0", fontSize: 10 },
      selectable: false,
      focusable: false,
      deletable: false,
      reconnectable: false,
      data: { edgeType: kind },
    });
  });

  return { nodes, edges, droppedEdgeCount };
}
