import { ChevronDownIcon, ChevronRightIcon, ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import { NODE_HEIGHT, NODE_WIDTH, type TaskNodeData } from "./types";
import { isTaskBlocked } from "./hierarchy";

export type { TaskNodeData } from "./types";
type TaskNodeType = Node<TaskNodeData, "task">;

const STATUS_TONE: Record<string, string> = {
  DEFINED: "border-gray-600 bg-gray-900 text-gray-200",
  PENDING: "border-gray-600 bg-gray-900 text-gray-200",
  READY: "border-sky-500 bg-sky-950 text-sky-100",
  ASSIGNED: "border-indigo-500 bg-indigo-950 text-indigo-100",
  PAUSED: "border-amber-500 bg-amber-950 text-amber-100",
  IN_PROGRESS: "border-indigo-500 bg-indigo-950 text-indigo-100",
  COMPLETED: "border-emerald-500 bg-emerald-950 text-emerald-100",
  FAILED: "border-red-500 bg-red-950 text-red-100",
  BLOCKED: "border-amber-500 bg-amber-950 text-amber-100",
  WAITING_INPUT: "border-amber-500 bg-amber-950 text-amber-100",
  AWAITING_APPROVAL: "border-amber-500 bg-amber-950 text-amber-100",
  AWAITING_PLAN_APPROVAL: "border-amber-500 bg-amber-950 text-amber-100",
  CANCELLED: "border-gray-700 bg-gray-900 text-gray-400",
  CANCELED: "border-gray-700 bg-gray-900 text-gray-400",
};

interface CardProps {
  data: TaskNodeData;
  selected?: boolean;
  fluid?: boolean;
}

/** The task action and expansion action are sibling buttons, so every part
 *  of the card remains clickable without nested interactive elements. */
export function TaskCard({ data, selected = false, fluid = false }: CardProps) {
  const { task, gates, hierarchy, onOpenTask, onToggleChildren } = data;
  const blocked = isTaskBlocked(task);
  const tone = STATUS_TONE[blocked ? "BLOCKED" : task.status] ?? STATUS_TONE.DEFINED;
  const priority = task.priority ?? 100;
  const urgent = priority <= 20 ? "ring-2 ring-red-400" : priority <= 50 ? "ring-1 ring-amber-400" : "";
  const cannotToggle = hierarchy.autoExpanded || hierarchy.visibleChildCount === 0;
  const toggleHelp = hierarchy.autoExpanded
    ? "Matching descendants are shown while filters are active."
    : hierarchy.visibleChildCount === 0
      ? "All children are hidden by the current filters."
      : undefined;
  const openGates = gates.filter((gate) => gate.status.toLowerCase() === "open");

  return (
    <div
      data-task-card
      className={`relative flex flex-col rounded-md border text-xs shadow ${tone} ${urgent} ${hierarchy.contextOnly ? "border-dashed" : ""} ${selected ? "outline outline-2 outline-white" : ""}`}
      style={{ width: fluid ? "100%" : NODE_WIDTH, height: NODE_HEIGHT }}
    >
      <button
        type="button"
        aria-label={`Open task ${task.title}`}
        aria-pressed={selected}
        data-task-id={task.id}
        className="nodrag nopan flex min-h-0 flex-1 cursor-pointer flex-col overflow-hidden rounded-md p-2 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300"
        onClick={(event) => {
          if (onOpenTask) {
            event.stopPropagation();
            onOpenTask(task.id);
          }
        }}
      >
        <span className="flex w-full items-center justify-between gap-1">
          <span className="truncate font-mono text-[10px] opacity-70" title={task.id}>{task.id.slice(0, 8)}</span>
          <span className="flex shrink-0 items-center gap-1 text-[9px] tracking-wide" title={blocked ? `${task.status} · blocked by dependencies or gates` : task.status}>
            {task.status === "IN_PROGRESS" && <span aria-hidden className="h-2 w-2 animate-pulse rounded-full bg-indigo-300 motion-reduce:animate-none" />}
            {task.status.replace(/_/g, " ")}
            {blocked && task.status !== "BLOCKED" && <ExclamationTriangleIcon aria-label="Blocked by dependencies or gates" className="h-3 w-3 text-amber-300" />}
          </span>
        </span>
        {hierarchy.parentTitle && (
          <span className="mt-0.5 w-full truncate text-[9px] opacity-60" title={hierarchy.parentTitle}>
            ↳ {hierarchy.parentTitle}
          </span>
        )}
        <span className="mt-1 line-clamp-2 w-full font-medium leading-4" title={task.title}>{task.title}</span>
        <span className="mt-1 flex w-full items-center gap-1 overflow-hidden text-[10px] opacity-80">
          {task.profile_id && <span className="truncate rounded bg-white/5 px-1" title={task.profile_id}>{task.profile_id}</span>}
          {task.intelligence_class && <span className="truncate rounded bg-white/5 px-1" title={task.intelligence_class}>{task.intelligence_class}</span>}
          {openGates.length > 0 && <span className="shrink-0" title={openGates.map((gate) => gate.gate_type).join(", ")}>{openGates.length} gate{openGates.length === 1 ? "" : "s"}</span>}
        </span>
        {hierarchy.descendantCount > 0 && (
          <span className="mt-auto block w-full pt-1 text-[10px]">
            <span>{hierarchy.completedCount}/{hierarchy.descendantCount} descendants completed</span>
            <span
              role="progressbar"
              aria-label={`Child completion for ${task.title}`}
              aria-valuemin={0}
              aria-valuemax={hierarchy.descendantCount}
              aria-valuenow={hierarchy.completedCount}
              className="mt-0.5 block h-1 overflow-hidden rounded bg-white/10"
            >
              <span className="block h-full bg-emerald-400" style={{ width: `${hierarchy.completedCount / hierarchy.descendantCount * 100}%` }} />
            </span>
          </span>
        )}
      </button>
      {hierarchy.childCount > 0 && (
        <button
          type="button"
          aria-label={`${hierarchy.expanded ? "Collapse" : "Expand"} children of ${task.title}`}
          aria-expanded={hierarchy.expanded}
          disabled={cannotToggle}
          title={toggleHelp}
          className="nodrag nopan flex h-7 shrink-0 items-center gap-1 rounded-b-md border-t border-white/10 px-2 text-[10px] hover:bg-white/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300 disabled:cursor-default disabled:opacity-60"
          onClick={(event) => { event.stopPropagation(); onToggleChildren?.(task.id); }}
          onKeyDown={(event) => { if (event.key !== "Escape") event.stopPropagation(); }}
        >
          {hierarchy.expanded ? <ChevronDownIcon aria-hidden className="h-3 w-3" /> : <ChevronRightIcon aria-hidden className="h-3 w-3" />}
          <span>{hierarchy.childCount} {hierarchy.childCount === 1 ? "child" : "children"}</span>
          {hierarchy.runningCount > 0 && <span className="ml-auto text-indigo-300">{hierarchy.runningCount} running</span>}
          {hierarchy.blockedCount > 0 && <span className="ml-auto text-amber-300">{hierarchy.blockedCount} blocked</span>}
        </button>
      )}
    </div>
  );
}

export default function TaskNode({ data, selected }: NodeProps<TaskNodeType>) {
  return (
    <>
      <Handle id="in-left" type="target" position={Position.Left} isConnectable={false} />
      <Handle id="in-top" type="target" position={Position.Top} isConnectable={false} />
      <TaskCard data={data} selected={selected} />
      <Handle id="out-right" type="source" position={Position.Right} isConnectable={false} />
      <Handle id="out-bottom" type="source" position={Position.Bottom} isConnectable={false} />
    </>
  );
}
