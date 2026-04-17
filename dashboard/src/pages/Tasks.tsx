import { useState } from "react";
import { Link } from "react-router-dom";
import {
  PlusIcon,
  StopIcon,
  ArrowPathIcon,
  CheckIcon,
  DocumentCheckIcon,
  ChatBubbleLeftIcon,
} from "@heroicons/react/24/outline";
import {
  useTasks,
  useProjects,
  useStopTask,
  useRestartTask,
  useApproveTask,
  useApprovePlan,
  type Task,
} from "../api/hooks";
import StatusBadge from "../components/StatusBadge";
import CreateTaskModal from "../components/CreateTaskModal";

export default function Tasks() {
  const { data: projects } = useProjects();
  const [activeTab, setActiveTab] = useState<string>("");
  const [showCompleted, setShowCompleted] = useState(
    () => localStorage.getItem("tasks:showCompleted") === "true",
  );
  const [createOpen, setCreateOpen] = useState(false);

  const toggleShowCompleted = (v: boolean) => {
    setShowCompleted(v);
    localStorage.setItem("tasks:showCompleted", String(v));
  };

  const projectList = projects ?? [];
  const selectedProjectId = activeTab || projectList[0]?.id || "";

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Tasks</h1>
        <button
          onClick={() => setCreateOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          <PlusIcon className="h-4 w-4" />
          Create Task
        </button>
      </div>

      {/* Tab bar with toggle on the right */}
      {projectList.length > 0 && (
        <div className="flex items-center justify-between border-b border-gray-800">
          <div className="flex gap-1">
            {projectList.map((p) => (
              <button
                key={p.id}
                onClick={() => setActiveTab(p.id)}
                className={`px-4 py-2 text-sm font-medium transition-colors ${
                  selectedProjectId === p.id
                    ? "border-b-2 border-indigo-400 text-indigo-400"
                    : "text-gray-400 hover:text-gray-200"
                }`}
              >
                {p.name || p.id}
              </button>
            ))}
          </div>

          {/* Toggle switch */}
          <button
            role="switch"
            aria-checked={showCompleted}
            onClick={() => toggleShowCompleted(!showCompleted)}
            className="flex items-center gap-2 pb-1 text-sm text-gray-400"
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
        </div>
      )}

      {selectedProjectId ? (
        <TaskTable projectId={selectedProjectId} showCompleted={showCompleted} />
      ) : (
        <p className="text-sm text-gray-500">No projects found.</p>
      )}

      <CreateTaskModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        defaultProjectId={selectedProjectId}
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
  const s = task.status?.toUpperCase() ?? "";

  return (
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
          onClick={() => window.location.assign(`/tasks/${task.id}`)}
        />
      )}
      {["COMPLETED", "FAILED", "BLOCKED"].includes(s) && (
        <QuickAction
          icon={<ArrowPathIcon className="h-3.5 w-3.5" />}
          title="Restart"
          onClick={() => restartTask.mutate({ task_id: task.id })}
        />
      )}
    </div>
  );
}

function TaskTable({ projectId, showCompleted }: { projectId: string; showCompleted: boolean }) {
  const { data: tasks, isLoading } = useTasks(projectId, { showAll: showCompleted });

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
            <th className="px-4 py-3 w-24"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {tasks.map((task) => (
            <tr key={task.id} className="hover:bg-gray-900/50">
              <td className="max-w-sm px-4 py-3">
                <Link
                  to={`/tasks/${task.id}`}
                  className="font-medium text-indigo-400 hover:underline"
                >
                  {task.title}
                </Link>
              </td>
              <td className="px-4 py-3">
                <StatusBadge status={task.status} />
              </td>
              <td className="px-4 py-3 text-gray-400">
                {task.priority != null ? `P${task.priority}` : "-"}
              </td>
              <td className="px-4 py-3 text-gray-400">{task.agent_name ?? "-"}</td>
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
