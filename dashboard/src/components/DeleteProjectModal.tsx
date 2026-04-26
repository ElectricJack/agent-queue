import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import Modal from "./Modal";
import { useDeleteProject } from "../api/hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  projectName: string;
}

const CONFIRM_WORD = "delete";

export default function DeleteProjectModal({ open, onClose, projectId, projectName }: Props) {
  const del = useDeleteProject();
  const navigate = useNavigate();
  const [typed, setTyped] = useState("");
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setTyped("");
      setFatal(null);
    }
  }, [open]);

  const confirmed = typed.trim().toLowerCase() === CONFIRM_WORD;

  const onConfirm = async () => {
    setFatal(null);
    try {
      await del.mutateAsync({ project_id: projectId });
      onClose();
      navigate("/system");
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Delete project">
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="space-y-1">
            <p>
              This permanently removes <span className="font-mono">{projectId}</span> and all of
              its tasks, workspaces, and constraints.
            </p>
            <p className="text-xs text-red-300/80">
              In-progress tasks must be stopped first or the request will fail.
            </p>
          </div>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium uppercase text-gray-500">
            Type <code className="text-gray-300">{CONFIRM_WORD}</code> to confirm
          </label>
          <input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoFocus
            placeholder={CONFIRM_WORD}
            className="w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-1.5 font-mono text-sm text-gray-200 focus:border-red-500 focus:outline-none"
          />
        </div>

        {fatal && (
          <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{fatal}</span>
          </div>
        )}

        <div className="flex items-center justify-between gap-2 border-t border-gray-800 pt-3">
          <span className="truncate text-xs text-gray-500">{projectName}</span>
          <div className="flex items-center gap-2">
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
              {del.isPending ? "Deleting..." : "Delete project"}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
