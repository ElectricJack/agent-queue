import { Link, useParams } from "react-router-dom";
import { usePlaybooks, type PlaybookSummary } from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";

export default function ProjectPlaybooks() {
  const { projectId = "" } = useParams();
  const { data: playbooks, isLoading } = usePlaybooks();

  const rows = (playbooks ?? []).filter(
    (p) => p.scope === "project" && p.scope_identifier === projectId,
  );

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (rows.length === 0) {
    return (
      <p className="text-sm text-gray-500">
        No playbooks scoped to this project. System and agent-type playbooks may still apply —
        see <Link to="/system/playbooks" className="text-indigo-400 hover:underline">System Playbooks</Link>.
      </p>
    );
  }

  return <PlaybookTable rows={rows} />;
}

function PlaybookTable({ rows }: { rows: PlaybookSummary[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-4 py-3">ID</th>
            <th className="px-4 py-3">Triggers</th>
            <th className="px-4 py-3">Nodes</th>
            <th className="px-4 py-3">Version</th>
            <th className="px-4 py-3">Last run</th>
            <th className="px-4 py-3">Running</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {rows.map((p) => (
            <tr key={p.id} className="hover:bg-gray-900/50">
              <td className="px-4 py-3">
                <Link
                  to={`/playbooks/${encodeURIComponent(p.id)}`}
                  className="font-medium text-indigo-400 hover:underline"
                >
                  {p.id}
                </Link>
              </td>
              <td className="px-4 py-3">
                <div className="flex flex-wrap gap-1">
                  {p.triggers.length === 0 ? (
                    <span className="text-gray-500">—</span>
                  ) : (
                    p.triggers.map((t) => (
                      <span
                        key={t}
                        className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-300"
                      >
                        {t}
                      </span>
                    ))
                  )}
                </div>
              </td>
              <td className="px-4 py-3 text-gray-400">{p.node_count}</td>
              <td className="px-4 py-3 text-gray-400">v{p.version}</td>
              <td className="px-4 py-3">
                {p.last_run ? (
                  <StatusBadge status={p.last_run.status} />
                ) : (
                  <span className="text-gray-500">never</span>
                )}
              </td>
              <td className="px-4 py-3 text-gray-400">
                {p.running_count > 0 ? (
                  <span className="text-green-400">{p.running_count}</span>
                ) : (
                  <span className="text-gray-500">0</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
