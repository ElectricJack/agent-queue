import type { KnowledgeRevisionRef } from "../../pages/knowledge/model";

export interface KnowledgeEditConflictProps {
  observed: KnowledgeRevisionRef;
  current: KnowledgeRevisionRef;
  onReload: () => void;
  onCompare: () => void;
  reloading?: boolean;
  comparing?: boolean;
}

const BUTTON = "rounded border border-amber-400/40 px-2 py-1 text-xs text-amber-100 hover:bg-amber-500/10 disabled:opacity-60";

/**
 * The record moved while the form was open. The editor decides: reload onto
 * the new head (keeping their draft) or compare the two revisions first.
 * Nothing here resubmits; the form stays blocked until an explicit reload.
 */
export default function KnowledgeEditConflict({
  observed, current, onReload, onCompare, reloading = false, comparing = false,
}: KnowledgeEditConflictProps) {
  return (
    <div role="alert" className="space-y-2 rounded-lg border border-amber-400/40 bg-amber-500/10 p-3 text-sm text-amber-100">
      <p>
        This record changed while you were editing: you started from revision {observed.sequence} and
        it is now revision {current.sequence}. Your draft was not saved.
      </p>
      <p className="text-xs text-amber-200/80">
        Reload to rebase your draft onto the current revision, then review it and save again. Compare
        to see exactly what changed first.
      </p>
      <div className="flex flex-wrap gap-1.5">
        <button type="button" className={BUTTON} onClick={onReload} disabled={reloading}>
          {reloading ? "Reloading…" : "Reload current"}
        </button>
        <button type="button" className={BUTTON} onClick={onCompare} aria-pressed={comparing}>
          {comparing ? "Hide comparison" : "Compare"}
        </button>
      </div>
    </div>
  );
}
