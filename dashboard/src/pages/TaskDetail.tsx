import { useParams, Link, useLocation } from "react-router-dom";
import { ArrowLeftIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useTask, type TaskRef } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";
import TaskActions from "../components/TaskActions";

export default function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  const location = useLocation();
  const { data: task, isLoading } = useTask(taskId ?? "");

  if (isLoading) return <p className="p-6 text-sm text-gray-500">Loading...</p>;
  if (!task) return <p className="p-6 text-sm text-gray-500">Task not found.</p>;

  const agent = task.assigned_agent;
  const from = (location.state as { from?: string } | null)?.from ?? "/system";
  const backLabel = labelForBack(from);

  return (
    <div className="space-y-6">
      <Link
        to={from}
        className="inline-flex items-center gap-1 text-sm text-gray-400 hover:text-gray-200"
      >
        <ArrowLeftIcon className="h-4 w-4" /> {backLabel}
      </Link>

      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold">{task.title}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <StatusBadge status={task.status} />
          {task.project_id && (
            <span className="text-sm text-gray-400">{task.project_id}</span>
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

      {/* Actions */}
      <TaskActions task={task} />

      {/* Description */}
      {task.description && (
        <section>
          <h2 className="mb-2 text-sm font-semibold uppercase text-gray-500">Description</h2>
          <div className="whitespace-pre-wrap rounded-lg border border-gray-800 bg-gray-900 p-4 text-sm text-gray-300">
            {task.description}
          </div>
        </section>
      )}

      {/* Metadata grid */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase text-gray-500">Details</h2>
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4 text-sm sm:grid-cols-3">
          <Field label="Agent" value={agent ?? "-"} />
          <Field label="Profile" value={task.profile_id ?? "default"} />
          <Field label="Retries" value={`${task.retry_count ?? 0} / ${task.max_retries ?? 3}`} />
          <Field label="Requires Approval" value={task.requires_approval ? "Yes" : "No"} />
          <Field label="Auto-approve Plan" value={task.auto_approve_plan ? "Yes" : "No"} />
          <Field label="Created" value={formatDate(task.created_at)} />
          <Field label="Updated" value={formatDate(task.updated_at)} />
          {task.parent_task_id && (
            <div>
              <span className="text-gray-500">Parent Task</span>
              <p>
                <Link
                  to={`/tasks/${task.parent_task_id}`}
                  state={{ from }}
                  className="text-indigo-400 hover:underline"
                >
                  {task.parent_task_id}
                </Link>
              </p>
            </div>
          )}
        </div>
      </section>

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
    </div>
  );
}

/**
 * Pick a back-button label that matches where the user came from.
 * Falls back to "Back" when the source path is unfamiliar.
 */
function labelForBack(from: string): string {
  if (from.match(/^\/projects\/[^/]+\/tasks$/)) return "Back to tasks";
  if (from.match(/^\/projects\/[^/]+\/?$/)) return "Back to project";
  if (from === "/system") return "Back to overview";
  if (from === "/system/events") return "Back to events";
  if (from.startsWith("/tasks/")) return "Back";
  return "Back";
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-gray-500">{label}</span>
      <p className="text-gray-300">{value}</p>
    </div>
  );
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
              to={`/tasks/${ref.id}`}
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
    // Daemon returns unix-epoch floats; ISO strings are also accepted.
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return date.toLocaleString();
  } catch {
    return String(value);
  }
}
