/**
 * Typed view models for the Knowledge presentation slice (plan §10, K10).
 *
 * These are the shapes the components render. They are deliberately not the
 * generated API types: main has no knowledge routes yet (K03 ships them), so
 * the live adapter K10 writes maps `knowledge_show` & co. onto these and the
 * components stay untouched. Names follow the approved plan's canonical
 * snapshot (category, lifecycle, verification, sources, outgoing links) so
 * that mapping is a rename, not a redesign.
 *
 * Nothing here is an execution concept: no status, priority, assignment or
 * progress. A knowledge card never grows task controls.
 */

export const KNOWLEDGE_CATEGORIES = [
  "fact", "decision", "policy", "procedure", "incident", "reference", "note",
] as const;
export type KnowledgeCategory = (typeof KNOWLEDGE_CATEGORIES)[number];

export const KNOWLEDGE_LIFECYCLES = ["active", "retired"] as const;
export type KnowledgeLifecycle = (typeof KNOWLEDGE_LIFECYCLES)[number];

export const KNOWLEDGE_VERIFICATIONS = ["unverified", "verified", "disputed"] as const;
export type KnowledgeVerification = (typeof KNOWLEDGE_VERIFICATIONS)[number];

/** The two record domains; mixed views tag every item with one. */
export type RecordKind = "task" | "knowledge";

export const LINK_TYPES = [
  "references", "motivated_by", "produces", "supports", "contradicts", "supersedes",
] as const;
export type LinkType = (typeof LINK_TYPES)[number];

/**
 * Actions a knowledge view may offer. The server decides which apply to the
 * viewer and the record; the UI only renders what it is handed. Workers see
 * `propose_correction` on protected records instead of `edit`.
 */
export const KNOWLEDGE_ACTIONS = [
  "edit", "history", "link", "retire", "restore", "create_task", "propose_correction",
] as const;
export type KnowledgeAction = (typeof KNOWLEDGE_ACTIONS)[number];

export const KNOWLEDGE_ACTION_LABELS: Record<KnowledgeAction, string> = {
  edit: "Edit",
  history: "History",
  link: "Link",
  retire: "Retire",
  restore: "Restore",
  create_task: "Create task",
  propose_correction: "Propose correction",
};

export interface KnowledgeRevisionRef {
  revisionId: string;
  sequence: number;
}

export type KnowledgeScopeView =
  | { kind: "project"; projectId: string }
  | { kind: "global"; projectId: null };

/** One row of `knowledge_list`: metadata only, never a body. */
export interface KnowledgeListItemView {
  kind: "knowledge";
  recordId: string;
  alias: string;
  title: string;
  summary: string | null;
  category: KnowledgeCategory;
  lifecycle: KnowledgeLifecycle;
  verification: KnowledgeVerification;
  /** An active authority grant bound to the current revision. */
  authoritative: boolean;
  /** Server-computed staleness (recheck or validity passed); separate from lifecycle. */
  stale: boolean;
  staleReason: string | null;
  tags: string[];
  /** RFC3339 UTC. */
  updatedAt: string;
  current: KnowledgeRevisionRef;
}

export interface KnowledgeListFilters {
  query: string;
  /** "" means any. */
  category: "" | KnowledgeCategory;
  /** "" means any; the default list shows active records only. */
  lifecycle: "" | KnowledgeLifecycle;
  /** "" means any. */
  verification: "" | KnowledgeVerification;
}

export const DEFAULT_KNOWLEDGE_FILTERS: KnowledgeListFilters = {
  query: "",
  category: "",
  lifecycle: "active",
  verification: "",
};

export interface KnowledgeListPage {
  items: KnowledgeListItemView[];
  nextCursor: string | null;
}

export type KnowledgeSourceType = "review" | "task" | "git" | "artifact" | "url" | "legacy";

/**
 * A typed source descriptor from the snapshot. `evidence` is the server's
 * verdict on retained evidence; the UI never upgrades an unavailable source.
 */
export interface KnowledgeSourceView {
  sourceId: string;
  type: KnowledgeSourceType;
  /** Human label, e.g. "review rev-fleet-cascade r2" or a URL. */
  label: string;
  /** In-dashboard destination for task/review sources; external URLs stay text. */
  href: string | null;
  evidence: "retained" | "unretained" | "unavailable";
}

export type KnowledgeLinkResolution =
  | "current"      // floating link hydrated to the target's current revision
  | "pinned"       // exact revision resolved
  | "unavailable"  // pinned revision or target gone
  | "redacted"     // pinned revision redacted
  | "unauthorized"; // viewer may not read the endpoint: no title, no snippet

export interface KnowledgeLinkView {
  linkId: string;
  version: number;
  type: LinkType;
  /** Relative to the record being viewed. */
  direction: "outgoing" | "incoming";
  endpoint: {
    kind: RecordKind;
    id: string;
    /** `null` whenever the viewer may not read the endpoint. */
    title: string | null;
  };
  /** `null` is a floating link. */
  pinnedRevision: KnowledgeRevisionRef | null;
  resolution: KnowledgeLinkResolution;
  /** Links are informational even when their titles resemble commands. */
  edgeDomain: "informational";
}

