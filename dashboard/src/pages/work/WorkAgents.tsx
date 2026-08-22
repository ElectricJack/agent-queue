import { useAllAgents, useProjects } from "../../api/hooks";

interface Props {
  projectId?: string;
}

export default function WorkAgents({ projectId }: Props) {
  const { data: projects } = useProjects();
  const ids = (projects ?? []).map((p) => p.id);
  const { data: agents = [], isLoading } = useAllAgents(ids);

  const filtered = projectId ? agents.filter((a) => a.project_id === projectId) : agents;

  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold">Agents ({filtered.length})</h2>
      <div className="overflow-x-auto rounded border border-gray-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-900 text-xs uppercase text-gray-500">
            <tr>
              <th className="px-3 py-2">Name</th>
              <th className="px-3 py-2">State</th>
              <th className="px-3 py-2">Project</th>
              <th className="px-3 py-2">Task</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {isLoading && (
              <tr>
                <td colSpan={4} className="px-3 py-4 text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && filtered.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-4 text-center text-gray-500">
                  No agents.
                </td>
              </tr>
            )}
            {filtered.map((a) => (
              <tr key={`${a.project_id}:${a.name}`} className="hover:bg-gray-900/50">
                <td className="px-3 py-2 text-gray-200">{a.name}</td>
                <td className="px-3 py-2 text-gray-300">{a.state}</td>
                <td className="px-3 py-2 text-gray-400">{a.project_id}</td>
                <td className="px-3 py-2 text-gray-400">{a.current_task_id ?? "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
