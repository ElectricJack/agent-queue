import { useId } from "react";
import type { KnowledgeAdapter } from "../../pages/knowledge/adapter";
import { KindBadge } from "../../pages/knowledge/KnowledgeBadges";
import {
  formatKnowledgeTimestamp,
  linkTypeLabel,
  type KnowledgeCitationView,
  type KnowledgeLinkView,
} from "../../pages/knowledge/model";
import { taskKnowledgeView, useTaskKnowledge } from "../../pages/knowledge/useKnowledge";

export interface TaskKnowledgePanelProps {
  taskId: string;
  adapter: KnowledgeAdapter;
  /** Text the user explicitly selected in the task; the only thing Save finding submits. */
  selectedText?: string | null;
  onOpenRecord?: (recordId: string, revisionId: string | null) => void;
  onSaveFinding?: (finding: { taskId: string; text: string }) => void;
}

const SMALL = "rounded border border-gray-700 px-2 py-0.5 text-[11px] text-gray-200 hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50";

function CitationItem({ citation, onOpenRecord }: { citation: KnowledgeCitationView } & Pick<TaskKnowledgePanelProps, "onOpenRecord">) {
  const readable = citation.title !== null && citation.resolution === "pinned";
  return (
    <li data-citation-id={citation.citationId} className="rounded-lg border border-gray-800 bg-gray-900 p-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <KindBadge kind="knowledge" />
        {citation.title !== null ? (
          <span className="text-gray-200 [overflow-wrap:anywhere]">{citation.title}</span>
        ) : (
          <span className="italic text-gray-500">Unavailable record</span>
        )}
        <span className="font-mono text-gray-500">r{citation.revision.sequence}</span>
        <span data-current={citation.isCurrent} className={citation.isCurrent ? "text-gray-500" : "text-amber-300"}>
          {citation.isCurrent ? "current" : "not current — pinned"}
        </span>
        {citation.resolution !== "pinned" && (
          <span data-resolution={citation.resolution} className="text-orange-300">
            {citation.resolution === "unauthorized" ? "not readable" : citation.resolution}
          </span>
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 text-gray-500">
        <span className="font-mono">{citation.alias}</span>
        <span><span className="sr-only">Cited </span>{formatKnowledgeTimestamp(citation.citedAt)}</span>
      </div>
      {readable && onOpenRecord && (
        <div className="mt-1.5">
          {/* Always the cited revision: a citation never opens "current" on its own. */}
          <button type="button" className={SMALL} onClick={() => onOpenRecord(citation.recordId, citation.revision.revisionId)}>
            Open revision {citation.revision.sequence}
          </button>
        </div>
      )}
    </li>
  );
}

function LinkItem({ link, onOpenRecord }: { link: KnowledgeLinkView } & Pick<TaskKnowledgePanelProps, "onOpenRecord">) {
  const readable = link.endpoint.title !== null && link.endpoint.kind === "knowledge"
    && (link.resolution === "current" || link.resolution === "pinned");
  return (
    <li data-edge-domain={link.edgeDomain} data-link-id={link.linkId} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <span className="font-medium text-gray-200">{linkTypeLabel(link.type)}</span>
      <KindBadge kind={link.endpoint.kind} />
      {link.endpoint.title !== null ? (
        <span className="text-gray-200 [overflow-wrap:anywhere]">{link.endpoint.title}</span>
      ) : (
        <span className="italic text-gray-500">Unavailable</span>
      )}
      <span className="text-gray-500">{link.pinnedRevision ? `pinned to r${link.pinnedRevision.sequence}` : "current"}</span>
      {readable && onOpenRecord && (
        <button type="button" className={SMALL} onClick={() => onOpenRecord(link.endpoint.id, link.pinnedRevision?.revisionId ?? null)}>
          Open
        </button>
      )}
    </li>
  );
}

/**
 * The informational knowledge panel for a task view (`TaskDetail` details
 * tab, `TaskWorkspace`): pinned citations, readable links and an explicit
 * "Save finding" for selected text. Read-only toward the task — nothing here
 * changes task state, and Save finding copies only what was selected.
 */
export default function TaskKnowledgePanel({ taskId, adapter, selectedText = null, onOpenRecord, onSaveFinding }: TaskKnowledgePanelProps) {
  const headingId = useId();
  const view = taskKnowledgeView(useTaskKnowledge(adapter, taskId));
  const selection = selectedText?.trim() ?? "";
  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="text-sm font-semibold uppercase text-gray-500">Knowledge</h2>
        {onSaveFinding && (
          <span className="flex items-center gap-2">
            <button type="button" className={SMALL} disabled={selection === ""}
              onClick={() => onSaveFinding({ taskId, text: selection })}>
              Save finding
            </button>
            {selection === "" && <span className="text-[11px] text-gray-500">Select text in the task to save a finding.</span>}
          </span>
        )}
      </div>
      <p className="text-xs text-gray-400">Citations and informational links this task carries. Nothing here changes the task.</p>
      {view.status === "loading" && <p role="status" className="text-sm text-gray-500">Loading knowledge…</p>}
      {view.status === "error" && <p role="alert" className="text-sm text-red-300">{view.message}</p>}
      {view.status === "ready" && (
        <>
          <section aria-label="Citations" className="space-y-1.5">
            <h3 className="text-xs font-semibold text-gray-300">Citations</h3>
            {view.data.citations.length === 0 ? (
              <p className="text-xs text-gray-500">No knowledge cited yet.</p>
            ) : (
              <ul className="space-y-1.5">
                {view.data.citations.map((citation) => <CitationItem key={citation.citationId} citation={citation} onOpenRecord={onOpenRecord} />)}
              </ul>
            )}
          </section>
          <section aria-label="Knowledge links" className="space-y-1.5">
            <h3 className="text-xs font-semibold text-gray-300">Links</h3>
            {view.data.links.length === 0 ? (
              <p className="text-xs text-gray-500">No knowledge links.</p>
            ) : (
              <ul className="space-y-1">
                {view.data.links.map((link) => <LinkItem key={link.linkId} link={link} onOpenRecord={onOpenRecord} />)}
              </ul>
            )}
          </section>
        </>
      )}
    </section>
  );
}
