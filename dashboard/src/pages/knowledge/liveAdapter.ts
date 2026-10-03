/** Generated SDK boundary; presentation components continue to use their delivered adapter. */
import {
  knowledgeShow, knowledgeHistory, knowledgeDiff, knowledgeUpdate, recordSearch,
  recordShow, linkList,
} from "../../api/client";
import { KnowledgeAdapterError, type KnowledgeAdapter } from "./adapter";
import type {
  KnowledgeDetailView, KnowledgeListItemView, KnowledgeLinkView, KnowledgeSourceView,
  KnowledgeCategory, KnowledgeLifecycle, KnowledgeVerification, KnowledgeAction,
  KnowledgeChangeKind,
} from "./model";

export const object = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" ? value as Record<string, unknown> : {};
const string = (value: unknown) => typeof value === "string" ? value : "";
const nullable = (value: unknown) => typeof value === "string" ? value : null;
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const number = (value: unknown) => typeof value === "number" ? value : 0;

export function knowledgeFailure(error: unknown): never {
  const code = string(object(object(error).payload).error_code);
  const mapped = code === "record.revision_redacted" ? "revision_redacted"
    : code === "record.revision_unavailable" ? "revision_unavailable"
    : code === "record.not_found" ? "not_found"
    : code === "knowledge.disabled" ? "disabled"
    : code === "record.forbidden" || code === "knowledge.read_only" ? "forbidden" : "unavailable";
  throw new KnowledgeAdapterError(mapped, "Knowledge request failed.");
}

function row(value: unknown): KnowledgeListItemView {
  const item = object(value);
  return {
    kind: "knowledge", recordId: string(item.record_id), alias: string(item.knowledge_alias),
    title: string(item.title), summary: nullable(item.summary),
    category: string(item.category) as KnowledgeCategory,
    lifecycle: string(item.lifecycle) as KnowledgeLifecycle,
    verification: string(item.verification) as KnowledgeVerification,
    authoritative: item.authoritative === true, stale: item.stale === true,
    staleReason: nullable(item.stale_reason), tags: array(item.tags).map(string),
    updatedAt: string(item.updated_at),
    current: { revisionId: string(item.revision_id), sequence: number(item.sequence) },
  };
}

