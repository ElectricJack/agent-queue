export type RevisionSummary = {
  revision: number;
  changes_note?: string | null;
  submitted_at?: unknown;
  responder_class?: string | null;
  responder_profile?: string | null;
  responder_profile_source?: string | null;
};

export function RevisionHeader({
  state,
  revisions,
  revision,
  onRevisionChange,
  showDiff,
  onShowDiffChange,
}: {
  state: string;
  revisions: RevisionSummary[];
  revision: number;
  onRevisionChange: (revision: number) => void;
  showDiff: boolean;
  onShowDiffChange: (show: boolean) => void;
}) {
  const current = revisions.find((item) => item.revision === revision);
  return (
    <header className="flex flex-wrap items-center gap-3 border-b border-gray-800 px-4 py-3 text-sm">
      <span className="rounded-full bg-indigo-950/60 px-2 py-0.5 text-xs text-indigo-200">{state.replace(/_/g, " ")}</span>
      <label className="text-xs text-gray-400">
        Revision
        <select
          aria-label="Review revision"
          value={revision}
          onChange={(event) => onRevisionChange(Number(event.target.value))}
          className="ml-1 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-100"
        >
          {revisions.map((item) => <option key={item.revision} value={item.revision}>rev {item.revision}</option>)}
        </select>
      </label>
      {revision > 1 && (
        <label className="flex items-center gap-1.5 text-xs text-gray-400">
          <input type="checkbox" checked={showDiff} onChange={(event) => onShowDiffChange(event.target.checked)} />
          Changes since previous
        </label>
      )}
      {current?.changes_note && <p className="text-xs text-gray-400">{current.changes_note}</p>}
    </header>
  );
}
