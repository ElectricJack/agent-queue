import { useAllAgents, useProjects } from "../api/hooks";
import StatusBadge from "../components/StatusBadge";

export default function Agents() {
  const { data: projects } = useProjects();
  const projectIds = (projects ?? []).map((p) => p.id);
  const { data: agents, isLoading } = useAllAgents(projectIds);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Agents</h1>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading...</p>
      ) : !agents?.length ? (
        <p className="text-sm text-gray-500">No agents registered.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
              <tr>
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">State</th>
                <th className="px-4 py-3">Project</th>
                <th className="px-4 py-3">Current Task</th>
                <th className="px-4 py-3">Workspace</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {agents.map((agent) => (
                <tr key={agent.workspace_id} className="hover:bg-gray-900/50">
                  <td className="px-4 py-3 font-medium">{agent.name || agent.workspace_id}</td>
                  <td className="px-4 py-3">
                    <StatusBadge status={agent.state} />
                  </td>
                  <td className="px-4 py-3 text-gray-400">{agent.project_id}</td>
                  <td className="max-w-xs truncate px-4 py-3 text-gray-400">
                    {agent.current_task_title ?? "-"}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{agent.workspace_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
