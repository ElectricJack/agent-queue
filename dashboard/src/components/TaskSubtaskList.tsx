import { useId } from "react";
import { useTaskSubtasks } from "../api/taskSubtasks";

/** Read-only status glyph for one subtask row. */
const STATUS_GLYPH: Record<string, string> = {
  pending: "○", // ○
  in_progress: "◐", // ◐
  done: "☑", // ☑
  skipped: "⊘", // ⊘
};

/**
 * Read-only checklist for a task's durable subtasks (``task_subtasks``) --
 * distinct from the hierarchy's "Subtasks" section, which lists child tasks.
 * A single agent ticks these off while working the task; nothing here is
 * editable from the dashboard.
 */
export default function TaskSubtaskList({ taskId }: { taskId: string }) {
  return <TaskSubtaskListForTask key={taskId} taskId={taskId} />;
}

function TaskSubtaskListForTask({ taskId }: { taskId: string }) {
  const headingId = useId();
  const { data, isPending, isError, error, refetch } = useTaskSubtasks(taskId);
  const subtasks = data?.subtasks ?? [];

  if (isPending) return null;
  if (isError) {
    return (
      <section aria-labelledby={headingId} className="space-y-2">
        <h2 id={headingId} className="text-sm font-semibold uppercase text-gray-500">Checklist</h2>
        <div role="alert" className="rounded-lg border border-red-800 bg-red-950/30 p-3 text-sm text-red-300">
          Could not load the checklist. {error.message}
          <button type="button" onClick={() => refetch()} className="ml-2 underline">Retry</button>
        </div>
      </section>
    );
  }
  if (subtasks.length === 0) return null;

  return (
    <section aria-labelledby={headingId} className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <h2 id={headingId} className="text-sm font-semibold uppercase text-gray-500">Checklist</h2>
        <span className="text-xs text-gray-500">{data?.settled ?? 0} / {data?.total ?? subtasks.length} settled</span>
      </div>
      <ol className="space-y-1" aria-label="Subtask checklist">
        {subtasks.map((subtask) => (
          <li
            key={subtask.id}
            className="flex items-start gap-2 rounded-lg border border-gray-800 bg-gray-900 px-3 py-2 text-sm"
          >
            <span aria-hidden="true" className="mt-0.5 text-gray-400">
              {STATUS_GLYPH[subtask.status] ?? "○"}
            </span>
            <span className="min-w-0 flex-1">
              <span className="text-gray-200">
                <span className="text-gray-500">{subtask.ordinal}.</span> {subtask.title}
              </span>
              {subtask.note && (
                <span className="mt-0.5 block text-xs text-gray-400">{subtask.note}</span>
              )}
            </span>
            <span className="shrink-0 text-xs text-gray-500">{subtask.status}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
