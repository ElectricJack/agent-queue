import { Link } from "react-router-dom";
import { useActiveTasksAllProjects } from "../../api/hooks";

interface Props {
  projectId?: string;
  statusFilter?: Set<string>;
  showCompleted: boolean;
}

export default function WorkTasks({ projectId, statusFilter, showCompleted }: Props) {
  const { data: tasks = [], isLoading } = useActiveTasksAllProjects();

  const filtered = tasks.filter((t) => {
    if (projectId && t.project_id !== projectId) return false;
    if (statusFilter && statusFilter.size > 0 && !statusFilter.has((t.status ?? "").toUpperCase()))
      return false;
    if (!showCompleted && ["COMPLETED", "CANCELED"].includes((t.status ?? "").toUpperCase()))
      return false;
    return true;
  });

  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">Tasks ({filtered.length})</h2>
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
              <tr key={t.id} className="hover:bg-gray-900/50">
                <td className="px-3 py-2">
                  <Link to={`/tasks/${t.id}`} className="text-indigo-400 hover:underline">
                    {t.title}
                  </Link>
                </td>
                <td className="px-3 py-2 text-gray-400">{t.project_id}</td>
                <td className="px-3 py-2 text-gray-300">{t.status}</td>
                <td className="px-3 py-2 text-gray-400">{t.priority ?? "-"}</td>
                <td className="px-3 py-2 text-gray-400">{t.assigned_agent ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
