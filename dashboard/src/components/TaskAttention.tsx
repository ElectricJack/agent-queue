import { useEditTask } from "../api/hooks";

export default function TaskAttention({ task }: { task: { id: string; needs_attention?: string | null } }) {
  const editTask = useEditTask();
  if (!task.needs_attention) return null;
  return (
    <section aria-label="Needs attention" className="rounded-lg border border-amber-700/50 bg-amber-950/20 p-3">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-amber-300">Needs attention</h2>
        <button
          type="button"
          className="rounded border border-amber-700 px-2 py-0.5 text-xs text-amber-200 hover:bg-amber-900/40 disabled:opacity-50"
          disabled={editTask.isPending}
          onClick={() => editTask.mutate({ task_id: task.id, clear_needs_attention: true })}
        >
          {editTask.isPending ? "Dismissing…" : "Dismiss"}
        </button>
      </div>
      <p className="whitespace-pre-wrap break-words text-sm text-amber-200">{task.needs_attention}</p>
    </section>
  );
}
