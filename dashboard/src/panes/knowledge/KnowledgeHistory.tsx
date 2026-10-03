import { VerificationBadge } from "../../pages/knowledge/KnowledgeBadges";
import {
  formatKnowledgeTimestamp,
  type AsyncView,
  type KnowledgeHistoryEntryView,
} from "../../pages/knowledge/model";

export interface KnowledgeHistoryProps {
  view: AsyncView<{ entries: KnowledgeHistoryEntryView[]; nextCursor: string | null }>;
  /** The revision the pane is showing; null means current. */
  viewedRevisionId: string | null;
  /** `null` asks for the current revision; an id pins a historical one. */
  onView: (revisionId: string | null) => void;
  onCompare: (fromRevisionId: string, toRevisionId: string) => void;
  onLoadMore?: () => void;
  loadingMore?: boolean;
}

const SMALL = "rounded border border-gray-700 px-2 py-0.5 text-[11px] text-gray-200 hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50";

/**
 * Revision envelopes, newest first. Viewing a historical one pins the pane
 * to it; the current one is reached only through its own "View" control, so
 * a pinned read never silently becomes the head. Compare needs both payloads,
 * so a redacted neighbour disables it with the reason.
 */
export default function KnowledgeHistory({
  view, viewedRevisionId, onView, onCompare, onLoadMore, loadingMore = false,
}: KnowledgeHistoryProps) {
  if (view.status === "loading") return <p role="status" className="text-sm text-gray-500">Loading history…</p>;
  if (view.status === "error") return <p role="alert" className="text-sm text-red-300">{view.message}</p>;
  const { entries, nextCursor } = view.data;
  if (entries.length === 0) return <p className="text-sm text-gray-500">No revisions.</p>;
  return (
    <div className="space-y-2">
      <ol aria-label="Revision history" className="space-y-2">
        {entries.map((entry, index) => {
          const previous = entries[index + 1];
          const isViewed = viewedRevisionId === null ? entry.isCurrent : entry.revisionId === viewedRevisionId;
          const compareReason = !previous
            ? nextCursor ? "Load more history to compare" : "First revision"
            : entry.redacted || previous.redacted
              ? "A redacted revision cannot be compared"
              : null;
          return (
            <li
              key={entry.revisionId}
              aria-current={isViewed ? "true" : undefined}
              className={`rounded-lg border p-2 text-xs ${isViewed ? "border-indigo-400/60 bg-indigo-500/10" : "border-gray-800 bg-gray-900"}`}
            >
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="font-mono text-gray-200">r{entry.sequence}</span>
                {entry.isCurrent && (
                  <span className="inline-flex items-center rounded-full bg-indigo-500/10 px-2 py-0.5 text-[10px] font-medium text-indigo-300">current</span>
                )}
                <span className="text-gray-400">{entry.changeKind.replace(/_/g, " ")}</span>
                <VerificationBadge verification={entry.verification} />
                {entry.redacted && (
                  <span className="inline-flex items-center rounded-full bg-gray-500/10 px-2 py-0.5 text-[10px] font-medium text-gray-400">redacted</span>
                )}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 text-gray-400">
                <span className="font-mono [overflow-wrap:anywhere]">{entry.revisionId}</span>
                <span>{entry.actorId}</span>
                <span>{formatKnowledgeTimestamp(entry.createdAt)}</span>
              </div>
              {entry.changeReason && <p className="mt-1 text-gray-300">{entry.changeReason}</p>}
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <button type="button" className={SMALL} aria-label={`View revision ${entry.sequence}`}
                  disabled={isViewed} onClick={() => onView(entry.isCurrent ? null : entry.revisionId)}>
                  {entry.isCurrent ? "View current" : "View"}
                </button>
                <button type="button" className={SMALL} aria-label={`Compare revision ${entry.sequence} with previous`}
                  disabled={compareReason !== null} title={compareReason ?? undefined}
                  onClick={() => previous && onCompare(previous.revisionId, entry.revisionId)}>
                  Compare with previous
                </button>
              </div>
            </li>
          );
        })}
      </ol>
      {nextCursor && onLoadMore && (
        <button type="button" onClick={onLoadMore} disabled={loadingMore}
          className="w-full rounded border border-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-900 disabled:opacity-60">
          {loadingMore ? "Loading more…" : "Load more"}
        </button>
      )}
    </div>
  );
}
