import { useState } from "react";
import { useActiveTasksAllProjects, useProjects } from "../../api/hooks";
import { useShellPaneStore } from "../../panes/store";

const STATUSES = [
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

export default function CommandCenterTasks() {
  const { data: projects } = useProjects();
  const { data: tasks = [], isLoading } = useActiveTasksAllProjects();
  const [projectId, setProjectId] = useState("");
  const [statusFilter, setStatusFilter] = useState<Set<string>>(new Set());
  const [showCompleted, setShowCompleted] = useState(false);
  const pane = useShellPaneStore();

  const toggleStatus = (s: string) => {
    setStatusFilter((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  };

  const filtered = tasks.filter((t) => {
    if (projectId && t.project_id !== projectId) return false;
    if (statusFilter.size > 0 && !statusFilter.has((t.status ?? "").toUpperCase()))
      return false;
    if (!showCompleted && ["COMPLETED", "CANCELED"].includes((t.status ?? "").toUpperCase()))
      return false;
    return true;
  });

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
          className="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
        >
          <option value="">All projects</option>
          {(projects ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name || p.id}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1 text-xs text-gray-500">
          <input
            type="checkbox"
            checked={showCompleted}
            onChange={(e) => setShowCompleted(e.target.checked)}
          />
          Show completed
        </label>
        <div className="ml-auto flex flex-wrap gap-1">
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => toggleStatus(s)}
              className={`rounded-full px-2 py-0.5 text-xs ${
                statusFilter.has(s)
                  ? "bg-indigo-500/20 text-indigo-300"
                  : "bg-gray-800 text-gray-400 hover:bg-gray-700"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto rounded border border-gray-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-900 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Title</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Priority</th>
              <th className="px-3 py-2">Agent</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {isLoading && (
              <tr>
                <td colSpan={5} className="px-3 py-4 text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && filtered.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-4 text-center text-gray-500">
                  No matching tasks.
                </td>
              </tr>
            )}
            {filtered.map((t) => (
              <TaskRow
                key={t.id}
                task={t}
                onOpen={() => pane.open("task-detail", { taskId: t.id })}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function TaskRow({
  task,
  onOpen,
}: {
  task: {
    id: string;
    title?: string;
    project_id?: string;
    status?: string;
    priority?: number | null;
    assigned_agent?: string | null;
  };
  onOpen: () => void;
}) {
  // Entity shortcut vocab (o r d y .) is handled by keydown on focused row.
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTableRowElement>) => {
    switch (e.key) {
      case "o":
      case "Enter":
        e.preventDefault();
        onOpen();
        break;
      case "r":
      case "d":
      case "y":
      case ".":
        // Reserved for restart / details / copy / focus per shell spec.
        e.preventDefault();
        onOpen();
        break;
      default:
        break;
    }
  };
  return (
    <tr
      tabIndex={0}
      onKeyDown={handleKeyDown}
      onClick={onOpen}
      className="cursor-pointer hover:bg-gray-900/50 focus:bg-gray-900/50 focus:outline-none"
    >
      <td className="px-3 py-2 text-indigo-400">{task.title}</td>
      <td className="px-3 py-2 text-gray-400">{task.project_id}</td>
      <td className="px-3 py-2 text-gray-300">{task.status}</td>
      <td className="px-3 py-2 text-gray-400">{task.priority ?? "-"}</td>
      <td className="px-3 py-2 text-gray-400">{task.assigned_agent ?? "-"}</td>
    </tr>
  );
}
