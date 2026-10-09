import { useId, useRef, useState } from "react";
import { usePolicyApply, usePolicyDiff, usePolicyExport } from "../../api/policy";
import type { ImportRequest, PolicyDiffResponse, Selection } from "../../api/client";
import { useFocusTrap } from "../../hooks/useFocusTrap";

const button = "rounded border border-gray-600 px-3 py-1.5 text-sm hover:bg-gray-700 disabled:opacity-50";
const input = "rounded border border-gray-600 bg-gray-950 px-2 py-1 text-sm";

export default function PolicyProfiles({ projectId }: { projectId: string }) {
  const [mode, setMode] = useState<"import" | "export" | null>(null);
  return <section className="space-y-3 rounded-lg border border-gray-800 p-4" aria-label="Policy profiles">
    <h3 className="font-medium">Policy profiles</h3>
    <p className="text-sm text-gray-400">Copy project policy with a preview and per-item placement.</p>
    <div className="flex gap-2">
      <button className={button} onClick={() => setMode("export")}>Export policy</button>
      <button className={button} onClick={() => setMode("import")}>Import policy</button>
    </div>
    {mode && <PolicyDialog projectId={projectId} mode={mode} onClose={() => setMode(null)} />}
  </section>;
}

function archiveFromFile(file: File): Promise<string> {
  if (file.size > 32 * 1024 * 1024) return Promise.reject(new Error("Policy archive exceeds 32 MiB"));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1]!);
    reader.onerror = () => reject(new Error("Could not read policy archive"));
    reader.readAsDataURL(file);
  });
}

