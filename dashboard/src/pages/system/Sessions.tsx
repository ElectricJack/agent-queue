import { Link, useLocation } from "react-router-dom";
import { useSessions } from "../../api/hooks";

export default function SystemSessions() {
  const location = useLocation();
  const { data: sessions = [], isLoading, error } = useSessions();

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-bold">Sessions</h1>
        <p className="text-sm text-gray-500">
          Every running or recent agent session across all projects.
        </p>
      </header>

      {isLoading && <p className="text-sm text-gray-400">Loading…</p>}
      {error && (
        <p className="text-sm text-red-400">
          Failed to load sessions: {(error as Error).message}
        </p>
      )}

      <div className="overflow-hidden rounded border border-gray-800">
        <table className="w-full text-sm">
          <thead className="bg-gray-900 text-left text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Task</th>
              <th className="px-3 py-2">Harness</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Idle</th>
              <th className="px-3 py-2">Restarts</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {sessions.map((s) => (
              <tr key={s.id} className="hover:bg-gray-900">
                <td className="px-3 py-2">
                  <Link
                    to={`/sessions/${encodeURIComponent(s.id)}`}
                    state={{ from: location.pathname + location.search }}
                    className="text-indigo-400 hover:text-indigo-300"
                  >
                    {s.name}
                  </Link>
                </td>
                <td className="px-3 py-2 text-gray-400">{s.project_id ?? "—"}</td>
                <td className="px-3 py-2 text-gray-400">{s.task_id ?? "—"}</td>
                <td className="px-3 py-2 text-gray-400">{s.harness ?? "—"}</td>
                <td className="px-3 py-2">
                  <span
                    className={`rounded px-2 py-0.5 text-xs ${
                      s.stalled ? "bg-amber-500/10 text-amber-400" : "bg-gray-800 text-gray-300"
                    }`}
                  >
                    {s.state ?? "?"}
                  </span>
                </td>
                <td className="px-3 py-2 text-gray-400">
                  {Math.round(s.idle_seconds ?? 0)}s
                </td>
                <td className="px-3 py-2 text-gray-400">{s.restarts ?? 0}</td>
              </tr>
            ))}
            {sessions.length === 0 && !isLoading && (
              <tr>
                <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
                  No sessions.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
