import type { ReactNode } from "react";
import { useListNav } from "../../shell/hotkeys/useListNav";
import KnowledgeCard from "./KnowledgeCard";
import type { AsyncView, KnowledgeListPage } from "./model";

export interface KnowledgeListProps {
  view: AsyncView<KnowledgeListPage>;
  selectedId: string | null;
  onSelect: (recordId: string) => void;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  /** Changes the empty-state copy and offers a reset. */
  filtersActive?: boolean;
  onClearFilters?: () => void;
}

/**
 * The metadata list. Loading is a live region, failure an alert, and an
 * empty page says whether the filters or the project emptied it. Arrow keys
 * move between cards (`useListNav`), Enter or Space selects.
 */
export default function KnowledgeList({
  view, selectedId, onSelect, onLoadMore, loadingMore = false, filtersActive = false, onClearFilters,
}: KnowledgeListProps) {
  // useListNav binds once, on mount, so its host must exist in every state —
  // not only once the first page has arrived.
  const hostRef = useListNav<HTMLDivElement>();

  let content: ReactNode;
  if (view.status === "loading") {
    content = <p role="status" className="text-sm text-gray-500">Loading knowledge…</p>;
  } else if (view.status === "error") {
    content = <p role="alert" className="text-sm text-red-300">{view.message}</p>;
  } else if (view.data.items.length === 0) {
    content = (
      <div data-knowledge-empty className="rounded-lg border border-dashed border-gray-800 p-4 text-sm text-gray-500">
        <p>{filtersActive ? "No knowledge records match these filters." : "No knowledge records yet."}</p>
        {filtersActive && onClearFilters && (
          <button type="button" onClick={onClearFilters} className="mt-2 rounded px-2 py-1 text-xs text-indigo-300 hover:bg-gray-800">
            Clear filters
          </button>
        )}
      </div>
    );
  } else {
    const { items, nextCursor } = view.data;
    content = (
      <>
        <ul aria-label="Knowledge records" className="space-y-2">
          {items.map((item) => (
            <li key={item.recordId}>
              <KnowledgeCard item={item} selected={item.recordId === selectedId} onSelect={() => onSelect(item.recordId)} />
            </li>
          ))}
        </ul>
        {nextCursor && onLoadMore && (
          <button
            type="button"
            onClick={onLoadMore}
            disabled={loadingMore}
            className="w-full rounded border border-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-900 disabled:opacity-60"
          >
            {loadingMore ? "Loading more…" : "Load more"}
          </button>
        )}
      </>
    );
  }

  return <div ref={hostRef} className="space-y-2">{content}</div>;
}
