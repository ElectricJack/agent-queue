import { Link } from "react-router-dom";
import { XMarkIcon, ArrowTopRightOnSquareIcon } from "@heroicons/react/24/outline";
import { useTask } from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
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

  return (
    <aside className="fixed inset-x-0 bottom-0 z-40 flex max-h-[75vh] flex-col overflow-y-auto border-t border-gray-800 bg-gray-950 p-4 md:right-0 md:top-0 md:bottom-0 md:left-auto md:max-h-none md:w-[420px] md:border-l md:border-t-0">
      <header className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-mono text-xs text-gray-500">{taskId}</p>
          <h2 className="truncate text-lg font-semibold text-gray-100">
            {task?.title ?? "Loading…"}
          </h2>
          <StatusBadge status={task?.status ?? ""} />
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-gray-400 hover:text-gray-200"
        >
          <XMarkIcon className="h-5 w-5" />
        </button>
      </header>

      {task?.description && (
        <section className="mb-3 whitespace-pre-wrap rounded border border-gray-800 bg-gray-900 p-2 text-sm text-gray-300">
          {task.description}
        </section>
      )}

      <section className="mb-3 flex flex-wrap gap-1 text-xs">
        {task?.profile_id && (
          <span className="rounded bg-gray-800 px-2 py-0.5">{task.profile_id}</span>
        )}
        {intelligenceClass && (
          <span className="rounded bg-gray-800 px-2 py-0.5">{intelligenceClass}</span>
        )}
      </section>

      {taskGates.length > 0 && (
        <section className="mb-3">
          <h3 className="mb-1 text-xs uppercase text-gray-400">Gates</h3>
          <ul className="space-y-1">
            {taskGates.map((g) => (
              <li
                key={g.id}
                className="flex items-center justify-between rounded border border-gray-800 bg-gray-900 p-2 text-sm"
              >
                <span>
                  {g.gate_type} <span className="text-xs text-gray-500">{g.status}</span>
                </span>
                {g.gate_type === "human" && g.status === "open" && (
                  <span className="flex gap-1">
                    <button
                      onClick={() => onResolveGate(g.id, "approve")}
                      className="rounded bg-emerald-600 px-2 py-0.5 text-xs"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => onResolveGate(g.id, "reject")}
                      className="rounded bg-red-600 px-2 py-0.5 text-xs"
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

      {(task?.depends_on ?? []).length > 0 && (
        <section className="mb-3">
          <h3 className="mb-1 text-xs uppercase text-gray-400">Depends on</h3>
          <ul className="space-y-0.5 text-xs">
            {task!.depends_on!.map((d) => (
              <li key={d.id}>
                <Link to={`/tasks/${d.id}`} className="font-mono text-indigo-400">
                  {d.id}
                </Link>
                <span className="ml-2 text-gray-400">{d.title}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {task?.pr_url && (
        <a
          href={task.pr_url}
          target="_blank"
          rel="noreferrer"
          className="mb-3 inline-flex items-center gap-1 text-sm text-indigo-400"
        >
          PR <ArrowTopRightOnSquareIcon className="h-4 w-4" />
        </a>
      )}

      <Link
        to={`/tasks/${taskId}`}
        className="mt-auto text-center text-sm text-indigo-400"
      >
        Open full task detail →
      </Link>
    </aside>
  );
}
