import { useId, useState, type FormEvent } from "react";
import type { KnowledgeAdapter } from "../../pages/knowledge/adapter";
import {
  KNOWLEDGE_CATEGORIES,
  type KnowledgeCategory,
  type KnowledgeDetailView,
  type KnowledgeEditDraft,
  type KnowledgeRevisionRef,
} from "../../pages/knowledge/model";
import { diffView, useKnowledgeDiff, useKnowledgeUpdate } from "../../pages/knowledge/useKnowledge";
import KnowledgeDiff from "./KnowledgeDiff";
import KnowledgeEditConflict from "./KnowledgeEditConflict";

export interface KnowledgeEditFormProps {
  adapter: KnowledgeAdapter;
  /** Must be the current revision; the pane disables Edit otherwise. */
  detail: KnowledgeDetailView;
  onSaved: (revision: KnowledgeRevisionRef) => void;
  onCancel: () => void;
}

const INPUT = "w-full rounded border border-gray-700 bg-gray-900 px-2 py-1 text-sm text-gray-200";
const LABEL = "block text-xs text-gray-400";

function draftFrom(detail: KnowledgeDetailView): KnowledgeEditDraft {
  return {
    title: detail.title,
    body: detail.body ?? "",
    summary: detail.summary,
    category: detail.category,
    tags: detail.tags,
    reason: "",
  };
}

function newIdempotencyKey(): string {
  const generator = globalThis.crypto as { randomUUID?: () => string } | undefined;
  return generator?.randomUUID?.() ?? `edit-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Guarded edit. The form holds the revision token it observed and sends it
 * as `if_revision` with one idempotency key for the whole attempt. A
 * conflict blocks saving until the editor reloads; the rebased form keeps
 * the draft but is never submitted on the editor's behalf.
 */
export default function KnowledgeEditForm({ adapter, detail, onSaved, onCancel }: KnowledgeEditFormProps) {
  const id = useId();
  const [draft, setDraft] = useState<KnowledgeEditDraft>(() => draftFrom(detail));
  const [tagsText, setTagsText] = useState(() => detail.tags.join(", "));
  const [observed, setObserved] = useState<KnowledgeRevisionRef>({ revisionId: detail.viewed.revisionId, sequence: detail.viewed.sequence });
  const [idempotencyKey] = useState(newIdempotencyKey);
  const [conflict, setConflict] = useState<KnowledgeRevisionRef | null>(null);
  const [comparing, setComparing] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const update = useKnowledgeUpdate(adapter);
  const diff = useKnowledgeDiff(
    adapter, detail.recordId,
    comparing && conflict ? observed.revisionId : null,
    comparing && conflict ? conflict.revisionId : null,
  );

  const set = <K extends keyof KnowledgeEditDraft>(key: K, value: KnowledgeEditDraft[K]) =>
    setDraft((previous) => ({ ...previous, [key]: value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (conflict) return;
    setFailure(null);
    setNotice(null);
    const tags = tagsText.split(",").map((tag) => tag.trim()).filter(Boolean);
    const result = await update.mutateAsync({
      recordId: detail.recordId,
      ifRevision: observed.revisionId,
      draft: { ...draft, tags },
      idempotencyKey,
    });
    switch (result.outcome) {
      case "updated":
      case "replayed":
        onSaved(result.revision);
        return;
      case "unchanged":
        setNotice("No changes to save.");
        return;
      case "conflict":
        setConflict(result.current);
        return;
      case "forbidden":
      case "invalid":
        setFailure(result.message);
        return;
    }
  }

  async function reload() {
    setReloading(true);
    try {
      const fresh = await adapter.show(detail.recordId, null);
      setObserved({ revisionId: fresh.current.revisionId, sequence: fresh.current.sequence });
      setConflict(null);
      setComparing(false);
      setNotice(`Rebased onto revision ${fresh.current.sequence}. Review your changes, then save again.`);
    } catch {
      setFailure("Could not reload the current revision.");
    } finally {
      setReloading(false);
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} aria-label={`Edit ${detail.alias}`} className="space-y-3">
      <p className="text-xs text-gray-500">
        Editing from revision {observed.sequence}
        <span className="sr-only"> ({observed.revisionId})</span>
        . Saving creates a new unverified revision.
      </p>
      {conflict && (
        <KnowledgeEditConflict
          observed={observed}
          current={conflict}
          onReload={() => void reload()}
          onCompare={() => setComparing((value) => !value)}
          reloading={reloading}
          comparing={comparing}
        />
      )}
      {conflict && comparing && (
        <KnowledgeDiff from={observed} to={conflict} view={diffView(diff)} onClose={() => setComparing(false)} />
      )}
      {notice && <p role="status" className="text-xs text-indigo-200">{notice}</p>}
      {failure && <p role="alert" className="text-xs text-red-300">{failure}</p>}

      <div>
        <label htmlFor={`${id}-title`} className={LABEL}>Title</label>
        <input id={`${id}-title`} className={INPUT} value={draft.title} onChange={(event) => set("title", event.target.value)} required maxLength={240} />
      </div>
      <div>
        <label htmlFor={`${id}-summary`} className={LABEL}>Summary</label>
        <input id={`${id}-summary`} className={INPUT} value={draft.summary ?? ""} onChange={(event) => set("summary", event.target.value || null)} />
      </div>
      <div className="flex flex-wrap gap-3">
        <div>
          <label htmlFor={`${id}-category`} className={LABEL}>Category</label>
          <select id={`${id}-category`} className={INPUT} value={draft.category} onChange={(event) => set("category", event.target.value as KnowledgeCategory)}>
            {KNOWLEDGE_CATEGORIES.map((category) => <option key={category} value={category}>{category}</option>)}
          </select>
        </div>
        <div className="min-w-0 flex-1">
          <label htmlFor={`${id}-tags`} className={LABEL}>Tags (comma separated)</label>
          <input id={`${id}-tags`} className={INPUT} value={tagsText} onChange={(event) => setTagsText(event.target.value)} />
        </div>
      </div>
      <div>
        <label htmlFor={`${id}-body`} className={LABEL}>Body (Markdown)</label>
        <textarea id={`${id}-body`} className={`${INPUT} min-h-40 font-mono`} value={draft.body} onChange={(event) => set("body", event.target.value)} />
      </div>
      <div>
        <label htmlFor={`${id}-reason`} className={LABEL}>Why this change</label>
        <input id={`${id}-reason`} className={INPUT} value={draft.reason} onChange={(event) => set("reason", event.target.value)} />
      </div>
      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={conflict !== null || update.isPending}
          className="rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50">
          {update.isPending ? "Saving…" : "Save"}
        </button>
        <button type="button" onClick={onCancel} className="rounded px-3 py-1 text-sm text-gray-300 hover:bg-gray-800">Cancel</button>
      </div>
    </form>
  );
}
