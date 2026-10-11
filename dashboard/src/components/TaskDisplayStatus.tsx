import StatusBadge from "./StatusBadge";
import { childCountText, DISPLAY_LABEL, type EpicActivity } from "./epicActivityFormat";

/** A task's status badge: a container shows its rolled-up activity instead of
 * the stored lifecycle, which stays in the tooltip. Every list surface uses it. */
export default function TaskDisplayStatus({ task }: { task: { status?: string | null } & EpicActivity }) {
  return <span title={task.display_reason ? `${task.display_reason} · task status ${task.status}` : undefined}>
    <StatusBadge status={task.display_state ? DISPLAY_LABEL[task.display_state] : task.status} />
  </span>;
}

export function EpicActivitySummary({ task }: { task: EpicActivity }) {
  const counts = childCountText(task.child_state_counts);
  if (!counts && !task.display_reason) return null;
  return <span className="mt-1 block text-xs text-gray-400">
    {counts && <span data-child-state-counts title={counts} className="block">{counts}</span>}
    {task.display_reason && <span data-display-reason className="block">{task.display_reason}</span>}
  </span>;
}
