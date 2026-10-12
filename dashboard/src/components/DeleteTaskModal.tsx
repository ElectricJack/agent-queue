import { useState } from "react";
import Modal from "./Modal";
import { useDeleteTask, useDeleteTaskPreview, type Task } from "../api/hooks";

export default function DeleteTaskModal({ task, onClose, onDeleted }: {
  task: Task;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const [recursive, setRecursive] = useState(false);
  const [branches, setBranches] = useState<"keep" | "delete">("keep");
  const [reason, setReason] = useState("");
  const [deleted, setDeleted] = useState(false);
  const [cleanupWarnings, setCleanupWarnings] = useState<string[]>([]);
  const input = { task_id: task.id, recursive, branches, stop_running: true };
  const preview = useDeleteTaskPreview(input);
  const deletion = useDeleteTask();

  const finish = () => { onClose(); onDeleted?.(); };
  const confirm = async () => {
    try {
      const result = await deletion.mutateAsync({ ...input, confirmed: true, reason });
      const failures = result?.warnings?.filter((notice) =>
        ["cleanup_failed", "branch_cleanup_incomplete", "notification_failed", "branch_repository_unknown", "branch_checkout_unavailable", "branch_inspection", "remote_inspection"].includes(notice.code),
      ) ?? [];
      if (failures.length) {
        setDeleted(true);
        setCleanupWarnings(failures.map((notice) => notice.message));
      } else finish();
    } catch {
      // React Query retains the error for the dialog.
    }
  };
  const plan = preview.data;

  return <Modal open onClose={deleted ? finish : onClose} title={deleted ? "Task deleted" : "Delete task"}>
    <div className="space-y-4">
      {deleted ? <>
        <p className="text-sm text-gray-300">Tasks were deleted and the audit was saved. Some cleanup needs attention:</p>
        {cleanupWarnings.map((notice, index) => <p key={index} role="alert" className="text-sm text-amber-200">{notice}</p>)}
        <button onClick={finish} className="rounded-md bg-gray-700 px-3 py-1.5 text-sm text-white">Done</button>
      </> : <>
        <p className="text-sm text-gray-300">Delete <strong>{task.title}</strong>? Audit history will remain.</p>
        <label className="block text-sm text-gray-300">
          Scope
          <select aria-label="Deletion scope" value={recursive ? "recursive" : "task"}
            disabled={deletion.isPending}
            onChange={(event) => setRecursive(event.target.value === "recursive")}
            className="mt-1 w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2">
            <option value="task">This task only; keep children</option>
            <option value="recursive">This task and all descendants</option>
          </select>
        </label>
        <label className="block text-sm text-gray-300">
          Branches
          <select aria-label="Branch cleanup" value={branches} disabled={deletion.isPending}
            onChange={(event) => setBranches(event.target.value as "keep" | "delete")}
            className="mt-1 w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2">
            <option value="keep">Keep branches and worktrees</option>
            <option value="delete">Delete local and remote branches and their worktrees</option>
          </select>
        </label>
        <p className="text-sm text-gray-300">Running sessions and claims for selected tasks will stop.</p>
        <label className="block text-sm text-gray-300">
          Reason (optional)
          <input value={reason} disabled={deletion.isPending} onChange={(event) => setReason(event.target.value)}
            className="mt-1 w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2" />
        </label>
        {preview.isFetching && <p role="status" className="text-sm text-gray-400">Loading deletion preview…</p>}
        {preview.error && <p role="alert" className="text-sm text-red-300">{preview.error.message}</p>}
        {plan && <div className="max-h-72 space-y-2 overflow-auto text-sm text-gray-300">
          <p className="font-semibold">Tasks to delete ({plan.task_ids?.length ?? 0})</p>
          <ul>{(plan.tasks ?? []).map((row) => <li key={row.id}>{row.id} — {row.title} ({row.status})</li>)}</ul>
          {!!plan.reparented_tasks?.length && <p>Children kept: {plan.reparented_tasks?.join(", ")}</p>}
          {(plan.branch_details ?? []).map((entry) => <div key={`${entry.repository_id}:${entry.branch}`}>
            <p>{entry.action === "delete" ? "Delete" : "Keep"} branch <code>{entry.branch}</code></p>
            <p>Remote: {entry.remote_sha ?? entry.remote_state}</p>
            {(entry.local_refs ?? []).map((ref) => <p key={ref.path}>{ref.path}: {ref.sha ?? "tracking ref only"}</p>)}
            {(entry.worktrees ?? []).map((checkout) => <p key={checkout.path}>Worktree: {checkout.path}</p>)}
          </div>)}
          {(plan.sessions ?? []).map((session) => <p key={session.session_id}>Stop session {session.session_id} ({session.task_id})</p>)}
          {(plan.warnings ?? []).map((notice, index) => <p key={index} className="text-amber-200">{notice.message}</p>)}
        </div>}
        {deletion.error && <p role="alert" className="text-sm text-red-300">{deletion.error.message}</p>}
        <div className="flex justify-end gap-2">
          <button onClick={onClose} disabled={deletion.isPending}
            className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300">Cancel</button>
          <button onClick={() => { void confirm(); }} disabled={deletion.isPending || preview.isFetching || !plan}
            className="rounded-md bg-red-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">
            {deletion.isPending ? "Deleting…" : "Delete selected tasks"}
          </button>
        </div>
      </>}
    </div>
  </Modal>;
}
