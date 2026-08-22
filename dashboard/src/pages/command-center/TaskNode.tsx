import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type { GraphGate, GraphTaskNode } from "./types";

export interface TaskNodeData extends Record<string, unknown> {
  task: GraphTaskNode;
  gates: GraphGate[];
  projectId: string;
}

type TaskNodeType = Node<TaskNodeData, "task">;

const STATUS_TONE: Record<string, string> = {
  DEFINED: "border-gray-600 bg-gray-900 text-gray-200",
  READY: "border-sky-500 bg-sky-950 text-sky-100",
  IN_PROGRESS: "border-indigo-500 bg-indigo-950 text-indigo-100",
  COMPLETED: "border-emerald-500 bg-emerald-950 text-emerald-100",
  FAILED: "border-red-500 bg-red-950 text-red-100",
  BLOCKED: "border-amber-500 bg-amber-950 text-amber-100",
};

function priorityBorderClass(p: number): string {
  if (p <= 20) return "ring-2 ring-red-400";
  if (p <= 50) return "ring-2 ring-amber-400";
  if (p <= 100) return "ring-1 ring-gray-500";
  return "";
}

function gateBadge(gate: GraphGate) {
  const label =
    gate.gate_type === "routing"
      ? "⏳"
      : gate.gate_type === "review" || gate.gate_type === "task"
        ? "\u{1F50D}"
        : gate.gate_type === "pr-merged"
          ? "\u{1F500}"
          : "❗";
  return (
    <span
      key={gate.id}
      title={`${gate.gate_type} — ${gate.status}`}
      className="ml-1 text-xs"
    >
      {label}
    </span>
  );
}

export default function TaskNode({ data, selected }: NodeProps<TaskNodeType>) {
  const { task, gates } = data;
  const tone = STATUS_TONE[task.status] ?? STATUS_TONE.DEFINED;
  const priority = task.priority ?? 100;
  const spinner =
    task.status === "IN_PROGRESS" ? (
      <span className="absolute -top-1 -right-1 h-3 w-3 animate-spin rounded-full border-2 border-indigo-400 border-t-transparent" />
    ) : null;
  const completedFlash =
    task.status === "COMPLETED" ? (
      <span className="absolute inset-0 -z-10 animate-pulse rounded bg-emerald-500/10" />
    ) : null;

  return (
    <div
      className={`relative rounded border p-2 text-xs shadow ${tone} ${priorityBorderClass(priority)} ${selected ? "outline outline-2 outline-white" : ""}`}
      style={{ width: 220, minHeight: 88 }}
    >
      {completedFlash}
      {spinner}
      <Handle type="target" position={Position.Top} />
      <div className="mb-1 flex items-center justify-between">
        <span className="font-mono text-[10px] opacity-70">{task.id.slice(0, 8)}</span>
        <span className="uppercase tracking-wide text-[9px]">{task.status}</span>
      </div>
      <div className="line-clamp-2 font-medium">{task.title}</div>
      <div className="mt-1 flex items-center gap-1 text-[10px] opacity-80">
        {task.profile_id && (
          <span className="rounded bg-white/5 px-1">{task.profile_id}</span>
        )}
        {task.intelligence_class && (
          <span className="rounded bg-white/5 px-1">{task.intelligence_class}</span>
        )}
        {gates.map(gateBadge)}
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
