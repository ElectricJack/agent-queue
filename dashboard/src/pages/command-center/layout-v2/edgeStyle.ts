import type { CSSProperties } from "react";

/** §2.3: one neutral stroke for every dependency edge, `--g-edge` at 1.5 px;
 *  the type is told by dash pattern, not colour, and the legend strip names
 *  the four. `discovered-from` is provenance, so it also loses its
 *  arrowhead in `flowNodes`. */
const EDGE_STROKE = "var(--g-edge)";

export function edgeStyleForType(depType: string): CSSProperties {
  switch (depType) {
    case "blocks": return { stroke: EDGE_STROKE, strokeWidth: 1.5 };
    case "parent-child": return { stroke: EDGE_STROKE, strokeWidth: 1.5, strokeDasharray: "4 4" };
    case "waits-for": return { stroke: EDGE_STROKE, strokeWidth: 1.5, strokeDasharray: "10 4" };
    case "conditional-blocks": return { stroke: EDGE_STROKE, strokeWidth: 1.5, strokeDasharray: "6 3" };
    case "discovered-from": return { stroke: EDGE_STROKE, strokeWidth: 1.5, strokeDasharray: "2 4" };
    default: return { stroke: EDGE_STROKE, strokeWidth: 1.5 };
  }
}
