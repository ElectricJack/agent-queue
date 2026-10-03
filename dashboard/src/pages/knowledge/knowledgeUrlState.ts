/**
 * Addressable Knowledge state lives in the URL (plan §10), the same way the
 * task workspace keeps its filters in `taskFilters.ts`. These helpers are pure
 * so K10 can wire them to `useSearchParams` without touching the components.
 *
 * Keys: `q`, `category`, `lifecycle` (`any` spells the empty filter because
 * the default is `active`), `verification`, `record`, `revision`.
 */
import {
  DEFAULT_KNOWLEDGE_FILTERS,
  KNOWLEDGE_CATEGORIES,
  KNOWLEDGE_LIFECYCLES,
  KNOWLEDGE_VERIFICATIONS,
  type KnowledgeCategory,
  type KnowledgeLifecycle,
  type KnowledgeListFilters,
  type KnowledgeVerification,
} from "./model";

function member<T extends string>(values: readonly T[], raw: string | null): T | "" {
  // An unknown value would silently widen or narrow the list under a label
  // that says otherwise, so only known values survive the round trip.
  return raw && (values as readonly string[]).includes(raw) ? (raw as T) : "";
}

export function readKnowledgeFilters(params: URLSearchParams): KnowledgeListFilters {
  const lifecycle = params.get("lifecycle");
  return {
    query: params.get("q") ?? "",
    category: member<KnowledgeCategory>(KNOWLEDGE_CATEGORIES, params.get("category")),
    lifecycle: lifecycle === "any"
      ? ""
      : member<KnowledgeLifecycle>(KNOWLEDGE_LIFECYCLES, lifecycle) || DEFAULT_KNOWLEDGE_FILTERS.lifecycle,
    verification: member<KnowledgeVerification>(KNOWLEDGE_VERIFICATIONS, params.get("verification")),
  };
}

export function writeKnowledgeFilters(params: URLSearchParams, filters: KnowledgeListFilters): URLSearchParams {
  const next = new URLSearchParams(params);
  const lifecycle = filters.lifecycle === DEFAULT_KNOWLEDGE_FILTERS.lifecycle
    ? ""
    : filters.lifecycle || "any";
  for (const [key, value] of [
    ["q", filters.query],
    ["category", filters.category],
    ["lifecycle", lifecycle],
    ["verification", filters.verification],
  ] as const) {
    if (value) next.set(key, value);
    else next.delete(key);
  }
  return next;
}

export function hasActiveKnowledgeFilters(filters: KnowledgeListFilters): boolean {
  return filters.query.trim() !== ""
    || filters.category !== DEFAULT_KNOWLEDGE_FILTERS.category
    || filters.lifecycle !== DEFAULT_KNOWLEDGE_FILTERS.lifecycle
    || filters.verification !== DEFAULT_KNOWLEDGE_FILTERS.verification;
}

export interface KnowledgeSelection {
  recordId: string | null;
  /** A named revision is a pinned, possibly historical, view; null is current. */
  revisionId: string | null;
}

export function readKnowledgeSelection(params: URLSearchParams): KnowledgeSelection {
  const recordId = params.get("record") || null;
  // A revision without its record addresses nothing.
  return { recordId, revisionId: recordId ? params.get("revision") || null : null };
}

export function writeKnowledgeSelection(params: URLSearchParams, selection: KnowledgeSelection): URLSearchParams {
  const next = new URLSearchParams(params);
  if (selection.recordId) next.set("record", selection.recordId);
  else next.delete("record");
  if (selection.recordId && selection.revisionId) next.set("revision", selection.revisionId);
  else next.delete("revision");
  return next;
}
