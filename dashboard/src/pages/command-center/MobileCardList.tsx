import type { MergedGraph } from "./types";

interface Props {
  graph: MergedGraph;
  onTaskClick: (taskId: string) => void;
}

const BUCKETS = ["IN_PROGRESS", "READY", "BLOCKED", "DEFINED", "FAILED", "COMPLETED"];

export default function MobileCardList({ graph, onTaskClick }: Props) {
  return (
    <div className="space-y-4 p-2">
      {BUCKETS.map((status) => {
        const tasks = graph.tasks.filter((t) => t.status === status);
        if (tasks.length === 0) return null;
        return (
          <section key={status}>
            <h3 className="mb-1 text-xs uppercase text-gray-400">
              {status} ({tasks.length})
            </h3>
            <ul className="space-y-1">
              {tasks.map((t) => (
                <li key={t.id}>
                  <button
                    type="button"
                    onClick={() => onTaskClick(t.id)}
                    className="w-full rounded border border-gray-800 bg-gray-900 p-2 text-left text-sm"
                  >
                    <div className="truncate">{t.title}</div>
                    <div className="text-xs text-gray-500">{t.profile_id ?? "—"}</div>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
