import type { AsyncView, KnowledgeDiffView, KnowledgeRevisionRef } from "../../pages/knowledge/model";

export interface KnowledgeDiffProps {
  from: KnowledgeRevisionRef;
  to: KnowledgeRevisionRef;
  view: AsyncView<KnowledgeDiffView>;
  onClose: () => void;
}

/**
 * An exact comparison of two retained revisions, in the same block style the
 * review pane uses. Both sides are named so the reader knows which bytes they
 * are looking at; a redacted side is an explicit error, not an empty diff.
 */
export default function KnowledgeDiff({ from, to, view, onClose }: KnowledgeDiffProps) {
  const label = `Changes from revision ${from.sequence} to revision ${to.sequence}`;
  return (
    <section aria-label={label} className="space-y-2 rounded-lg border border-gray-800 bg-gray-900 p-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-200">{label}</h3>
        <button type="button" onClick={onClose} className="rounded px-2 py-0.5 text-xs text-gray-400 hover:bg-gray-800 hover:text-gray-200">
          Close comparison
        </button>
      </div>
      {view.status === "loading" && <p role="status" className="text-sm text-gray-500">Loading comparison…</p>}
      {view.status === "error" && <p role="alert" className="text-sm text-red-300">{view.message}</p>}
      {view.status === "ready" && (
        view.data.blocks.length === 0 ? (
          <p className="text-sm text-gray-500">The bodies are identical.</p>
        ) : (
          <div className="space-y-1 text-sm">
            {view.data.blocks.map((block, index) => (
              <pre
                key={`${block.op}-${index}`}
                data-diff-op={block.op}
                className={
                  "overflow-x-auto whitespace-pre-wrap rounded p-2 " +
                  (block.op === "added" ? "bg-emerald-950" : block.op === "removed" ? "bg-red-950" : "bg-gray-950")
                }
              >
                <span className="sr-only">{block.op === "added" ? "Added: " : block.op === "removed" ? "Removed: " : "Unchanged: "}</span>
                {block.text}
              </pre>
            ))}
          </div>
        )
      )}
    </section>
  );
}
