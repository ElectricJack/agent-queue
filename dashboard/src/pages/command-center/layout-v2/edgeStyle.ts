import type { CSSProperties } from "react";

/** Dependency edges are told apart by colour and dash, not by label alone. */
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
