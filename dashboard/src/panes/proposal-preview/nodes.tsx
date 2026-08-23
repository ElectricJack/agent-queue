import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type { ProposalNodeData } from "./graph";

type ProposalTaskNodeType = Node<ProposalNodeData, "proposalTask">;

export default function ProposalTaskNode({ data, selected }: NodeProps<ProposalTaskNodeType>) {
  const tone = data.ghost
    ? "border-gray-700 bg-gray-900/40 text-gray-500 border-dashed"
    : "border-indigo-500/60 bg-indigo-950/60 text-indigo-100";

  return (
    <div
      className={`relative rounded border p-2 text-xs shadow ${tone} ${
        selected ? "outline outline-2 outline-white" : ""
      }`}
      style={{ width: 160, minHeight: 52 }}
      data-testid="proposal-graph-node"
      data-ghost={data.ghost}
    >
      <Handle type="target" position={Position.Top} />
      {data.ghost ? (
        <>
          <div className="font-mono text-[10px] opacity-70">existing task</div>
          <div className="truncate font-mono">{data.title}</div>
        </>
      ) : (
        <>
          <div className="line-clamp-2 font-medium">{data.title}</div>
          {data.depCount > 0 && (
            <div className="mt-1 text-[10px] opacity-70">{data.depCount} dep(s)</div>
          )}
        </>
      )}
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
