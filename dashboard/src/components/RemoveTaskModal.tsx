import { useState } from "react";
import Modal from "./Modal";
import { useRemoveTask, type Task } from "../api/hooks";
import { integrationRemovalRefusal } from "../api/deleteRefusals";

export default function RemoveTaskModal({ task, onClose, onRemoved }: {
  task: Task;
  onClose: () => void;
  onRemoved?: () => void;
}) {
  const remove = useRemoveTask();
  const [reason, setReason] = useState("No longer needed");
  const [failure, setFailure] = useState<string | null>(null);

  const confirm = async () => {
    setFailure(null);
    try {
      await remove.mutateAsync({ task_id: task.id, confirmed: true, reason: reason.trim() });
      onClose();
      onRemoved?.();
    } catch (error) {
      setFailure(integrationRemovalRefusal(error) ?? (error instanceof Error ? error.message : String(error)));
    }
  };

  return <Modal open onClose={onClose} title="Remove task">
    <div className="space-y-4">
      <p className="text-sm text-gray-300">
        Remove <strong>{task.title}</strong> and all descendant tasks from the active graph?
        Running sessions will stop and open integration batches will be aborted.
        Branches will stay. Tasks with integration history will be archived so their records remain available.
      </p>
      <label className="block text-sm text-gray-300">
        Reason
        <input value={reason} onChange={(event) => setReason(event.target.value)}
          className="mt-1 w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2" />
      </label>
      {failure && <p role="alert" className="text-sm text-red-300">{failure}</p>}
      <div className="flex justify-end gap-2">
        <button onClick={onClose} disabled={remove.isPending}
          className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300">
          Cancel
        </button>
        <button onClick={() => { void confirm(); }} disabled={remove.isPending || !reason.trim()}
          className="rounded-md bg-red-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">
          {remove.isPending ? "Removing…" : "Remove task and descendants"}
        </button>
      </div>
    </div>
  </Modal>;
}