export function PolicyDialog({ projectId, mode = "import", onClose }: {
  projectId: string; mode?: "import" | "export"; onClose: () => void;
}) {
  const exportPolicy = usePolicyExport();
  const diffPolicy = usePolicyDiff();
  const applyPolicy = usePolicyApply();
  const [request, setRequest] = useState<ImportRequest | null>(null);
  const [preview, setPreview] = useState<PolicyDiffResponse | null>(null);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<string | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const busy = exportPolicy.isPending || diffPolicy.isPending || applyPolicy.isPending;
  useFocusTrap(dialog, true, { onEscape: () => { if (!busy) onClose(); } });

  async function run(action: () => Promise<void>) {
    setError(null);
    try { await action(); } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  }

  async function refresh(next: ImportRequest) {
    const data = await diffPolicy.mutateAsync(next);
    setRequest({ ...next, values: { ...data.values, ...next.values } });
    setPreview(data);
    setDirty(false);
  }

  function select(id: string, patch: Partial<Selection>) {
    if (!request) return;
    setRequest({ ...request, selections: {
      ...request.selections, [id]: { ...request.selections?.[id], ...patch },
    } });
    setDirty(true);
  }

  async function apply() {
    if (!request || !preview || dirty) return;
    const selections = Object.fromEntries(preview.items.map(row => [row.id, {
      ...request.selections?.[row.id], scope: row.selected ? row.scope : "skip",
      expected_checksum: row.current_checksum,
    }]));
    const result = await applyPolicy.mutateAsync({ ...request, selections });
    setReceipt(`Imported ${result.applied.length} items. ${result.reviews?.length ?? 0} pending review; ${result.pending_configuration?.length ?? 0} pending configuration. No playbooks activated.`);
  }

  function download() {
    const data = exportPolicy.data!;
    const bytes = Uint8Array.from(atob(data.archive), char => char.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: "application/zip" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${projectId}.aqpolicy`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
    <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}
      className="mx-3 max-h-[90vh] w-full max-w-3xl space-y-4 overflow-y-auto rounded-lg border border-gray-700 bg-gray-900 p-5">
      <div className="flex items-center justify-between">
        <h2 id={titleId} className="text-lg font-semibold">{mode === "import" ? "Import" : "Export"} policy profile</h2>
        <button className={button} onClick={onClose} disabled={busy}>Close</button>
      </div>
      {error && <p role="alert" className="text-red-300">{error}</p>}
      {receipt ? <p role="status">{receipt}</p> : mode === "export" ? <>
        <button className={button} disabled={busy} onClick={() => void run(async () => { await exportPolicy.mutateAsync({ project_id: projectId }); })}>Preview exact files</button>
        {exportPolicy.data && <>
          {exportPolicy.data.files.map(file => <details key={file.path} className="rounded border border-gray-700 p-2">
            <summary>{file.path}</summary><pre className="overflow-auto whitespace-pre-wrap text-xs">{file.content}</pre>
          </details>)}
          <button className={button} onClick={download}>Download .aqpolicy</button>
        </>}
      </> : <>
        <label className="block text-sm">Policy archive
          <input type="file" accept=".aqpolicy,.zip" className="ml-3" disabled={busy} onChange={event => {
            const file = event.target.files?.[0];
            if (file) void run(async () => {
              setReceipt(null); setPreview(null); setRequest(null);
              await refresh({ project_id: projectId, archive: await archiveFromFile(file) });
            });
          }} />
        </label>
        {preview && request && <>
          <h3 className="font-medium">{preview.name}</h3>
          {preview.placeholders.map(placeholder => <label key={placeholder.name} className="block text-sm">
            {placeholder.description}
            <input aria-label={placeholder.description} disabled={busy} className={`${input} ml-3`} value={request.values?.[placeholder.name] ?? ""} onChange={event => {
              setRequest({ ...request, values: { ...request.values, [placeholder.name]: event.target.value } }); setDirty(true);
            }} />
          </label>)}
          <p className="text-sm text-gray-400">System items need an explicit placement. Select overwrites after reviewing their diff. Project agent settings, promotion flows and routing bindings remain inactive drafts; configure profiles globally and flows through project configuration when ready; playbooks require review and activation.</p>
          {[...new Set(preview.items.map(row => row.type))].sort().map(type => <fieldset key={type} className="space-y-3 rounded border border-gray-700 p-3">
            <legend className="px-1 font-medium">{type.replace(/_/g, " ")}</legend>
            {preview.items.filter(row => row.type === type).map(row => <div key={row.id} className="space-y-2">
              <div className="flex flex-wrap items-center gap-3">
                <span>{row.name} · {row.status} · {row.original_scope} · {row.state}</span>
                <label className="text-sm">Placement
                  <select aria-label={`Placement for ${row.name}`} disabled={busy} className={`${input} ml-2`} value={request.selections?.[row.id]?.scope ?? "project"}
                    onChange={event => select(row.id, { scope: event.target.value as Selection["scope"], overwrite: false })}>
                    <option value="project">Project</option><option value="global">Global</option><option value="skip">Skip</option>
                  </select>
                </label>
                {row.requires_scope_choice && <label className="text-sm">
                  <input type="checkbox" aria-label={`Confirm placement for ${row.name}`} disabled={busy} checked={request.selections?.[row.id]?.scope !== undefined && request.selections[row.id]?.scope !== "skip"}
                    onChange={event => select(row.id, { scope: event.target.checked ? "project" : "skip" })} /> Confirm placement
                </label>}
                {row.status === "will overwrite" && <label className="text-sm">
                  <input type="checkbox" aria-label={`Overwrite ${row.name}`} checked={request.selections?.[row.id]?.overwrite ?? false}
                    disabled={busy || row.requires_scope_choice && !request.selections?.[row.id]?.scope}
                    onChange={event => select(row.id, { overwrite: event.target.checked })} /> Overwrite
                </label>}
              </div>
              {row.diff && <details><summary>Diff for {row.name}</summary><pre className="overflow-auto whitespace-pre-wrap text-xs">{row.diff}</pre></details>}
              <p className="text-xs text-gray-400">{row.selected ? "Selected" : "Skipped"}: {row.destinations?.join(", ")}</p>
            </div>)}
          </fieldset>)}
          <div className="flex gap-3">
            <button className={button} disabled={busy} onClick={() => void run(() => refresh(request))}>Update preview</button>
            <button className={button} disabled={busy || dirty || preview.placeholders.some(p => !request.values?.[p.name]) || !preview.items.some(row => row.selected)} onClick={() => void run(apply)}>Import selected items</button>
          </div>
          {dirty && <p role="status" className="text-sm text-amber-300">Update the preview to review your changes before importing.</p>}
        </>}
      </>}
    </div>
  </div>;
}
