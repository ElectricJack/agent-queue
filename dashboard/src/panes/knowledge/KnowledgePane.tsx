import { useId, useState, type KeyboardEvent } from "react";
import MarkdownPreview from "../../components/MarkdownPreview";
import type { KnowledgeAdapter } from "../../pages/knowledge/adapter";
import { KnowledgeBadgeRow } from "../../pages/knowledge/KnowledgeBadges";
import type { KnowledgeAction, KnowledgeDetailView, KnowledgeRevisionRef } from "../../pages/knowledge/model";
import {
  detailView, diffView, historyView, useKnowledgeDetail, useKnowledgeDiff, useKnowledgeHistory,
} from "../../pages/knowledge/useKnowledge";
import KnowledgeActions from "./KnowledgeActions";
import KnowledgeDiff from "./KnowledgeDiff";
import KnowledgeEditForm from "./KnowledgeEditForm";
import KnowledgeHistory from "./KnowledgeHistory";
import KnowledgeProvenance from "./KnowledgeProvenance";
import KnowledgeVerification from "./KnowledgeVerification";

export interface KnowledgePaneProps {
  adapter: KnowledgeAdapter;
  recordId: string;
  /** null reads the current revision; an id pins a (possibly historical) one. */
  revisionId: string | null;
  onRevisionChange: (revisionId: string | null) => void;
  onOpenRecord?: (recordId: string, revisionId: string | null) => void;
  onOpenTask?: (taskId: string) => void;
  /** Actions this slice only announces; K10 wires the real flows. */
  onAction?: (action: KnowledgeAction, detail: KnowledgeDetailView) => void;
}

type Tab = "body" | "history" | "provenance" | "verification";
const TABS: { id: Tab; label: string }[] = [
  { id: "body", label: "Body" },
  { id: "history", label: "History" },
  { id: "provenance", label: "Provenance" },
  { id: "verification", label: "Verification" },
];

/**
 * One knowledge record: header with the facet badges, the server-allowed
 * actions, and tabs for body, history, provenance and verification. The
 * pane shows exactly the revision it was asked for; a historical one is
 * announced and offers the current one, never the other way round. There is
 * no pane manifest here yet: K10 registers it once authorization is wired.
 */
