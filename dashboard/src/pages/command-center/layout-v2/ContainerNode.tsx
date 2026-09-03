import { memo } from "react";
import { ChevronDownIcon, MagnifyingGlassPlusIcon } from "@heroicons/react/24/outline";
import { Handle, Position } from "@xyflow/react";
import type { ContainerNodeData } from "../types";
import { UNIT_H } from "./units";

export interface ContainerNodeProps { id: string; data: ContainerNodeData; selected?: boolean }

function ContainerNode({ data, selected }: ContainerNodeProps) {
  const { node, onFocus, onToggleChildren, onOpenTask } = data;
  const headerPx = 0.35 * UNIT_H * (data.layoutScale ?? 1);
  return (
    <div data-container-id={node.id} className={`h-full w-full rounded-lg border border-white/15 bg-white/[0.03] ${selected ? "outline outline-2 outline-white" : ""} ${node.context_only ? "border-dashed" : ""}`}>
      <Handle id="in-left" type="target" position={Position.Left} isConnectable={false} />
      <Handle id="in-right" type="target" position={Position.Right} isConnectable={false} />
      <Handle id="in-top" type="target" position={Position.Top} isConnectable={false} />
      <div className="flex items-center gap-2 px-2 text-[11px] text-gray-200" style={{ height: headerPx }}>
        <button type="button" aria-label={`Open task ${node.title}`} data-task-id={node.id}
          className="nodrag nopan min-w-0 flex-1 truncate text-left font-medium hover:underline"
          onClick={(e) => { e.stopPropagation(); onOpenTask?.(node.id, { id: node.id, playbook_run_id: node.playbook_run_id }); }}>{node.title}</button>
        <span className="shrink-0 text-[9px] uppercase tracking-wide opacity-70">{node.status.replace(/_/g, " ")}</span>
        <span className="shrink-0 rounded bg-white/10 px-1">{node.agg_completed}/{node.agg_descendants} done</span>
        {(node.agg_running ?? 0) > 0 && <span className="shrink-0 text-indigo-300">{node.agg_running} running</span>}
        {(node.agg_blocked ?? 0) > 0 && <span className="shrink-0 text-amber-300">{node.agg_blocked} blocked</span>}
        <button type="button" aria-label={`Focus on ${node.title}`} className="nodrag nopan rounded p-0.5 hover:bg-white/10"
          onClick={(e) => { e.stopPropagation(); onFocus?.(node.id); }}><MagnifyingGlassPlusIcon className="h-3.5 w-3.5" /></button>
        <button type="button" aria-label={`Collapse children of ${node.title}`} aria-expanded={true} className="nodrag nopan rounded p-0.5 hover:bg-white/10"
          onClick={(e) => { e.stopPropagation(); onToggleChildren?.(node.id); }}><ChevronDownIcon className="h-3.5 w-3.5" /></button>
      </div>
      <Handle id="out-left" type="source" position={Position.Left} isConnectable={false} />
      <Handle id="out-right" type="source" position={Position.Right} isConnectable={false} />
      <Handle id="out-bottom" type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

/** Its `data` keeps its identity while the container is unchanged, so a
 *  re-rendered `NodeWrapper` costs nothing here. */
export default memo(ContainerNode);
