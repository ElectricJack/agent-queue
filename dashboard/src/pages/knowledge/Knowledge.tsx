import { useCallback, useState } from "react";
import KnowledgePane from "../../panes/knowledge/KnowledgePane";
import type { KnowledgeAdapter } from "./adapter";
import KnowledgeFilters from "./KnowledgeFilters";
import KnowledgeList from "./KnowledgeList";
import { hasActiveKnowledgeFilters, type KnowledgeSelection } from "./knowledgeUrlState";
import {
  DEFAULT_KNOWLEDGE_FILTERS,
  type KnowledgeAction,
  type KnowledgeDetailView,
  type KnowledgeListFilters,
} from "./model";
import { listView, useKnowledgeList } from "./useKnowledge";

export interface KnowledgeState {
  filters: KnowledgeListFilters;
  selection: KnowledgeSelection;
}

export interface KnowledgeProps {
  adapter: KnowledgeAdapter;
  /** Starting state, e.g. read from the URL by the route owner. */
  initialFilters?: KnowledgeListFilters;
  /** Controlled addressable state supplied by the route. */
  state?: KnowledgeState;
  initialSelection?: KnowledgeSelection;
  /** Every filter or selection change, so the route owner can write the URL. */
  onStateChange?: (state: KnowledgeState) => void;
  /** Leaving the knowledge surface for a task: the route owner navigates. */
  onOpenTask?: (taskId: string) => void;
  /** Actions this slice only announces (link, retire, restore, create task, propose correction). */
  onAction?: (action: KnowledgeAction, detail: KnowledgeDetailView) => void;
  heading?: string;
}

const NO_SELECTION: KnowledgeSelection = { recordId: null, revisionId: null };

/**
 * The Knowledge surface: filters and the metadata list beside the detail
 * pane for the selected record. It is not routed and registers no pane;
 * K10 mounts it behind `knowledge.ui_enabled` with a live adapter and wires
 * `onStateChange` to the URL. Work stays the command-center default.
 */
export default function Knowledge({
  adapter,
  initialFilters = DEFAULT_KNOWLEDGE_FILTERS,
  initialSelection = NO_SELECTION,
  onStateChange,
  state,
  onOpenTask,
  onAction,
  heading = "Knowledge",
}: KnowledgeProps) {
  const [localFilters, setFiltersState] = useState<KnowledgeListFilters>(initialFilters);
  const [localSelection, setSelectionState] = useState<KnowledgeSelection>(initialSelection);

  const filters = state?.filters ?? localFilters;
  const selection = state?.selection ?? localSelection;

  const setFilters = useCallback((next: KnowledgeListFilters) => {
    setFiltersState(next);
    onStateChange?.({ filters: next, selection });
  }, [onStateChange, selection]);

  const setSelection = useCallback((next: KnowledgeSelection) => {
    setSelectionState(next);
    onStateChange?.({ filters, selection: next });
  }, [onStateChange, filters]);

  const list = useKnowledgeList(adapter, filters);
  const view = listView(list);

  return (
    <div className="flex h-full min-h-0 flex-col gap-4 lg:flex-row">
      <section aria-label={heading} className="flex min-h-0 flex-col gap-3 lg:w-96 lg:shrink-0">
        <h1 className="text-lg font-semibold text-gray-100">{heading}</h1>
        <KnowledgeFilters filters={filters} onChange={setFilters} />
        <div className="dashboard-scrollbar min-h-0 flex-1 overflow-y-auto">
          <KnowledgeList
            view={view}
            selectedId={selection.recordId}
            onSelect={(recordId) => setSelection({ recordId, revisionId: null })}
            onLoadMore={list.hasNextPage ? () => void list.fetchNextPage() : undefined}
            loadingMore={list.isFetchingNextPage}
            filtersActive={hasActiveKnowledgeFilters(filters)}
            onClearFilters={() => setFilters({ ...DEFAULT_KNOWLEDGE_FILTERS })}
          />
        </div>
      </section>
      <div className="min-h-0 min-w-0 flex-1">
        {selection.recordId ? (
          <KnowledgePane
            key={selection.recordId}
            adapter={adapter}
            recordId={selection.recordId}
            revisionId={selection.revisionId}
            onRevisionChange={(revisionId) => setSelection({ recordId: selection.recordId, revisionId })}
            onOpenRecord={(recordId, revisionId) => setSelection({ recordId, revisionId })}
            onOpenTask={onOpenTask}
            onAction={onAction}
          />
        ) : (
          <p className="rounded-lg border border-dashed border-gray-800 p-6 text-center text-sm text-gray-500">
            Select a record to read it.
          </p>
        )}
      </div>
    </div>
  );
}
