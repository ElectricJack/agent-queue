/**
 * The seam between the Knowledge presentation components and whatever serves
 * them. This slice ships one implementation, the in-memory fixture
 * (`fixtureAdapter.ts`); K10 adds the live one over the generated SDK once
 * K03's routes exist on main. The components take an adapter as a prop and
 * never import either implementation.
 *
 * Reads reject with a `KnowledgeAdapterError` carrying the server's typed
 * code; writes resolve to a typed `KnowledgeUpdateResult` so a revision
 * conflict is data the form renders, not an exception it catches.
 */
import type {
  KnowledgeDetailView,
  KnowledgeDiffView,
  KnowledgeHistoryPage,
  KnowledgeListFilters,
  KnowledgeListPage,
  KnowledgeUpdateInput,
  KnowledgeUpdateResult,
  TaskKnowledgeView,
} from "./model";

export type KnowledgeErrorCode =
  | "not_found"
  | "revision_unavailable"
  | "revision_redacted"
  | "disabled"
  | "forbidden"
  | "unavailable";

export class KnowledgeAdapterError extends Error {
  readonly code: KnowledgeErrorCode;
  constructor(code: KnowledgeErrorCode, message: string) {
    super(message);
    this.name = "KnowledgeAdapterError";
    this.code = code;
  }
}

export interface KnowledgeAdapter {
  /** Separates caches for live project scopes. */
  cacheKey?: string;
  list(filters: KnowledgeListFilters, cursor: string | null): Promise<KnowledgeListPage>;
  /** `revisionId` null reads the current revision; a named one is pinned and may be historical. */
  show(recordId: string, revisionId: string | null): Promise<KnowledgeDetailView>;
  history(recordId: string, cursor: string | null): Promise<KnowledgeHistoryPage>;
  diff(recordId: string, fromRevisionId: string, toRevisionId: string): Promise<KnowledgeDiffView>;
  update(input: KnowledgeUpdateInput): Promise<KnowledgeUpdateResult>;
  /** The informational knowledge a task carries: pinned citations and readable links. */
  taskKnowledge(taskId: string): Promise<TaskKnowledgeView>;
}

const GENERIC_MESSAGES: Record<KnowledgeErrorCode, string> = {
  not_found: "This record is not available.",
  revision_unavailable: "This revision is not available.",
  revision_redacted: "This revision was redacted.",
  disabled: "Knowledge is disabled for this project.",
  forbidden: "You are not allowed to do that.",
  unavailable: "Knowledge is temporarily unavailable.",
};

/**
 * The one line an error state shows. Typed adapter errors map to fixed copy
 * so a denied and a missing record read the same; anything else is reported
 * generically rather than echoing server text into the page.
 */
export function describeKnowledgeError(error: unknown): string {
  if (error instanceof KnowledgeAdapterError) return GENERIC_MESSAGES[error.code];
  return "Could not load knowledge.";
}
