import { useState } from "react";
import { useParams, Link, useLocation } from "react-router-dom";
import { ArrowLeftIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useTask, type TaskRef } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";
import TaskActions from "../components/TaskActions";
import TaskComments from "../components/TaskComments";
import TaskSessions from "../components/TaskSessions";
import TaskAttention from "../components/TaskAttention";
import TaskDescription from "../components/TaskDescription";
import TaskFieldsEditor, { ReadField, type EditableTask } from "../components/TaskFieldsEditor";
import TaskGraph, { TaskExplain } from "./task/TaskGraph";
import { workspaceHref } from "../shell/projectNavigation";

export default function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  return <TaskDetailContent key={taskId} taskId={taskId ?? ""} />;
}

function TaskDetailContent({ taskId }: { taskId: string }) {
  const location = useLocation();
  const { data: task, isLoading } = useTask(taskId ?? "");

  const [activeTab, setActiveTab] = useState<"details" | "explain" | "graph">("details");

  if (isLoading) return <p className="p-6 text-sm text-gray-500">Loading...</p>;
  if (!task) return <p className="p-6 text-sm text-gray-500">Task not found.</p>;

  const from = (location.state as { from?: string } | null)?.from ?? workspaceHref(task.project_id, "tasks");
  const backLabel = labelForBack(from);

  return (
    <div className="h-full overflow-y-auto p-6 space-y-6">
      <Link
        to={from}
        className="inline-flex items-center gap-1 text-sm text-gray-400 hover:text-gray-200"
      >
        <ArrowLeftIcon className="h-4 w-4" /> {backLabel}
      </Link>

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-bold">{task.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <StatusBadge status={task.status} />
            {task.project_id && (
              <Link to={workspaceHref(task.project_id, "tasks")} className="text-sm text-indigo-400 hover:underline">{task.project_id}</Link>
            )}
            {task.priority != null && (
              <span className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-300">
                P{task.priority}
              </span>
            )}
            {task.task_type && (
              <span className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-300">
                {task.task_type}
              </span>
            )}
            {task.is_plan_subtask && (
              <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-xs text-indigo-400">
                subtask
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Actions */}
      <TaskActions task={task} />

      <TaskAttention task={task as typeof task & { needs_attention?: string | null }} />

      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-800">
        {(
          [
            { id: "details", label: "Details" },
            { id: "explain", label: "Explain" },
            { id: "graph", label: "Graph" },
          ] as const
        ).map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setActiveTab(t.id)}
            className={`px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === t.id
                ? "border-b-2 border-indigo-400 text-indigo-400"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === "explain" && taskId && <TaskExplain taskId={taskId} />}
      {activeTab === "graph" && taskId && <TaskGraph taskId={taskId} from={from} />}

      {activeTab === "details" && (
        <>
      <TaskDescription key={task.id} task={task} />

      <TaskSessions taskId={task.id} />

      <TaskComments taskId={task.id} />

      {/* Metadata grid */}
      <TaskFieldsEditor key={`fields-${task.id}`} task={task as EditableTask}>
        <ReadField label="Created" value={formatDate(task.created_at)} />
        <ReadField label="Updated" value={formatDate(task.updated_at)} />
        {task.parent_task_id && (
          <div>
            <span className="text-gray-500">Parent Task</span>
            <p>
              <Link
                to={`/tasks/${encodeURIComponent(task.parent_task_id)}`}
                state={{ from }}
                className="text-indigo-400 hover:underline"
              >
                {task.parent_task_id}
              </Link>
            </p>
          </div>
        )}
      </TaskFieldsEditor>

      {/* PR link */}
      {task.pr_url && (
        <section>
          <h2 className="mb-2 text-sm font-semibold uppercase text-gray-500">Pull Request</h2>
          <a
            href={task.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-indigo-400 hover:underline"
          >
            {task.pr_url} <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
          </a>
        </section>
      )}

      {/* Subtasks */}
      {task.subtasks && task.subtasks.length > 0 && (
        <TaskRefList title="Subtasks" items={task.subtasks} from={from} />
      )}

      {/* Dependencies */}
      {task.depends_on && task.depends_on.length > 0 && (
        <TaskRefList title="Depends On" items={task.depends_on} from={from} />
      )}

      {/* Blocks */}
      {task.blocks && task.blocks.length > 0 && (
        <TaskRefList title="Blocks" items={task.blocks} from={from} />
      )}
        </>
      )}
    </div>
  );
}

/**
 * Pick a back-button label that matches where the user came from.
 * Falls back to "Back" when the source path is unfamiliar.
 */
function labelForBack(from: string): string {
  if (from.match(/^\/(projects\/[^/]+|command-center)\/tasks/)) return "Back to tasks";
  if (from.match(/^\/projects\/[^/]+\/?$/)) return "Back to project";
  if (from.startsWith("/tasks/")) return "Back";
  return "Back";
}

function TaskRefList({ title, items, from }: { title: string; items: TaskRef[]; from: string }) {
  return (
    <section>
      <h2 className="mb-2 text-sm font-semibold uppercase text-gray-500">{title}</h2>
      <div className="space-y-1">
        {items.map((ref) => (
          <div
            key={ref.id}
            className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 px-4 py-2"
          >
            <Link
              to={`/tasks/${encodeURIComponent(ref.id)}`}
              state={{ from }}
              className="truncate text-sm font-medium text-indigo-400 hover:underline"
            >
              {ref.title}
            </Link>
            <StatusBadge status={ref.status} />
          </div>
        ))}
      </div>
    </section>
  );
}

function formatDate(value?: string | number | null): string {
  if (value == null) return "-";
  try {
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return date.toLocaleString();
  } catch {
    return String(value);
  }
}
