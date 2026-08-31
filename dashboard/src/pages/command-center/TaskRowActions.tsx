import { useEffect, useRef, useState } from "react";
import { PauseIcon, PlayIcon, StopIcon, ArrowPathIcon, CheckIcon, DocumentCheckIcon, ChatBubbleLeftIcon, TrashIcon } from "@heroicons/react/24/outline";
import { usePauseTask, useResumeTask, useStopTask, useRestartTask, useApproveTask, useApprovePlan, useEditTask, type Task } from "../../api/hooks";
import DeleteTaskModal from "../../components/DeleteTaskModal";
import { useShellPaneStore } from "../../panes/store";
import { TASK_STATUSES } from "./taskFilters";

const STATUS_OPTIONS = TASK_STATUSES;

function QuickAction({
  icon,
  title,
  onClick,
  variant = "default",
  disabled = false,
}: {
  icon: React.ReactNode;
  title: string;
  onClick: () => void;
  variant?: "default" | "success" | "danger";
  disabled?: boolean;
}) {
  const cls =
    variant === "success"
      ? "text-emerald-400 hover:bg-emerald-500/20"
      : variant === "danger"
        ? "text-red-400 hover:bg-red-500/20"
        : "text-gray-400 hover:bg-gray-700";
  return (
    <button type="button" disabled={disabled} aria-label={title} onClick={(event) => { event.stopPropagation(); onClick(); }} title={title} className={`rounded p-1 transition-colors ${cls}`}>
      {icon}
    </button>
  );
}

export function RowActions({ task }: { task: Task }) {
  const stopTask = useStopTask();
  const pauseTask = usePauseTask();
  const resumeTask = useResumeTask();
  const controlPending = pauseTask.isPending || resumeTask.isPending;
  const restartTask = useRestartTask();
  const approveTask = useApproveTask();
  const approvePlan = useApprovePlan();
  const pane = useShellPaneStore();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const s = task.status?.toUpperCase() ?? "";

  return (
    <>
      <div className="flex items-center gap-0.5">
        {["DEFINED", "READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED", "WAITING_INPUT", "AWAITING_APPROVAL", "AWAITING_PLAN_APPROVAL"].includes(s) && (
          <QuickAction icon={<PauseIcon className="h-3.5 w-3.5" />}
            title={pauseTask.isPending ? "Pausing…" : "Pause"} disabled={controlPending}
            onClick={() => { resumeTask.reset(); pauseTask.mutate({ task_id: task.id }); }} />
        )}
        {s === "PAUSED" && (
          <QuickAction icon={<PlayIcon className="h-3.5 w-3.5" />}
            title={resumeTask.isPending ? "Resuming…" : "Resume"} disabled={controlPending}
            onClick={() => { pauseTask.reset(); resumeTask.mutate({ task_id: task.id }); }} />
        )}
        {s === "IN_PROGRESS" && (
          <QuickAction
            icon={<StopIcon className="h-3.5 w-3.5" />}
            title="Stop"
            onClick={() => stopTask.mutate({ task_id: task.id })}
            variant="danger"
          />
        )}
        {s === "AWAITING_APPROVAL" && (
          <QuickAction
            icon={<CheckIcon className="h-3.5 w-3.5" />}
            title="Approve"
            onClick={() => approveTask.mutate({ task_id: task.id })}
            variant="success"
          />
        )}
        {s === "AWAITING_PLAN_APPROVAL" && (
          <QuickAction
            icon={<DocumentCheckIcon className="h-3.5 w-3.5" />}
            title="Approve Plan"
            onClick={() => approvePlan.mutate({ task_id: task.id })}
            variant="success"
          />
        )}
        {s === "WAITING_INPUT" && (
          <QuickAction
            icon={<ChatBubbleLeftIcon className="h-3.5 w-3.5" />}
            title="Answer (open detail)"
            onClick={() => pane.open("task-detail", { taskId: task.id })}
          />
        )}
        {["COMPLETED", "FAILED", "BLOCKED"].includes(s) && (
          <QuickAction
            icon={<ArrowPathIcon className="h-3.5 w-3.5" />}
            title="Restart"
            onClick={() => restartTask.mutate({ task_id: task.id })}
          />
        )}
        <QuickAction
          icon={<TrashIcon className="h-3.5 w-3.5" />}
          title="Delete"
          onClick={() => setDeleteOpen(true)}
          variant="danger"
        />
      </div>
      {(pauseTask.error || resumeTask.error) && <p role="alert" className="max-w-64 text-xs text-red-300">
        {(pauseTask.error || resumeTask.error)?.message}
      </p>}
      <DeleteTaskModal open={deleteOpen} onClose={() => setDeleteOpen(false)} task={task} />
    </>
  );
}

