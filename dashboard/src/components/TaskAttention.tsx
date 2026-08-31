export default function TaskAttention({ task }: { task: { needs_attention?: string | null } }) {
  if (!task.needs_attention) return null;
  return (
    <section aria-label="Needs attention" className="rounded-lg border border-amber-700/50 bg-amber-950/20 p-3">
      <h2 className="mb-1 text-sm font-semibold text-amber-300">Needs attention</h2>
      <p className="whitespace-pre-wrap break-words text-sm text-amber-200">{task.needs_attention}</p>
    </section>
  );
}
