import { Link } from "react-router-dom";
import { useExplainTask, useTaskDeps, type TaskDepsResponse } from "../../api/hooks";

interface Props {
  taskId: string;
  from: string;
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === "COMPLETED"
      ? "bg-emerald-500/10 text-emerald-400"
      : status === "IN_PROGRESS"
        ? "bg-indigo-500/10 text-indigo-400"
        : status === "BLOCKED"
          ? "bg-amber-500/10 text-amber-400"
          : status === "FAILED"
            ? "bg-red-500/10 text-red-400"
            : "bg-gray-800 text-gray-300";
  return <span className={`rounded px-2 py-0.5 text-xs ${tone}`}>{status || "?"}</span>;
}

function TaskList({
  title,
  items,
  from,
}: {
  title: string;
  items: TaskDepsResponse["depends_on"];
  from: string;
}) {
  return (
    <div className="rounded border border-gray-800 bg-gray-950 p-3">
      <h3 className="mb-2 text-sm font-semibold text-gray-300">
        {title} <span className="text-xs text-gray-500">({items?.length ?? 0})</span>
      </h3>
      {!items || items.length === 0 ? (
        <p className="text-xs text-gray-500">None.</p>
      ) : (
        <ul className="space-y-1 text-sm">
          {items.map((t) => (
            <li key={t.id} className="flex items-center justify-between gap-2">
              <Link
                to={`/tasks/${encodeURIComponent(t.id)}`}
                state={{ from }}
                className="truncate font-mono text-indigo-400 hover:text-indigo-300"
              >
                {t.id}
              </Link>
              <span className="flex-1 truncate text-gray-400">{t.title}</span>
              <StatusPill status={t.status ?? ""} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function TaskGraph({ taskId, from }: Props) {
  const { data, isLoading, error } = useTaskDeps(taskId);
  if (isLoading) return <p className="text-sm text-gray-400">Loading graph…</p>;
  if (error) return <p className="text-sm text-red-400">{(error as Error).message}</p>;
  if (!data) return null;
  return (
    <div className="grid gap-4 md:grid-cols-2">
      <TaskList title="Depends on (upstream)" items={data.depends_on ?? []} from={from} />
      <TaskList title="Blocks (downstream)" items={data.blocks ?? []} from={from} />
    </div>
  );
}

export function TaskExplain({ taskId }: { taskId: string }) {
  const { data, isLoading, error } = useExplainTask(taskId);
  if (isLoading) return <p className="text-sm text-gray-400">Loading reasons…</p>;
  if (error) return <p className="text-sm text-red-400">{(error as Error).message}</p>;
  if (!data) return null;
  const reasons = data.reasons ?? [];
  if (reasons.length === 0) {
    return (
      <p className="text-sm text-emerald-400">
        No blockers — this task is ready or already running.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {reasons.map((r, idx) => (
        <li
          key={`${r.code}-${idx}`}
          className="rounded border border-gray-800 bg-gray-950 p-3 text-sm"
        >
          <div className="mb-1 flex items-center gap-2">
            <span className="rounded bg-amber-500/10 px-2 py-0.5 text-xs text-amber-400">
              {r.code}
            </span>
            {r.ref && <span className="font-mono text-xs text-gray-500">ref: {r.ref}</span>}
          </div>
          <p className="text-gray-300">{r.detail}</p>
        </li>
      ))}
    </ul>
  );
}
