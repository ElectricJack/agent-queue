import { useState } from "react";
import { PencilIcon, TrashIcon } from "@heroicons/react/24/outline";
import { useProfiles, type Profile } from "../../api/hooks";
import SystemProfileEditDrawer from "../../components/profile/SystemProfileEditDrawer";
import DeleteProfileModal from "../../components/profile/DeleteProfileModal";

export default function SystemProfiles() {
  const { data: profiles, isLoading } = useProfiles();
  const [editId, setEditId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Profile | null>(null);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">System Profiles</h1>
        <p className="text-xs text-gray-500">
          Source of truth lives in <span className="font-mono">~/.agent-queue/vault/agent-types/</span>.
          Edits here write through the daemon and re-sync the vault file.
        </p>
      </div>

      {isLoading ? (
        <p className="text-sm text-gray-500">Loading...</p>
      ) : (profiles ?? []).length === 0 ? (
        <p className="text-sm text-gray-500">No profiles registered.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-800 text-xs uppercase text-gray-500">
              <tr>
                <th className="px-4 py-3">ID</th>
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Model</th>
                <th className="px-4 py-3">Tools</th>
                <th className="px-4 py-3">MCP servers</th>
                <th className="px-4 py-3">Prompt</th>
                <th className="w-24 px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {(profiles ?? []).map((p) => (
                <tr key={p.id} className="hover:bg-gray-900/50">
                  <td className="px-4 py-3">
                    <span className="font-mono text-xs text-gray-300">{p.id}</span>
                  </td>
                  <td className="px-4 py-3 text-gray-200">{p.name}</td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-400">
                    {p.model || "—"}
                  </td>
                  <td className="px-4 py-3 text-gray-400">
                    {p.allowed_tools?.length ?? 0}
                  </td>
                  <td className="px-4 py-3 text-gray-400">
                    {p.mcp_servers?.length ?? 0}
                  </td>
                  <td className="px-4 py-3">
                    {p.has_system_prompt ? (
                      <span className="rounded bg-indigo-500/10 px-2 py-0.5 text-xs text-indigo-300">
                        custom
                      </span>
                    ) : (
                      <span className="text-gray-500">default</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => setEditId(p.id)}
                        title="Edit"
                        className="rounded p-1 text-gray-400 transition-colors hover:bg-gray-700 hover:text-gray-100"
                      >
                        <PencilIcon className="h-3.5 w-3.5" />
                      </button>
                      <button
                        onClick={() => setDeleteTarget(p)}
                        title="Delete"
                        className="rounded p-1 text-red-400 transition-colors hover:bg-red-500/20"
                      >
                        <TrashIcon className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editId && (
        <SystemProfileEditDrawer
          open={true}
          onClose={() => setEditId(null)}
          profileId={editId}
        />
      )}

      {deleteTarget && (
        <DeleteProfileModal
          open={true}
          onClose={() => setDeleteTarget(null)}
          profileId={deleteTarget.id}
          profileName={deleteTarget.name}
        />
      )}
    </div>
  );
}
