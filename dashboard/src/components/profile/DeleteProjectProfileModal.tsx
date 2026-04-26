import { useEffect, useState } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import Modal from "../Modal";
import { useDeleteProjectProfile } from "../../api/hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  agentType: string;
  hasGlobal: boolean;
}

export default function DeleteProjectProfileModal({
  open,
  onClose,
  projectId,
  agentType,
  hasGlobal,
}: Props) {
  const del = useDeleteProjectProfile();
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (!open) setFatal(null);
  }, [open]);

  const onConfirm = async () => {
    setFatal(null);
    try {
      await del.mutateAsync({ project_id: projectId, agent_type: agentType });
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Reset to global">
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
          <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="space-y-1">
            <p>
              Removes the project override for{" "}
              <span className="font-mono">{agentType}</span>.
            </p>
            <p className="text-xs text-amber-300/80">
              {hasGlobal
                ? "Future tasks will use the global default profile for this agent type."
                : "There is no global default — tasks for this agent type will fail to launch until another profile is created."}
            </p>
          </div>
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
            disabled={del.isPending}
            className="rounded-md bg-amber-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-amber-500 disabled:cursor-not-allowed disabled:bg-gray-700"
          >
            {del.isPending ? "Removing..." : "Remove override"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
