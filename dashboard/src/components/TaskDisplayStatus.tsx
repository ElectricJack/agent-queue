import StatusBadge from "./StatusBadge";
import { childCountText, DISPLAY_LABEL, type EpicActivity } from "./epicActivityFormat";

/** A task's status badge: a container shows its rolled-up activity instead of
 * the stored lifecycle, which stays in the tooltip. Every list surface uses it. */
export default function TaskDisplayStatus({ task }: { task: { status?: string | null } & EpicActivity }) {
  if (task.status === "READY" && !task.child_state_counts && task.claim_readiness && !task.claim_readiness.claimable) {
    return <span title={`${task.claim_readiness.reason} · task status READY`}>
      <StatusBadge status={task.claim_readiness.label} />
    </span>;
  }
  return <span title={task.display_reason ? `${task.display_reason} · task status ${task.status}` : undefined}>
    <StatusBadge status={task.display_state ? DISPLAY_LABEL[task.display_state] : task.status} />
  </span>;
}

export function EpicActivitySummary({ task }: { task: EpicActivity }) {
  const counts = childCountText(task.child_state_counts);
  const reason = !task.child_state_counts && task.claim_readiness && !task.claim_readiness.claimable ? task.claim_readiness.reason : task.display_reason;
  if (!counts && !reason) return null;
  return <span className="mt-1 block text-xs text-gray-400">
    {counts && <span data-child-state-counts title={counts} className="block">{counts}</span>}
    {reason && <span data-display-reason className="block">{reason}</span>}
  </span>;
}
