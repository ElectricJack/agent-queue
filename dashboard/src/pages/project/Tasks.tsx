import { useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  PlusIcon,
  StopIcon,
  ArrowPathIcon,
  CheckIcon,
  DocumentCheckIcon,
  ChatBubbleLeftIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import {
  useTasks,
  useStopTask,
  useRestartTask,
  useApproveTask,
  useApprovePlan,
  useEditTask,
  type Task,
} from "../../api/hooks";
import CreateTaskModal from "../../components/CreateTaskModal";
import DeleteTaskModal from "../../components/DeleteTaskModal";

const STATUS_OPTIONS = [
  "PENDING",
  "READY",
  "IN_PROGRESS",
  "AWAITING_APPROVAL",
  "AWAITING_PLAN_APPROVAL",
  "WAITING_INPUT",
  "COMPLETED",
  "FAILED",
  "BLOCKED",
  "CANCELED",
];

export default function ProjectTasks() {
  const { projectId = "" } = useParams();
  const [showCompleted, setShowCompleted] = useState(
    () => localStorage.getItem("tasks:showCompleted") === "true",
  );
  const [createOpen, setCreateOpen] = useState(false);

  const toggleShowCompleted = (v: boolean) => {
    setShowCompleted(v);
    localStorage.setItem("tasks:showCompleted", String(v));
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <button
          role="switch"
          aria-checked={showCompleted}
          onClick={() => toggleShowCompleted(!showCompleted)}
          className="flex items-center gap-2 text-sm text-gray-400"
        >
          <span>Show completed</span>
          <span
            className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full transition-colors duration-200 ${
              showCompleted ? "bg-indigo-500" : "bg-gray-700"
            }`}
          >
            <span
              className={`pointer-events-none inline-block h-4 w-4 translate-y-0.5 rounded-full bg-white shadow ring-0 transition-transform duration-200 ${
                showCompleted ? "translate-x-4.5" : "translate-x-0.5"
              }`}
            />
          </span>
        </button>
        <button
          onClick={() => setCreateOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          <PlusIcon className="h-4 w-4" />
          Create Task
        </button>
      </div>

      <TaskTable projectId={projectId} showCompleted={showCompleted} />

      <CreateTaskModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        defaultProjectId={projectId}
      />
    </div>
  );
}

function QuickAction({
  icon,
  title,
  onClick,
  variant = "default",
}: {
  icon: React.ReactNode;
  title: string;
  onClick: () => void;
  variant?: "default" | "success" | "danger";
}) {
  const cls =
    variant === "success"
      ? "text-emerald-400 hover:bg-emerald-500/20"
      : variant === "danger"
        ? "text-red-400 hover:bg-red-500/20"
        : "text-gray-400 hover:bg-gray-700";
  return (
    <button onClick={onClick} title={title} className={`rounded p-1 transition-colors ${cls}`}>
      {icon}
    </button>
  );
}

function RowActions({ task }: { task: Task }) {
  const stopTask = useStopTask();
  const restartTask = useRestartTask();
  const approveTask = useApproveTask();
  const approvePlan = useApprovePlan();
  const navigate = useNavigate();
  const location = useLocation();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const s = task.status?.toUpperCase() ?? "";

  return (
    <>
      <div className="flex items-center gap-0.5">
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
            onClick={() => navigate(`/tasks/${task.id}`, { state: { from: location.pathname } })}
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
      <DeleteTaskModal open={deleteOpen} onClose={() => setDeleteOpen(false)} task={task} />
    </>
  );
}

function InlineStatus({ task }: { task: Task }) {
  const editTask = useEditTask();
  const current = task.status?.toUpperCase() ?? "";
  const tone = statusTone(current);
  const isPending =
    editTask.isPending && editTask.variables?.task_id === task.id;

  return (
    <select
      value={STATUS_OPTIONS.includes(current) ? current : ""}
      onChange={(e) => {
        const next = e.target.value;
        if (!next || next === current) return;
        editTask.mutate({ task_id: task.id, status: next });
      }}
      disabled={isPending}
      onClick={(e) => e.stopPropagation()}
      title="Admin override — bypasses the state machine"
      className={`cursor-pointer rounded-full border-0 bg-transparent px-2 py-0.5 text-xs font-medium focus:ring-1 focus:ring-indigo-500 focus:outline-none disabled:opacity-50 ${tone}`}
    >
      {STATUS_OPTIONS.includes(current) ? null : <option value="">{current || "-"}</option>}
      {STATUS_OPTIONS.map((s) => (
        <option key={s} value={s}>
          {s}
        </option>
      ))}
    </select>
  );
}

function InlinePriority({ task }: { task: Task }) {
  const editTask = useEditTask();
  const isPending =
    editTask.isPending && editTask.variables?.task_id === task.id;

  return (
    <input
      type="number"
      defaultValue={task.priority ?? ""}
      placeholder="-"
      disabled={isPending}
      onClick={(e) => e.stopPropagation()}
      onBlur={(e) => {
        const raw = e.target.value.trim();
        const next = raw === "" ? null : parseInt(raw, 10);
        if (next === task.priority || (next == null && task.priority == null)) return;
        if (next != null && !Number.isFinite(next)) return;
        editTask.mutate({ task_id: task.id, priority: next });
      }}
      className="w-14 rounded-md border border-transparent bg-transparent px-1.5 py-0.5 text-sm text-gray-300 hover:border-gray-700 focus:border-indigo-500 focus:bg-gray-950 focus:outline-none disabled:opacity-50"
    />
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
    case "PENDING":
    case "READY":
      return "bg-gray-500/10 text-gray-300";
    case "CANCELED":
      return "bg-gray-700/30 text-gray-500";
    default:
      return "bg-gray-700/30 text-gray-300";
  }
}

function TaskTable({ projectId, showCompleted }: { projectId: string; showCompleted: boolean }) {
  const { data: tasks, isLoading } = useTasks(projectId, { showAll: showCompleted });
  const location = useLocation();

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (!tasks?.length) return <p className="text-sm text-gray-500">No tasks in this project.</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-4 py-3">Title</th>
            <th className="px-4 py-3">Status</th>
            <th className="px-4 py-3">Priority</th>
            <th className="px-4 py-3">Agent</th>
            <th className="w-24 px-4 py-3"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {tasks.map((task) => (
            <tr key={task.id} className="hover:bg-gray-900/50">
              <td className="max-w-sm px-4 py-3">
                <Link
                  to={`/tasks/${task.id}`}
                  state={{ from: location.pathname }}
                  className="font-medium text-indigo-400 hover:underline"
                >
                  {task.title}
                </Link>
              </td>
              <td className="px-4 py-3">
                <InlineStatus task={task} />
              </td>
              <td className="px-4 py-3">
                <InlinePriority task={task} />
              </td>
              <td className="px-4 py-3 text-gray-400">{task.assigned_agent ?? "-"}</td>
              <td className="px-4 py-3">
                <RowActions task={task} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