export default function KnowledgePane({
  adapter, recordId, revisionId, onRevisionChange, onOpenRecord, onOpenTask, onAction,
}: KnowledgePaneProps) {
  const id = useId();
  const [tab, setTab] = useState<Tab>("body");
  const [editing, setEditing] = useState(false);
  const [compare, setCompare] = useState<{ from: string; to: string } | null>(null);

  const detailQuery = useKnowledgeDetail(adapter, recordId, revisionId);
  const detail = detailView(detailQuery);
  const historyQuery = useKnowledgeHistory(adapter, recordId, tab === "history");
  const diffQuery = useKnowledgeDiff(adapter, recordId, compare?.from ?? null, compare?.to ?? null);

  if (detail.status === "loading") return <p role="status" className="p-4 text-sm text-gray-500">Loading record…</p>;
  if (detail.status === "error") {
    return (
      <div className="space-y-2 p-4">
        <p role="alert" className="text-sm text-red-300">{detail.message}</p>
        {revisionId !== null && (
          <button type="button" onClick={() => onRevisionChange(null)}
            className="rounded border border-gray-700 px-2 py-1 text-xs text-gray-200 hover:bg-gray-800">
            View current revision
          </button>
        )}
      </div>
    );
  }
  const record = detail.data;
  const historical = !record.viewed.isCurrent;
  const diffRefs = compare ? refsFor(record, compare) : null;

  function onTabKey(event: KeyboardEvent<HTMLDivElement>) {
    const index = TABS.findIndex((t) => t.id === tab);
    if (event.key === "ArrowRight") { event.preventDefault(); setTab(TABS[(index + 1) % TABS.length]!.id); }
    if (event.key === "ArrowLeft") { event.preventDefault(); setTab(TABS[(index - 1 + TABS.length) % TABS.length]!.id); }
  }

  return (
    <article aria-labelledby={`${id}-title`} className="flex h-full min-h-0 flex-col gap-3 p-4">
      <header className="space-y-2">
        <h2 id={`${id}-title`} className="text-base font-semibold text-gray-100 [overflow-wrap:anywhere]">{record.title}</h2>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500">
          <span className="font-mono">{record.alias}</span>
          <span>{record.scope.kind === "global" ? "global" : `project ${record.scope.projectId}`}</span>
          <span>
            revision {record.viewed.sequence}
            {record.viewed.isCurrent ? " (current)" : ` of ${record.current.sequence}`}
          </span>
        </div>
        <KnowledgeBadgeRow
          item={{
            kind: "knowledge",
            category: record.category,
            lifecycle: record.lifecycle,
            verification: record.verification.state,
            authoritative: record.verification.authority !== null,
            stale: record.freshness.stale,
          }}
          staleReason={record.freshness.staleReason}
        />
        {historical && (
          <div role="note" className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-400/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
            <span>Viewing revision {record.viewed.sequence} of {record.current.sequence} — not the current revision.</span>
            <button type="button" onClick={() => onRevisionChange(null)}
              className="rounded border border-amber-400/40 px-2 py-0.5 text-amber-100 hover:bg-amber-500/10">
              View current
            </button>
          </div>
        )}
        {record.lifecycle === "retired" && (
          <div role="note" className="flex flex-wrap items-center gap-2 rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-xs text-gray-300">
            <span>Retired{record.retirementReason ? `: ${record.retirementReason}` : ""}.</span>
            {record.successor && (
              <span className="flex items-center gap-1">
                Successor: {record.successor.title ?? <span className="italic text-gray-500">Unavailable</span>}
                {record.successor.title !== null && onOpenRecord && (
                  <button type="button" onClick={() => onOpenRecord(record.successor!.recordId, null)}
                    className="rounded border border-gray-700 px-2 py-0.5 text-gray-200 hover:bg-gray-800">
                    Open
                  </button>
                )}
              </span>
            )}
          </div>
        )}
        <KnowledgeActions
          allowed={record.allowedActions}
          onEdit={() => { setTab("body"); setEditing(true); }}
          onHistory={() => setTab("history")}
          onAction={(action) => onAction?.(action, record)}
          editDisabledReason={historical ? "Edit from the current revision" : record.redacted ? "A redacted revision cannot be edited" : null}
        />
      </header>

      <div role="tablist" aria-label="Record sections" onKeyDown={onTabKey} className="flex gap-1 border-b border-gray-800">
        {TABS.map((t) => (
          <button
            key={t.id}
            id={`${id}-tab-${t.id}`}
            role="tab"
            type="button"
            aria-selected={tab === t.id}
            aria-controls={`${id}-panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => setTab(t.id)}
            className={`px-3 py-1.5 text-xs ${tab === t.id ? "border-b-2 border-indigo-400 text-indigo-300" : "text-gray-400 hover:text-gray-200"}`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div
        id={`${id}-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`${id}-tab-${tab}`}
        className="dashboard-scrollbar min-h-0 flex-1 overflow-y-auto"
      >
        {tab === "body" && (
          editing ? (
            <KnowledgeEditForm
              adapter={adapter}
              detail={record}
              onSaved={() => { setEditing(false); onRevisionChange(null); }}
              onCancel={() => setEditing(false)}
            />
          ) : record.redacted ? (
            <p role="note" className="rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm text-gray-400">
              This revision was redacted. Its content is permanently unavailable.
            </p>
          ) : (
            <div className="space-y-3">
              {record.summary && <p className="text-sm text-gray-300">{record.summary}</p>}
              <MarkdownPreview source={record.body ?? ""} />
              {record.tags.length > 0 && (
                <p className="text-xs text-gray-500">{record.tags.map((tag) => `#${tag}`).join(" ")}</p>
              )}
            </div>
          )
        )}
        {tab === "history" && (
          <div className="space-y-3">
            {diffRefs && (
              <KnowledgeDiff from={diffRefs.from} to={diffRefs.to} view={diffView(diffQuery)} onClose={() => setCompare(null)} />
            )}
            <KnowledgeHistory
              view={historyView(historyQuery)}
              viewedRevisionId={revisionId}
              onView={onRevisionChange}
              onCompare={(from, to) => setCompare({ from, to })}
              onLoadMore={historyQuery.hasNextPage ? () => void historyQuery.fetchNextPage() : undefined}
              loadingMore={historyQuery.isFetchingNextPage}
            />
          </div>
        )}
        {tab === "provenance" && <KnowledgeProvenance detail={record} onOpenRecord={onOpenRecord} onOpenTask={onOpenTask} />}
        {tab === "verification" && <KnowledgeVerification detail={record} />}
      </div>
    </article>
  );
}

/** Sequence numbers for the diff heading, from whatever the pane already knows. */
function refsFor(record: KnowledgeDetailView, compare: { from: string; to: string }): { from: KnowledgeRevisionRef; to: KnowledgeRevisionRef } {
  const known = new Map<string, number>([
    [record.viewed.revisionId, record.viewed.sequence],
    [record.current.revisionId, record.current.sequence],
  ]);
  const seq = (revisionId: string) =>
    known.get(revisionId) ?? (Number.parseInt(revisionId.split("-").pop() ?? "", 10) || 0);
  return {
    from: { revisionId: compare.from, sequence: seq(compare.from) },
    to: { revisionId: compare.to, sequence: seq(compare.to) },
  };
}
