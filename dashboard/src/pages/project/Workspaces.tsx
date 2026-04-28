import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  PencilIcon,
  PlusIcon,
  TrashIcon,
} from "@heroicons/react/24/outline";
import {
  useAgents,
  useEditWorkspace,
  useWorkspaces,
  type Workspace as WorkspaceSummary,
} from "../../api/hooks";
import StatusBadge from "../../components/StatusBadge";
import AddWorkspaceModal from "../../components/workspace/AddWorkspaceModal";
import EditWorkspaceDrawer from "../../components/workspace/EditWorkspaceDrawer";
import DeleteWorkspaceModal from "../../components/workspace/DeleteWorkspaceModal";

export default function ProjectWorkspaces() {
  const { projectId = "" } = useParams();
  const { data: workspaces, isLoading } = useWorkspaces(projectId);
  const { data: agents } = useAgents(projectId);
  const editWorkspace = useEditWorkspace(projectId);

  const [addOpen, setAddOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<WorkspaceSummary | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<WorkspaceSummary | null>(null);

  const agentByWorkspace = new Map((agents ?? []).map((a) => [a.workspace_id, a]));

  const toggleEnabled = (ws: WorkspaceSummary) => {
    editWorkspace.mutate({
      workspace_id: ws.id,
      enabled: ws.enabled === false,
    });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end">
        <button
          onClick={() => setAddOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          <PlusIcon className="h-4 w-4" /> Add workspace
        </button>
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading...</p>
      ) : !workspaces?.length ? (
        <p className="text-sm text-gray-500">
          No workspaces in this project. Click "Add workspace" to create one.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
              <tr>
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Source</th>
                <th className="px-4 py-3">Path</th>
                <th className="px-4 py-3">State</th>
                <th className="px-4 py-3">Current Task</th>
                <th className="px-4 py-3">Enabled</th>
                <th className="w-24 px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {workspaces.map((ws) => {
                const agent = agentByWorkspace.get(ws.id);
                const isDisabled = ws.enabled === false;
                return (
                  <tr
                    key={ws.id}
                    className={`hover:bg-gray-900/50 ${isDisabled ? "opacity-60" : ""}`}
                  >
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
                    <td className="px-4 py-3">
                      <button
                        onClick={() => toggleEnabled(ws)}
                        disabled={
                          editWorkspace.isPending &&
                          editWorkspace.variables?.workspace_id === ws.id
                        }
                        title={
                          isDisabled
                            ? "Enable — orchestrator may assign tasks here"
                            : "Disable — orchestrator skips this workspace for new tasks"
                        }
                        className={`relative inline-flex h-5 w-9 cursor-pointer rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                          isDisabled ? "bg-gray-700" : "bg-indigo-500"
                        }`}
                      >
                        <span
                          className={`pointer-events-none inline-block h-4 w-4 translate-y-0.5 rounded-full bg-white shadow transition-transform ${
                            isDisabled ? "translate-x-0.5" : "translate-x-4.5"
                          }`}
                        />
                      </button>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => setEditTarget(ws)}
                          title="Edit"
                          className="rounded p-1 text-gray-400 transition-colors hover:bg-gray-800 hover:text-gray-100"
                        >
                          <PencilIcon className="h-3.5 w-3.5" />
                        </button>
                        <button
                          onClick={() => setDeleteTarget(ws)}
                          title="Delete"
                          className="rounded p-1 text-red-400 transition-colors hover:bg-red-500/20"
                        >
                          <TrashIcon className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <AddWorkspaceModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        projectId={projectId}
      />

      {editTarget && (
        <EditWorkspaceDrawer
          open={true}
          onClose={() => setEditTarget(null)}
          projectId={projectId}
          workspace={editTarget}
        />
      )}

      {deleteTarget && (
        <DeleteWorkspaceModal
          open={true}
          onClose={() => setDeleteTarget(null)}
          projectId={projectId}
          workspace={deleteTarget}
        />
      )}
    </div>
  );
}
