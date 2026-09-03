import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type {
  ClusterBoundsDTO,
  EdgeOverlayDTO,
  GraphEdgeDTO,
  GraphLayoutDTO,
  GraphNodeDTO,
  GridPositionDTO,
  NodeOverlayDTO,
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
  TRAVERSED_EDGE_WIDTH,
  UNTRAVERSED_EDGE_OPACITY,
  type RuleClusterNodeData,
  type RunOverlayInput,
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
  /** True when a run overlay was supplied *and* it pinned this exact
   *  artifact, so its state was drawn. */
  overlayApplied: boolean;
  /** True when a run overlay was supplied and it pinned a *different*
   *  artifact, so nothing of it was drawn. The canvas says so out loud rather
   *  than silently showing a graph with no run state on it. */
  overlayMismatch: boolean;
}

export interface SemanticGraphInput {
  /** The artifact this projection is of. An overlay is only ever drawn on the
   *  artifact whose hash matches it. */
  artifact?: { artifact_sha256: string };
  nodes?: GraphNodeDTO[];
  edges?: GraphEdgeDTO[];
  rules?: RuleClusterDTO[];
  layout?: GraphLayoutDTO;
  diagnostics?: { rule_id?: string | null; step_id?: string | null }[];
}

const EMPTY: SemanticGraphLayoutResult = {
  nodes: [],
  edges: [],
  droppedEdgeCount: 0,
  overlayApplied: false,
  overlayMismatch: false,
};

/** Run state may be drawn only on the exact artifact the run executed.
 *
 *  A run pins its artifact; the projection carries the hash it was compiled
 *  from. Any drift between the two — a refetch still in flight, a run of an
 *  artifact that has since been superseded, a caller that forgot to pin —
 *  means the step and edge ids in the overlay are ids of a *different*
 *  program, and decorating this graph with them would assert an execution
 *  path that never happened. So the two hashes must be identical, and there
 *  is deliberately no partial or best-effort match. */
export function overlayAppliesTo(
  graph: SemanticGraphInput | undefined,
  overlay: RunOverlayInput | undefined,
): boolean {
  const executed = overlay?.artifact?.artifact_sha256;
  const projected = graph?.artifact?.artifact_sha256;
  return Boolean(executed && projected && executed === projected);
}

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
  overlay?: RunOverlayInput,
): SemanticGraphLayoutResult {
  const overlayApplied = overlayAppliesTo(graph, overlay);
  const overlayMismatch = Boolean(overlay) && !overlayApplied;
  const nodeOverlay = new Map<string, NodeOverlayDTO>(
    overlayApplied ? (overlay?.nodes ?? []).map((row) => [row.step_id, row]) : [],
  );
  const edgeOverlay = new Map<string, EdgeOverlayDTO>(
    overlayApplied ? (overlay?.edges ?? []).map((row) => [row.edge_id, row]) : [],
  );

  const apiNodes = graph?.nodes ?? [];
  if (apiNodes.length === 0) return { ...EMPTY, overlayApplied, overlayMismatch };

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
      data: { node, overlay: nodeOverlay.get(node.id), overlayApplied },
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
    const kindStyle = EDGE_KIND_STYLES[apiEdge.kind] ?? NEUTRAL_EDGE_STYLE;
    // The dash pattern is never touched by the overlay — weight and opacity
    // carry "was this taken", the dashes keep carrying the edge kind.
    const traversalCount = overlayApplied ? (edgeOverlay.get(apiEdge.id)?.traversal_count ?? 0) : 0;
    const traversed = traversalCount > 0;
    const style = !overlayApplied
      ? kindStyle
      : traversed
        ? { ...kindStyle, strokeWidth: TRAVERSED_EDGE_WIDTH, strokeOpacity: 1 }
        : { ...kindStyle, strokeOpacity: UNTRAVERSED_EDGE_OPACITY };
    const baseLabel = apiEdge.label || apiEdge.outcome;
    const runNote = !overlayApplied
      ? ""
      : traversed
        ? `, traversed ${traversalCount} time${traversalCount === 1 ? "" : "s"} in this run`
        : ", not traversed in this run";
    edges.push({
      id: apiEdge.id,
      source: apiEdge.source,
      target: apiEdge.target,
      sourceHandle: `out-${apiEdge.source_port}`,
      targetHandle: "in",
      // A loop that went round more than once says so on the edge itself; one
      // traversal adds no count, because "×1" reads as noise next to "×7".
      label: traversalCount > 1 ? `${baseLabel} ×${traversalCount}` : baseLabel,
      ariaLabel: `${kindLabel} edge from ${apiEdge.source} to ${apiEdge.target} on outcome ${apiEdge.outcome}${runNote}`,
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
      // Every transition record is one independently selectable edge. These
      // are per-edge flags on purpose: the canvas leaves `elementsSelectable`
      // off so the cards stay read-only, and xyflow resolves selection as
      // `edge.selectable || (elementsSelectable && edge.selectable === undefined)`,
      // so an explicit `true` here selects edges without selecting anything else.
      selectable: true,
      focusable: true,
      // Focusable edges get `tabIndex=0` and xyflow's Enter/Space/Escape
      // handling, which is a toggle button, not the default `group` role.
      ariaRole: "button",
      deletable: false,
      reconnectable: false,
      zIndex: traversed ? 3 : 2,
      data: {
        edgeKind: apiEdge.kind,
        outcome: apiEdge.outcome,
        condition: apiEdge.condition ?? null,
        traversalCount,
        traversed,
      },
    });
  }

  return {
    nodes: [...clusters, ...steps],
    edges,
    droppedEdgeCount,
    overlayApplied,
    overlayMismatch,
  };
}