export function InlineStatus({ task }: { task: Task }) {
  const editTask = useEditTask();
  const current = task.status?.toUpperCase() ?? "";
  const tone = statusTone(current);
  const isPending =
    editTask.isPending && editTask.variables?.task_id === task.id;

  return (
    <select
      aria-label={`Status for ${task.title}`}
      value={STATUS_OPTIONS.includes(current) ? current : ""}
      onChange={(e) => {
        const next = e.target.value;
        if (!next || next === current) return;
        editTask.mutate({ task_id: task.id, status: next });
      }}
      disabled={isPending || current === "PAUSED"}
      onClick={(e) => e.stopPropagation()}
      title="Admin override — bypasses the state machine"
      className={`cursor-pointer rounded-full border-0 bg-transparent px-2 py-0.5 text-xs font-medium focus:ring-1 focus:ring-indigo-500 focus:outline-none disabled:opacity-50 ${tone}`}
    >
      {STATUS_OPTIONS.includes(current) ? null : <option value="">{current || "-"}</option>}
      {STATUS_OPTIONS.filter((s) => s !== "PAUSED" || current === "PAUSED").map((s) => (
        <option key={s} value={s}>
          {s}
        </option>
      ))}
    </select>
  );
}

export function InlinePriority({ task }: { task: Task }) {
  const editTask = useEditTask();
  const [draft, setDraft] = useState(String(task.priority ?? ""));
  const [focused, setFocused] = useState(false);
  const [conflict, setConflict] = useState<string | null>(null);
  const baseline = useRef(task.priority ?? null);
  const isPending = editTask.isPending && editTask.variables?.task_id === task.id;

  useEffect(() => {
    if (!focused && !isPending) setDraft(String(task.priority ?? ""));
  }, [task.priority, focused, isPending]);

  return (
    <div>
      <input
        type="number" step="1" aria-label={`Priority for ${task.title}`}
        value={draft} placeholder="-" disabled={isPending}
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => setDraft(e.target.value)}
        onFocus={() => { setFocused(true); baseline.current = task.priority ?? null; setConflict(null); }}
        onBlur={() => {
          setFocused(false);
          const raw = draft.trim();
          const next = raw === "" ? null : Number(raw);
          if (next === baseline.current) return;
          if (next !== null && !Number.isInteger(next)) {
            setConflict("Priority must be a whole number.");
            return;
          }
          if ((task.priority ?? null) !== baseline.current) {
            setConflict("Priority changed while editing. Please try again.");
            return;
          }
          editTask.mutate({ task_id: task.id, priority: next });
        }}
        className="w-16 rounded-md border border-transparent bg-transparent px-1.5 py-0.5 text-sm text-gray-300 hover:border-gray-700 focus:border-indigo-500 focus:bg-gray-950 focus:outline-none disabled:opacity-50"
      />
      {conflict && <p role="alert" className="mt-1 max-w-36 text-xs text-amber-300">{conflict}</p>}
    </div>
  );
}

function statusTone(status: string): string {
  switch (status) {
    case "IN_PROGRESS":
      return "bg-blue-500/10 text-blue-300";
    case "COMPLETED":
      return "bg-emerald-500/10 text-emerald-300";
    case "FAILED":
    case "BLOCKED":
      return "bg-red-500/10 text-red-300";
    case "AWAITING_APPROVAL":
    case "AWAITING_PLAN_APPROVAL":
    case "WAITING_INPUT":
      return "bg-amber-500/10 text-amber-300";
    case "DEFINED":
    case "ASSIGNED":
    case "PAUSED":
    case "PENDING":
    case "READY":
      return "bg-gray-500/10 text-gray-300";
    case "CANCELED":
      return "bg-gray-700/30 text-gray-500";
    default:
      return "bg-gray-700/30 text-gray-300";
  }
}
