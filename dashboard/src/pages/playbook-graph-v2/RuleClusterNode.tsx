import type { Node, NodeProps } from "@xyflow/react";
import { CLUSTER_HEADER, type RuleClusterNodeData } from "./types";

type RuleClusterNodeType = Node<RuleClusterNodeData, "ruleCluster">;

/** One rule, drawn as the box its steps live inside.
 *
 *  A rule owns a closed subgraph — no edge ever crosses `rule_id` — so the box
 *  is not decoration: it is the boundary the engine actually enforces. The node
 *  is non-interactive and sits below the step cards. */
export default function RuleClusterNode({ data, width, height }: NodeProps<RuleClusterNodeType>) {
  const { rule, diagnosticCount } = data;
  return (
    <div
      style={{ width, height }}
      aria-hidden={false}
      role="group"
      aria-label={`Rule ${rule.name} on ${rule.event_type}`}
      className="pointer-events-none rounded-lg border border-dashed border-gray-700 bg-gray-900/30"
    >
      <div
        style={{ height: CLUSTER_HEADER }}
        className="flex items-center gap-2 px-3 text-[11px] text-gray-300"
      >
        <span className="truncate font-semibold" title={rule.name}>
          {rule.name}
        </span>
        <span className="shrink-0 rounded bg-gray-800 px-1.5 py-0.5 font-mono text-[9px] text-gray-300">
          {rule.event_type}
        </span>
        {diagnosticCount > 0 && (
          <span className="shrink-0 rounded bg-amber-950 px-1.5 py-0.5 text-[9px] text-amber-200">
            {diagnosticCount} diagnostic{diagnosticCount === 1 ? "" : "s"}
          </span>
        )}
      </div>
    </div>
  );
}
