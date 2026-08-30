import { useState } from "react";
import { CpuChipIcon, PencilSquareIcon } from "@heroicons/react/24/outline";
import { useIntelligenceClasses, type IntelligenceClassRow } from "../../api/hooks";
import { describeProviderMapping, groupIntelligenceClasses } from "../../components/intelligence-classes/mapping";
import IntelligenceClassEditor from "./IntelligenceClassEditor";

export default function IntelligenceClassesStub() {
  const { data, isLoading, error } = useIntelligenceClasses();
  const [editing, setEditing] = useState<IntelligenceClassRow | null>(null);
  const classes = data?.classes ?? [];

  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-lg font-semibold">Intelligence Classes</h2>
        <p className="text-sm text-gray-500">
          Model and reasoning settings used by task routing. Edits are saved to the vault and apply to future launches; running sessions are unchanged.
        </p>
      </header>

      {isLoading && <p className="text-sm text-gray-500">Loading intelligence classes…</p>}
      {error && <p role="alert" className="text-sm text-red-400">Failed to load: {(error as Error).message}</p>}

      {!isLoading && classes.length === 0 && (
        <div className="flex flex-col items-center justify-center gap-3 rounded border border-dashed border-gray-700 bg-gray-900/40 p-10 text-center">
          <CpuChipIcon className="h-8 w-8 text-gray-600" />
          <p className="text-gray-400">No intelligence classes found. Restart the daemon to seed the defaults.</p>
        </div>
      )}

      {groupIntelligenceClasses(classes).map(({ label, rows }) => (
        <section key={label}>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">{label}</h3>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {rows.map((cls) => (
              <article key={cls.id} className="rounded-lg border border-gray-800 bg-gray-900 p-3">
                <div className="mb-1 flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="font-medium text-gray-100">{cls.name}</p>
                    <code className="font-mono text-xs text-gray-500">{cls.id}</code>
                  </div>
                  <button type="button" aria-label={"Edit " + (cls.name || cls.id)}
                    onClick={(event) => { event.currentTarget.focus({ preventScroll: true }); setEditing(cls); }}
                    className="flex shrink-0 items-center gap-1 rounded px-2 py-1 text-xs text-indigo-300 hover:bg-gray-800">
                    <PencilSquareIcon className="h-4 w-4" />Edit
                  </button>
                </div>
                {cls.description && <p className="mb-2 text-xs text-gray-400">{cls.description}</p>}
                <ul className="space-y-0.5 break-words text-xs text-gray-500">
                  {Object.entries(cls.mapping).sort(([a], [b]) => a.localeCompare(b)).map(([provider, slice]) => (
                    <li key={provider} className="font-mono">{provider}: {describeProviderMapping(slice)}</li>
                  ))}
                </ul>
              </article>
            ))}
          </div>
        </section>
      ))}
      {editing && <IntelligenceClassEditor key={editing.id} row={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}
