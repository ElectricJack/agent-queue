import { useParams } from "react-router-dom";
import { useAgents, useWorkspaces } from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";

export default function ProjectWorkspaces() {
  const { projectId = "" } = useParams();
  const { data: workspaces, isLoading } = useWorkspaces(projectId);
  const { data: agents } = useAgents(projectId);

  const agentByWorkspace = new Map((agents ?? []).map((a) => [a.workspace_id, a]));

  if (isLoading) return <p className="text-sm text-gray-500">Loading...</p>;
  if (!workspaces?.length) {
    return <p className="text-sm text-gray-500">No workspaces in this project.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
          <tr>
            <th className="px-4 py-3">Name</th>
            <th className="px-4 py-3">Source</th>
            <th className="px-4 py-3">Path</th>
            <th className="px-4 py-3">State</th>
            <th className="px-4 py-3">Current Task</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {workspaces.map((ws) => {
            const agent = agentByWorkspace.get(ws.id);
            return (
              <tr key={ws.id} className="hover:bg-gray-900/50">
                <td className="px-4 py-3 font-medium">{ws.name || ws.id}</td>
                <td className="px-4 py-3 text-gray-400">{ws.source_type || "-"}</td>
                <td className="max-w-md truncate px-4 py-3 font-mono text-xs text-gray-500">
                  {ws.workspace_path}
                </td>
                <td className="px-4 py-3">
                  {agent ? (
                    <StatusBadge status={agent.state} />
                  ) : (
                    <span className="text-xs text-gray-500">idle</span>
                  )}
                </td>
                <td className="max-w-xs truncate px-4 py-3 text-gray-400">
                  {agent?.current_task_title ?? "-"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
