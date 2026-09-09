import type { TaskActivityItem } from "../../api/activity";
import { absoluteTime, relativeAge } from "./activityFormat";

/**
 * Every model that worked the task inside the window, newest attempt first.
 *
 * An attempt that reported no model is shown as unattributed rather than
 * being filled in from the profile's configured model: the point of the
 * column is what actually ran.
 */
export function ModelsCell({ item }: { item: TaskActivityItem }) {
  const { models, unattributed_attempts: unattributed, attempt_count: attempts } = item;
  if (!models.length && !attempts) {
    return <span className="text-xs text-gray-600" title="The task changed state with no agent session in this window">No agent session</span>;
  }
  return (
    <span className="flex flex-wrap items-center gap-1">
      {models.map((model) => (
        <span key={model} title={model}
          className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-[10px] text-gray-200">{model}</span>
      ))}
      {unattributed > 0 && (
        <span title={`${unattributed} attempt(s) recorded no model`}
          className="rounded border border-amber-500/40 px-1.5 py-0.5 text-[10px] text-amber-200">
          {models.length ? `+${unattributed} unattributed` : "Unattributed"}
        </span>
      )}
      {attempts > 1 && (
        <span className="text-[10px] text-gray-500" title="Session attempts in this window">
          {attempts} attempts
        </span>
      )}
    </span>
  );
}

/** When the task was last worked, plus the outcome its close recorded. */
export function ActivityCell({ item }: { item: TaskActivityItem }) {
  return (
    <span className="flex flex-col">
      <span className="text-xs text-gray-300" title={absoluteTime(item.last_activity_at)}>
        {relativeAge(item.last_activity_at)}
      </span>
      {item.outcome && (
        <span className={`text-[10px] ${item.outcome === "pass" ? "text-emerald-400" : "text-red-300"}`}>
          {item.outcome}{item.failure_class ? ` · ${item.failure_class}` : ""}
        </span>
      )}
      {item.archived && <span className="text-[10px] text-gray-500">archived</span>}
    </span>
  );
}
