import { KindBadge } from "../../pages/knowledge/KnowledgeBadges";
import {
  linkTypeLabel,
  type KnowledgeDetailView,
  type KnowledgeLinkView,
  type KnowledgeSourceView,
} from "../../pages/knowledge/model";

export interface KnowledgeProvenanceProps {
  detail: KnowledgeDetailView;
  onOpenRecord?: (recordId: string, revisionId: string | null) => void;
  onOpenTask?: (taskId: string) => void;
}

const EVIDENCE_LABELS: Record<KnowledgeSourceView["evidence"], string> = {
  retained: "retained",
  unretained: "not retained",
  unavailable: "evidence unavailable",
};

const RESOLUTION_LABELS: Record<KnowledgeLinkView["resolution"], string | null> = {
  current: null,
  pinned: null,
  unavailable: "unavailable",
  redacted: "redacted",
  unauthorized: "not readable",
};

const SMALL = "rounded border border-gray-700 px-2 py-0.5 text-[11px] text-gray-200 hover:bg-gray-800";

function SourceItem({ source }: { source: KnowledgeSourceView }) {
  // URL sources stay text: nothing here fetches or follows a remote address,
  // and an in-dashboard destination is the only kind that becomes a link.
  const internal = source.href && source.href.startsWith("/") && !source.href.startsWith("//") && source.type !== "url";
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
      <span className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-[10px] uppercase text-gray-400">{source.type}</span>
      {internal ? (
        <a href={source.href!} className="text-indigo-300 hover:underline [overflow-wrap:anywhere]">{source.label}</a>
      ) : (
        <span className="text-gray-200 [overflow-wrap:anywhere]">{source.label}</span>
      )}
      <span data-evidence={source.evidence} className={source.evidence === "retained" ? "text-gray-500" : "text-orange-300"}>
        {EVIDENCE_LABELS[source.evidence]}
      </span>
    </li>
  );
}

function LinkItem({ link, onOpenRecord, onOpenTask }: { link: KnowledgeLinkView } & Pick<KnowledgeProvenanceProps, "onOpenRecord" | "onOpenTask">) {
  const readable = link.endpoint.title !== null && (link.resolution === "current" || link.resolution === "pinned");
  const resolution = RESOLUTION_LABELS[link.resolution];
  return (
    <li data-edge-domain={link.edgeDomain} data-link-id={link.linkId} className="rounded-lg border border-gray-800 bg-gray-900 p-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-gray-400">
          <span className="sr-only">{link.direction === "outgoing" ? "Outgoing link" : "Incoming link"}</span>
          <span aria-hidden="true">{link.direction === "outgoing" ? "→" : "←"}</span>
        </span>
        <span className="font-medium text-gray-200">{linkTypeLabel(link.type)}</span>
        <KindBadge kind={link.endpoint.kind} />
        {/* An endpoint the viewer may not read shows neither title nor snippet. */}
        {link.endpoint.title !== null ? (
          <span className="text-gray-200 [overflow-wrap:anywhere]">{link.endpoint.title}</span>
        ) : (
          <span className="italic text-gray-500">Unavailable</span>
        )}
        <span className="text-gray-500">
          {link.pinnedRevision ? `pinned to r${link.pinnedRevision.sequence}` : "current"}
        </span>
        {resolution && <span data-resolution={link.resolution} className="text-orange-300">{resolution}</span>}
        <span className="rounded bg-gray-800 px-1.5 py-0.5 text-[10px] text-gray-400">informational</span>
      </div>
      {readable && (
        <div className="mt-1.5">
          {link.endpoint.kind === "knowledge" && onOpenRecord && (
            <button type="button" className={SMALL} onClick={() => onOpenRecord(link.endpoint.id, link.pinnedRevision?.revisionId ?? null)}>
              {link.pinnedRevision ? `Open revision ${link.pinnedRevision.sequence}` : "Open"}
            </button>
          )}
          {link.endpoint.kind === "task" && onOpenTask && (
            <button type="button" className={SMALL} onClick={() => onOpenTask(link.endpoint.id)}>Open task</button>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * Where a revision came from and what it is connected to. Sources carry the
 * server's evidence verdict; links are informational projections and are
 * never drawn or described as execution edges.
 */
export default function KnowledgeProvenance({ detail, onOpenRecord, onOpenTask }: KnowledgeProvenanceProps) {
  return (
    <div className="space-y-4">
      <section aria-label="Revision identity" className="space-y-1 text-xs text-gray-400">
        <p>Exact revision: <span className="font-mono [overflow-wrap:anywhere]">{detail.viewed.revisionId}</span></p>
        {detail.viewed.contentSha256 && <p>SHA256: <span className="font-mono [overflow-wrap:anywhere]">{detail.viewed.contentSha256}</span></p>}
        <p>Recorded by {detail.viewed.actorId}. Evidence and verification are shown separately.</p>
      </section>
      <section aria-label="Sources" className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-200">Sources</h3>
        {detail.redacted ? (
          <p className="text-xs text-gray-500">Sources are unavailable for a redacted revision.</p>
        ) : detail.sources.length === 0 ? (
          <p className="text-xs text-gray-500">No sources recorded.</p>
        ) : (
          <ul className="space-y-1">{detail.sources.map((source) => <SourceItem key={source.sourceId} source={source} />)}</ul>
        )}
      </section>
      <section aria-label="Links" className="space-y-2">
        <h3 className="text-sm font-semibold text-gray-200">Links</h3>
        {detail.links.length === 0 ? (
          <p className="text-xs text-gray-500">No links.</p>
        ) : (
          <ul className="space-y-1.5">
            {detail.links.map((link) => <LinkItem key={link.linkId} link={link} onOpenRecord={onOpenRecord} onOpenTask={onOpenTask} />)}
          </ul>
        )}
      </section>
    </div>
  );
}
