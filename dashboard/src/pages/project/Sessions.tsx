import { Link, useParams } from "react-router-dom";
import { useSessions } from "../../api/hooks";

export default function ProjectSessions() {
  const { projectId = "" } = useParams();
  const { data: sessions = [], isLoading } = useSessions(projectId);

  return (
    <div className="space-y-4">
      {isLoading && <p className="text-sm text-gray-400">Loading…</p>}
      <div className="overflow-hidden rounded border border-gray-800">
        <table className="w-full text-sm">
          <thead className="bg-gray-900 text-left text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">Task</th>
              <th className="px-3 py-2">Harness</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Idle</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {sessions.map((s) => (
              <tr key={s.id} className="hover:bg-gray-900">
                <td className="px-3 py-2">
                  <Link
                    to={`/sessions/${s.id}`}
                    className="text-indigo-400 hover:text-indigo-300"
                  >
                    {s.name}
                  </Link>
                </td>
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
              </tr>
            ))}
            {sessions.length === 0 && !isLoading && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-gray-500">
                  No sessions for this project.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
