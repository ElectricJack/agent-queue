import { useEffect, useState } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import Modal from "../Modal";
import { useDeleteProfile } from "../../api/hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  profileId: string;
  profileName?: string | null;
}

export default function DeleteProfileModal({ open, onClose, profileId, profileName }: Props) {
  const del = useDeleteProfile();
  const [typed, setTyped] = useState("");
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setTyped("");
      setFatal(null);
    }
  }, [open]);

  const confirmed = typed === profileId;

  const onConfirm = async () => {
    setFatal(null);
    try {
      await del.mutateAsync({ profile_id: profileId });
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Delete system profile">
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="space-y-1">
            <p>
              Deletes the system profile{" "}
              <span className="font-mono">{profileId}</span>
              {profileName ? <span className="text-amber-300/80"> ({profileName})</span> : null}.
            </p>
            <p className="text-xs text-amber-300/80">
              Tasks and projects that referenced this profile by id will fail to launch
              until they're pointed at a different profile. Project-scoped overrides
              for this agent type still work.
            </p>
          </div>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
            Type <code className="text-gray-300">{profileId}</code> to confirm
          </label>
          <input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-sm text-gray-200 focus:border-red-500 focus:outline-none"
          />
        </div>

        {fatal && (
          <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{fatal}</span>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-gray-800 pt-3">
          <button
            onClick={onClose}
            className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={!confirmed || del.isPending}
            className="rounded-md bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {del.isPending ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