export interface KnowledgeAuthorityView {
  kind: "policy";
  reviewId: string | null;
  reason: string | null;
  grantedBy: string;
  grantedAt: string;
}

export interface KnowledgeVerificationView {
  state: KnowledgeVerification;
  lastVerifiedAt: string | null;
  lastVerifiedBy: string | null;
  /** Present only while an authority grant is active for the current revision. */
  authority: KnowledgeAuthorityView | null;
}

export interface KnowledgeFreshnessView {
  validFrom: string | null;
  validUntil: string | null;
  recheckAt: string | null;
  stale: boolean;
  staleReason: string | null;
}

export type KnowledgeChangeKind =
  | "create" | "update" | "retire" | "restore" | "verify" | "dispute" | "redact";

/** The revision envelope the detail view is showing. */
export interface KnowledgeViewedRevision extends KnowledgeRevisionRef {
  isCurrent: boolean;
  createdAt: string;
  actorId: string;
  changeKind: KnowledgeChangeKind;
  contentSha256: string | null;
}

export interface KnowledgeDetailView {
  /** Authorized snapshot for submitting a complete protected correction. */
  proposalSnapshot?: Record<string, unknown>;
  kind: "knowledge";
  recordId: string;
  alias: string;
  scope: KnowledgeScopeView;
  title: string;
  /** Markdown; `null` when the viewed revision's payload is redacted. */
  body: string | null;
  summary: string | null;
  category: KnowledgeCategory;
  tags: string[];
  lifecycle: KnowledgeLifecycle;
  retirementReason: string | null;
  successor: { recordId: string; alias: string; title: string | null } | null;
  verification: KnowledgeVerificationView;
  freshness: KnowledgeFreshnessView;
  viewed: KnowledgeViewedRevision;
  current: KnowledgeRevisionRef;
  /** Server-returned; the only thing that makes an action button appear. */
  allowedActions: KnowledgeAction[];
  /** Protected records take proposals from workers instead of direct edits. */
  protection: "none" | "protected";
  /** The viewed revision's payload is a redaction tombstone. */
  redacted: boolean;
  sources: KnowledgeSourceView[];
  links: KnowledgeLinkView[];
}

export interface KnowledgeHistoryEntryView extends KnowledgeRevisionRef {
  createdAt: string;
  actorId: string;
  changeKind: KnowledgeChangeKind;
  changeReason: string | null;
  verification: KnowledgeVerification;
  redacted: boolean;
  isCurrent: boolean;
}

export interface KnowledgeHistoryPage {
  entries: KnowledgeHistoryEntryView[];
  nextCursor: string | null;
}

export interface KnowledgeDiffBlock {
  op: "equal" | "added" | "removed";
  text: string;
}

export interface KnowledgeDiffView {
  from: KnowledgeRevisionRef;
  to: KnowledgeRevisionRef;
  blocks: KnowledgeDiffBlock[];
}

/** The fields an edit form submits. Omitted fields stay unchanged on the server. */
export interface KnowledgeEditDraft {
  title: string;
  body: string;
  summary: string | null;
  category: KnowledgeCategory;
  tags: string[];
  /** User-supplied change reason; stored in the payload so it can be redacted. */
  reason: string;
}

export interface KnowledgeUpdateInput {
  recordId: string;
  /** The revision token the editor observed. Absent is a 428 server-side; the form never omits it. */
  ifRevision: string;
  draft: KnowledgeEditDraft;
  idempotencyKey: string;
}

/**
 * Typed outcomes of a guarded update. A conflict is a *result*, not an
 * exception: the live adapter maps HTTP 409 `record.revision_conflict` onto
 * `conflict` with the current token the server returned, so the form can show
 * reload/compare without ever retrying against the new head on its own.
 */
export type KnowledgeUpdateResult =
  | { outcome: "updated"; revision: KnowledgeRevisionRef }
  | { outcome: "replayed"; revision: KnowledgeRevisionRef }
  | { outcome: "unchanged" }
  | { outcome: "conflict"; current: KnowledgeRevisionRef }
  | { outcome: "forbidden"; message: string }
  | { outcome: "invalid"; message: string };

/** A citation a task carries: always an exact revision, never "current". */
export interface KnowledgeCitationView {
  citationId: string;
  recordId: string;
  alias: string;
  /** `null` when the viewer may not read the record. */
  title: string | null;
  revision: KnowledgeRevisionRef;
  isCurrent: boolean;
  resolution: Exclude<KnowledgeLinkResolution, "current">;
  citedAt: string;
}

export interface TaskKnowledgeView {
  taskId: string;
  citations: KnowledgeCitationView[];
  links: KnowledgeLinkView[];
}

/**
 * What a presentation component receives for anything asynchronous. The
 * hooks turn React Query state into this; tests build it directly.
 */
export type AsyncView<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: T };

export function formatKnowledgeTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function linkTypeLabel(type: LinkType): string {
  return type.replace(/_/g, " ");
}
