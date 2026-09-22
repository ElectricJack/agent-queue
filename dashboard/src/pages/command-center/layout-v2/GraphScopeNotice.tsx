import type { Variant } from "../../../api/graphLayout";

export type EmptyReason = "no_work" | "all_finished" | "no_matches";

interface GraphScopeNoticeProps {
  /** The variant requested by this view, before a focused-scope fallback. */
  requestedVariant: Variant;
  /** The variant the server actually returned for the current scope. */
  appliedVariant: Variant | null;
  /** Server evidence for an otherwise empty first page. Null is unknown. */
  emptyReason: EmptyReason | null;
  showCompleted: boolean;
  onShowCompleted: (show: boolean) => void;
  loading?: boolean;
  error?: Error | null;
  hiddenFinishedCount?: number | null;
  showBanner?: boolean;
  showEmpty?: boolean;
  emptyClassName?: string;
}

/**
 * Scope-level feedback shared by the canvas and portrait list. In particular,
 * an empty active result is not enough evidence to call a scope all-finished:
 * that conclusion comes from the server's `empty_reason` for the list.
 */
export default function GraphScopeNotice({
  requestedVariant,
  appliedVariant,
  emptyReason,
  showCompleted,
  onShowCompleted,
  loading = false,
  error = null,
  hiddenFinishedCount = null,
  showBanner = true,
  showEmpty = true,
  emptyClassName = "",
}: GraphScopeNoticeProps) {
  if (loading || error) return null;
  const showingFallback = requestedVariant === "active" && appliedVariant === "all" && !showCompleted;

  return (
    <>
      {showBanner && showingFallback && <p role="status" className="shrink-0 border-b border-gray-800 px-4 py-1 text-xs text-gray-400">
        No active work here, so completed work is shown inside this container.
      </p>}
      {showEmpty && emptyReason === "all_finished" && !showCompleted && (
        <div className={emptyClassName}>
          <p>
            No unfinished work here.
            {hiddenFinishedCount !== null && hiddenFinishedCount > 0 && (
              <> {hiddenFinishedCount} finished {hiddenFinishedCount === 1 ? "task" : "tasks"} hidden.</>
            )}
          </p>
          <button type="button" onClick={() => onShowCompleted(true)}
            className="pointer-events-auto rounded-md border border-gray-700 px-3 py-1.5 text-xs text-gray-200 hover:bg-gray-800">
            Show completed
          </button>
        </div>
      )}
      {showEmpty && emptyReason === "no_work" && <p className={emptyClassName}>No tasks yet.</p>}
      {showEmpty && emptyReason === "no_matches" && <p className={emptyClassName}>No tasks match these filters.</p>}
      {showEmpty && emptyReason === null && <p className={emptyClassName}>No tasks to display.</p>}
    </>
  );
}