export function createLiveKnowledgeAdapter(projectId: string): KnowledgeAdapter {
  const scope = { project_id: projectId };
  const identity = (id: string) => `record:${id}`;
  async function read(id: string, revisionId: string | null = null) {
    try {
      const { data } = await knowledgeShow({ body: { ...scope, identity: identity(id), revision_id: revisionId } });
      return object(data);
    } catch (error) { return knowledgeFailure(error); }
  }
  async function links(id: string, revisionId: string | null): Promise<KnowledgeLinkView[]> {
    const { data } = await linkList({ body: { ...scope, identity: id, revision_id: revisionId } });
    return Promise.all(array(data?.links).map(async (value) => {
      const link = object(value);
      const pin = nullable(link.target_revision_id);
      const recordId = string(link.target_record_id);
      let endpoint = { kind: "knowledge" as "knowledge" | "task", id: recordId, title: null as string | null };
      let sequence = 0;
      let resolution: KnowledgeLinkView["resolution"] = pin ? "pinned" : "current";
      try {
        const { data: target } = await recordShow({ body: { ...scope, identity: identity(recordId), revision_id: pin } });
        const record = object(target);
        const kind = record.kind === "task" ? "task" : "knowledge";
        const content = object(kind === "task" ? record.task : record.snapshot);
        endpoint = { kind, id: kind === "task" ? string(content.id) : recordId, title: nullable(content.title) };
        sequence = number(record.sequence);
      } catch (error) {
        const code = object(object(error).payload).error_code;
        resolution = code === "record.revision_redacted" ? "redacted" : "unavailable";
      }
      return { linkId: string(link.link_id), version: number(link.version),
        type: string(link.link_type) as KnowledgeLinkView["type"], direction: "outgoing" as const,
        endpoint, pinnedRevision: pin ? { revisionId: pin, sequence } : null,
        resolution, edgeDomain: "informational" as const };
    }));
  }
  async function show(id: string, revisionId: string | null): Promise<KnowledgeDetailView> {
    const value = await read(id, revisionId);
    const snapshot = object(value.snapshot);
    const current = { revisionId: string(value.current_revision_id), sequence: number(value.current_sequence) };
    const viewedId = string(value.revision_id);
    const authority = object(value.authority);
    return {
      kind: "knowledge", recordId: string(value.record_id), alias: string(value.knowledge_alias),
      scope: string(value.scope_key) === "global" ? { kind: "global", projectId: null } : { kind: "project", projectId },
      title: string(snapshot.title), body: nullable(snapshot.body), summary: nullable(snapshot.summary),
      category: string(snapshot.category) as KnowledgeCategory, tags: array(snapshot.tags).map(string),
      lifecycle: string(snapshot.lifecycle) as KnowledgeLifecycle, retirementReason: nullable(snapshot.retirement_reason),
      successor: snapshot.successor_record_id ? { recordId: string(snapshot.successor_record_id), alias: "", title: null } : null,
      verification: { state: string(snapshot.verification) as KnowledgeVerification,
        lastVerifiedAt: nullable(snapshot.last_verified_at), lastVerifiedBy: nullable(snapshot.last_verified_by),
        authority: value.authority ? { kind: "policy", reviewId: nullable(authority.review_id), reason: nullable(authority.reason),
          grantedBy: string(authority.actor_id), grantedAt: string(authority.created_at) } : null },
      freshness: { validFrom: nullable(snapshot.valid_from), validUntil: nullable(snapshot.valid_until),
        recheckAt: nullable(snapshot.recheck_at), stale: value.stale === true, staleReason: nullable(value.stale_reason) },
      current, viewed: { revisionId: viewedId, sequence: number(value.sequence), isCurrent: viewedId === current.revisionId,
        createdAt: string(value.created_at), actorId: string(value.actor_id),
        changeKind: string(value.change_kind) as KnowledgeChangeKind, contentSha256: nullable(value.content_sha256) },
      allowedActions: array(value.allowed_actions).map(string) as KnowledgeAction[],
      protection: value.protection === "protected" ? "protected" : "none", redacted: false,
      sources: array(snapshot.sources).map((value): KnowledgeSourceView => {
        const source = object(value);
        return { sourceId: string(source.source_id), type: string(source.kind) as KnowledgeSourceView["type"],
          label: string(source.label) || string(source.task_id) || string(source.url) || string(source.source_id),
          href: source.kind === "url" && /^https?:\/\//i.test(string(source.url)) ? string(source.url) : null,
          evidence: source.artifact_id || source.kind === "review" ? "retained" : "unretained" };
      }),
      links: await links(identity(id), revisionId),
      proposalSnapshot: snapshot,
    };
  }
  return {
    cacheKey: projectId,
    async list(filters, cursor) {
      try {
        const { data } = await recordSearch({ body: { ...scope, kind: "knowledge", query: filters.query, cursor,
          category: filters.category || null, lifecycle: filters.lifecycle || null,
          verification: filters.verification || null, include_retired: filters.lifecycle !== "active", include_disputed: true } });
        return { items: array(data?.items).map(row), nextCursor: data?.next_cursor ?? null };
      } catch (error) { return knowledgeFailure(error); }
    },
    show,
    async history(id, cursor) {
      try {
        const { data } = await knowledgeHistory({ body: { ...scope, identity: identity(id), before_sequence: cursor ? Number(cursor) : null } });
        const current = await read(id);
        const entries = array(data?.revisions).map((value) => {
          const item = object(value);
          return { revisionId: string(item.revision_id), sequence: number(item.sequence), createdAt: string(item.created_at),
            actorId: string(item.actor_id), changeKind: string(item.change_kind) as KnowledgeChangeKind,
            changeReason: nullable(item.change_reason), verification: string(item.verification ?? "unverified") as KnowledgeVerification,
            redacted: item.availability === "record.revision_redacted", isCurrent: item.revision_id === current.revision_id };
        });
        return { entries, nextCursor: entries.length === 25 ? String(entries[entries.length - 1]!.sequence) : null };
      } catch (error) { return knowledgeFailure(error); }
    },
    async diff(id, fromRevisionId, toRevisionId) {
      try {
        const { data } = await knowledgeDiff({ body: { ...scope, identity: identity(id), from_revision: fromRevisionId, to_revision: toRevisionId } });
        const [from, to] = await Promise.all([read(id, fromRevisionId), read(id, toRevisionId)]);
        return { from: { revisionId: fromRevisionId, sequence: number(from.sequence) },
          to: { revisionId: toRevisionId, sequence: number(to.sequence) },
          blocks: array(data?.changes).flatMap((value) => {
            const change = object(value);
            return [{ op: "removed" as const, text: `${string(change.field)}: ${JSON.stringify(change.from)}` },
              { op: "added" as const, text: `${string(change.field)}: ${JSON.stringify(change.to)}` }];
          }) };
      } catch (error) { return knowledgeFailure(error); }
    },
    async update(input) {
      try {
        const { reason, ...draft } = input.draft;
        const { data } = await knowledgeUpdate({ body: { ...scope, identity: identity(input.recordId), if_revision: input.ifRevision,
          idempotency_key: input.idempotencyKey, ...draft, change_reason: reason } });
        return { outcome: data?.outcome === "replayed" ? "replayed" : "updated",
          revision: { revisionId: data?.revision_id ?? "", sequence: number(object(data).sequence) } };
      } catch (error) {
        const payload = object(object(error).payload);
        if (payload.error_code === "record.revision_conflict") return { outcome: "conflict",
          current: { revisionId: string(payload.current_token), sequence: number(payload.current_sequence) } };
        return { outcome: "forbidden", message: "The correction could not be saved." };
      }
    },
    async taskKnowledge(taskId) {
      try { return { taskId, citations: [], links: await links(`task:${taskId}`, null) }; }
      catch (error) { return knowledgeFailure(error); }
    },
  };
}
