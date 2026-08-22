import { Link } from "react-router-dom";
import { XMarkIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useTask, type Task, type TaskRef } from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import TaskActions from "../../components/TaskActions";
import type { GraphGate } from "./types";

interface Props {
  taskId: string | null;
  gates: GraphGate[];
  onResolveGate: (gateId: string, decision: "approve" | "reject") => void;
  onClose: () => void;
}

export default function TaskSidebar({ taskId, gates, onResolveGate, onClose }: Props) {
  const { data: task } = useTask(taskId ?? "");
  if (!taskId) return null;
  const taskGates = gates.filter((g) => (g.task_ids ?? []).includes(taskId));
  const intelligenceClass = (task as { intelligence_class?: string } | undefined)
    ?.intelligence_class;
  const branchName = (task as { branch_name?: string } | undefined)?.branch_name;

  return (
    <aside className="fixed inset-x-0 bottom-0 z-40 flex max-h-[80vh] flex-col overflow-y-auto border-t border-gray-800 bg-gray-950 md:right-0 md:top-0 md:bottom-0 md:left-auto md:max-h-none md:w-[640px] md:border-l md:border-t-0 lg:w-[720px]">
      <header className="sticky top-0 z-10 flex items-start justify-between gap-2 border-b border-gray-800 bg-gray-950/95 p-4 backdrop-blur">
        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-xs text-gray-500">{taskId}</p>
          <h2 className="mt-0.5 truncate text-lg font-semibold text-gray-100">
            {task?.title ?? "Loading…"}
          </h2>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            {task?.status && <StatusBadge status={task.status} />}
            {task?.project_id && (
              <span className="text-gray-400">{task.project_id}</span>
            )}
            {task?.priority != null && (
              <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
                P{task.priority}
              </span>
            )}
            {task?.task_type && (
              <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
                {task.task_type}
              </span>
            )}
            {task?.profile_id && (
              <span className="rounded bg-gray-800 px-2 py-0.5 text-gray-300">
                {task.profile_id}
              </span>
            )}
            {intelligenceClass && (
              <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-300">
                {intelligenceClass}
              </span>
            )}
            {task?.is_plan_subtask && (
              <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-indigo-400">
                subtask
              </span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-gray-400 hover:text-gray-200"
          aria-label="Close"
        >
          <XMarkIcon className="h-5 w-5" />
        </button>
      </header>

      <div className="space-y-4 p-4">
        {task && <TaskActions task={task} />}

        {task?.description ? (
          <section>
            <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">
              Description
            </h3>
            <div className="whitespace-pre-wrap rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm text-gray-300">
              {task.description}
            </div>
          </section>
        ) : null}

        <section>
          <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">
            Details
          </h3>
          <div className="grid grid-cols-1 gap-x-4 gap-y-2 rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm sm:grid-cols-2">
            <MetaField label="Agent" value={task?.assigned_agent ?? "—"} />
            <MetaField
              label="Retries"
              value={`${task?.retry_count ?? 0} / ${task?.max_retries ?? 3}`}
            />
            <MetaField
              label="Requires approval"
              value={task?.requires_approval ? "Yes" : "No"}
            />
            <MetaField
              label="Auto-approve plan"
              value={task?.auto_approve_plan ? "Yes" : "No"}
            />
            <MetaField
              label="Skip verification"
              value={task?.skip_verification ? "Yes" : "No"}
            />
            <MetaField label="Branch" value={branchName ?? "—"} mono />
            <MetaField label="Created" value={formatDate(task?.created_at)} />
            <MetaField label="Updated" value={formatDate(task?.updated_at)} />
            {task?.parent_task_id && (
              <div>
                <span className="text-xs text-gray-500">Parent task</span>
                <p className="mt-0.5">
                  <Link
                    to={`/tasks/${task.parent_task_id}`}
                    className="font-mono text-xs text-indigo-400 hover:underline"
                  >
                    {task.parent_task_id}
                  </Link>
                </p>
              </div>
            )}
          </div>
        </section>

        {task?.pr_url && (
          <section>
            <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">
              Pull request
            </h3>
            <a
              href={task.pr_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-sm text-indigo-400 hover:underline"
            >
              {task.pr_url} <ArrowTopRightOnSquareIcon className="h-3.5 w-3.5" />
            </a>
          </section>
        )}

        {taskGates.length > 0 && (
          <section>
            <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">
              Gates
            </h3>
            <ul className="space-y-1.5">
              {taskGates.map((g) => (
                <li
                  key={g.id}
                  className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 p-2.5 text-sm"
                >
                  <span className="text-gray-300">
                    {g.gate_type}{" "}
                    <span className="text-xs text-gray-500">{g.status}</span>
                  </span>
                  {g.gate_type === "human" && g.status === "open" && (
                    <span className="flex gap-1.5">
                      <button
                        onClick={() => onResolveGate(g.id, "approve")}
                        className="rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-500"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => onResolveGate(g.id, "reject")}
                        className="rounded bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-500"
                      >
                        Reject
                      </button>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        {(task?.subtasks ?? []).length > 0 && (
          <TaskRefSection title="Subtasks" items={task!.subtasks!} />
        )}

        {(task?.depends_on ?? []).length > 0 && (
          <TaskRefSection title="Depends on" items={task!.depends_on!} />
        )}

        {(task?.blocks ?? []).length > 0 && (
          <TaskRefSection title="Blocks" items={task!.blocks!} />
        )}

        <div className="border-t border-gray-800 pt-3 text-center">
          <Link
            to={`/tasks/${taskId}`}
            className="inline-flex items-center gap-1 text-sm text-indigo-400 hover:underline"
          >
            Open full task page →
          </Link>
        </div>
      </div>
    </aside>
  );
}

function MetaField({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <span className="text-xs text-gray-500">{label}</span>
      <p
        className={`truncate text-gray-300 ${mono ? "font-mono text-xs" : ""}`}
        title={value}
      >
        {value}
      </p>
    </div>
  );
}

function TaskRefSection({ title, items }: { title: string; items: TaskRef[] }) {
  return (
    <section>
      <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">
        {title}
      </h3>
      <ul className="space-y-1">
        {items.map((r) => (
          <li
            key={r.id}
            className="flex items-center justify-between gap-2 rounded-lg border border-gray-800 bg-gray-900 px-3 py-2 text-sm"
          >
            <Link
              to={`/tasks/${r.id}`}
              className="min-w-0 flex-1 truncate text-indigo-400 hover:underline"
              title={r.title}
            >
              {r.title}
            </Link>
            <StatusBadge status={r.status} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function formatDate(value?: string | number | null): string {
  if (value == null) return "—";
  try {
    const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return d.toLocaleString();
  } catch {
    return String(value);
  }
}

// Suppress unused type import when Task inference already picks it up.
export type { Task };
