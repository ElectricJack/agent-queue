import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { TrashIcon } from "@heroicons/react/24/outline";
import Modal from "../../components/Modal";
import { usePoolDelete } from "../../api/hooks";
import { poolDisplayName, type PoolEntry } from "./pools";

export default function DeletePool({ entry }: { entry: PoolEntry }) {
  const [open, setOpen] = useState(false);
  const [depth, setDepth] = useState<"profile" | "directory">("profile");
  const del = usePoolDelete();
  const navigate = useNavigate();
  const name = poolDisplayName(entry.pool);
  const confirm = async () => {
    const result = await del.mutateAsync({ profile_id: entry.profileId, depth });
    // Keep cleanup warnings in the destination so they survive removal of
    // the pool window along with its confirmed deleted query row.
    navigate("/agents", { state: { poolDeleteWarnings: result.warnings } });
  };
  return <>
    <div className="border-t border-gray-800 pt-4">
      <button type="button" onClick={() => { del.reset(); setOpen(true); }}
        className="flex items-center gap-2 rounded border border-red-800 px-3 py-2 text-xs text-red-300 hover:bg-red-950/40">
        <TrashIcon className="h-4 w-4" />Delete pool
      </button>
    </div>
    <Modal open={open} onClose={() => { if (!del.isPending) setOpen(false); }} title={"Delete " + name + " pool"}>
      <div className="space-y-4 text-sm text-gray-300">
        <p>This deletes the pool profile, its bounds and enabled state across all projects.
          The router will no longer select this pool.</p>
        <p className="rounded border border-amber-600/40 bg-amber-950/30 p-3 text-amber-200">
          Running workers will stop, interrupting current work. Held tasks are released through
          guarded cleanup; routed and provider-held tasks return to the router for a fresh route.
          No task is marked complete. There are currently {entry.instances.length} live pool instances.
        </p>
        <label className="block space-y-2">
          <span>Vault deletion depth</span>
          <select value={depth} onChange={(event) => setDepth(event.target.value as typeof depth)}
            disabled={del.isPending} className="w-full rounded border border-gray-700 bg-gray-950 p-2">
            <option value="profile">Profile only — preserve memory and other files</option>
            <option value="directory">Entire profile directory — delete all its files</option>
          </select>
        </label>
        <p className="break-words text-xs text-gray-400">Profile only removes vault/agent-types/{entry.profileId}/profile.md.
          Entire directory also removes profile memory, playbooks and backups. Both choices record retirement
          so install and update cannot recreate the pool. Manual pauses and unrelated holds remain in place.</p>
        {del.error && <p role="alert" className="text-red-300">{del.error.message}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" disabled={del.isPending} onClick={() => setOpen(false)} className="rounded bg-gray-800 px-3 py-2">Cancel</button>
          <button type="button" disabled={del.isPending} onClick={() => { void confirm().catch(() => {}); }}
            className="rounded bg-red-700 px-3 py-2 text-white hover:bg-red-600 disabled:opacity-50">
            {del.isPending ? "Deleting…" : "Delete pool"}
          </button>
        </div>
      </div>
    </Modal>
  </>;
}
