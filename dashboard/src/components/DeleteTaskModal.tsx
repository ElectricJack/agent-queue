import { useEffect, useState } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";
import Modal from "./Modal";
import { useDeleteTask, type Task } from "../api/hooks";

interface Props {
  open: boolean;
  onClose: () => void;
  task: Task;
}

export default function DeleteTaskModal({ open, onClose, task }: Props) {
  const del = useDeleteTask();
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    if (!open) setFatal(null);
  }, [open]);

  const inProgress = (task.status?.toUpperCase() ?? "") === "IN_PROGRESS";

  const onConfirm = async () => {
    setFatal(null);
    try {
      await del.mutateAsync({ task_id: task.id, cascade: true });
      onClose();
    } catch (err) {
      setFatal(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="Delete task">
      <div className="space-y-4">
        {inProgress ? (
          <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
            <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
            <div className="space-y-1">
              <p>
                <span className="font-mono">{task.id}</span> is currently in progress.
              </p>
              <p className="text-xs text-amber-300/80">
                Stop the task first, then delete.
              </p>
            </div>
          </div>
        ) : (
          <>
            <p className="text-sm text-gray-300">
              Delete <strong>{task.title}</strong>? Any descendant tasks will also be deleted.
              This cannot be undone.
            </p>
            {fatal && (
              <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
                <ExclamationTriangleIcon className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{fatal}</span>
              </div>
            )}
          </>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-gray-800 pt-3">
          <button
            onClick={onClose}
            className="rounded-md bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
          >
            {inProgress ? "Close" : "Cancel"}
          </button>
          {!inProgress && (
            <button
              onClick={onConfirm}
              disabled={del.isPending}
              className="rounded-md bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:bg-gray-700"
            >
              {del.isPending ? "Deleting..." : "Delete task and descendants"}
            </button>
          )}
        </div>
      </div>
    </Modal>
  );
}
