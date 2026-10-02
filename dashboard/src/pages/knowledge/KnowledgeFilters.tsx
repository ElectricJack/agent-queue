import { useId } from "react";
import { hasActiveKnowledgeFilters } from "./knowledgeUrlState";
import {
  DEFAULT_KNOWLEDGE_FILTERS,
  KNOWLEDGE_CATEGORIES,
  KNOWLEDGE_VERIFICATIONS,
  type KnowledgeCategory,
  type KnowledgeLifecycle,
  type KnowledgeListFilters,
  type KnowledgeVerification,
} from "./model";

export interface KnowledgeFiltersProps {
  filters: KnowledgeListFilters;
  onChange: (filters: KnowledgeListFilters) => void;
}

const SELECT = "rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200";

/**
 * Metadata search and the three facet filters. Every control is labelled;
 * the owner decides where the state lives (this slice's `Knowledge` keeps it
 * in memory, K10 writes it to the URL with `knowledgeUrlState.ts`).
 */
export default function KnowledgeFilters({ filters, onChange }: KnowledgeFiltersProps) {
  const id = useId();
  const set = <K extends keyof KnowledgeListFilters>(key: K, value: KnowledgeListFilters[K]) =>
    onChange({ ...filters, [key]: value });
  return (
    <form role="search" aria-label="Filter knowledge" onSubmit={(event) => event.preventDefault()} className="space-y-2">
      <div>
        <label htmlFor={`${id}-q`} className="sr-only">Search knowledge</label>
        <input
          id={`${id}-q`}
          type="search"
          value={filters.query}
          onChange={(event) => set("query", event.target.value)}
          placeholder="Search title, summary, tags…"
          className="w-full rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200 placeholder:text-gray-500"
        />
      </div>
      <div className="flex flex-wrap items-end gap-2 text-xs text-gray-400">
        <label className="flex flex-col gap-0.5">
          Category
          <select className={SELECT} value={filters.category} onChange={(event) => set("category", event.target.value as "" | KnowledgeCategory)}>
            <option value="">Any</option>
            {KNOWLEDGE_CATEGORIES.map((category) => <option key={category} value={category}>{category}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          Lifecycle
          <select className={SELECT} value={filters.lifecycle} onChange={(event) => set("lifecycle", event.target.value as "" | KnowledgeLifecycle)}>
            <option value="active">active</option>
            <option value="retired">retired</option>
            <option value="">Any</option>
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          Verification
          <select className={SELECT} value={filters.verification} onChange={(event) => set("verification", event.target.value as "" | KnowledgeVerification)}>
            <option value="">Any</option>
            {KNOWLEDGE_VERIFICATIONS.map((verification) => <option key={verification} value={verification}>{verification}</option>)}
          </select>
        </label>
        {hasActiveKnowledgeFilters(filters) && (
          <button
            type="button"
            onClick={() => onChange({ ...DEFAULT_KNOWLEDGE_FILTERS })}
            className="rounded px-2 py-1 text-xs text-indigo-300 hover:bg-gray-800"
          >
            Clear filters
          </button>
        )}
      </div>
    </form>
  );
}
