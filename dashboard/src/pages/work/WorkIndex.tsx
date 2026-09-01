import { useState } from "react";
import { useProjects } from "../../api/hooks";
import WorkTasks from "./WorkTasks";
import WorkAgents from "./WorkAgents";

const STATUSES = [
  "PENDING",
  "READY",
  "IN_PROGRESS",
  "WAITING_INPUT",
  "COMPLETED",
  "FAILED",
  "BLOCKED",
  "CANCELED",
];

export default function WorkIndex() {
  const { data: projects } = useProjects();
  const [projectId, setProjectId] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<Set<string>>(new Set());
  const [showCompleted, setShowCompleted] = useState(false);

  const toggleStatus = (s: string) => {
    setStatusFilter((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  };

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Work</h1>
        <p className="text-sm text-gray-500">
          Everything the system is doing or waiting on. Filter to focus.
        </p>
      </header>

      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-gray-500" htmlFor="proj">
            Project:
          </label>
          <select
            id="proj"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className="rounded border border-gray-800 bg-gray-900 px-2 py-1 text-sm text-gray-200"
          >
            <option value="">All</option>
            {(projects ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name || p.id}
              </option>
            ))}
          </select>
          <label className="ml-4 flex items-center gap-1 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={showCompleted}
              onChange={(e) => setShowCompleted(e.target.checked)}
            />
            Show completed
          </label>
        </div>
        <div className="flex flex-wrap gap-1">
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

      <WorkTasks
        projectId={projectId || undefined}
        statusFilter={statusFilter}
        showCompleted={showCompleted}
      />
      <WorkAgents projectId={projectId || undefined} />
    </div>
  );
}
