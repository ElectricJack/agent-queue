import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { XMarkIcon } from "@heroicons/react/24/outline";
import { useEditIntelligenceClass, type IntelligenceClassRow } from "../../api/hooks";
import { describeProviderMapping, providerFields } from "../../components/intelligence-classes/mapping";
import { fieldText, parseMapping, PROVIDERS, providerEffort, validateMappingChanges } from "./intelligenceClassForm";

const inputClass = "mt-1 w-full rounded-md border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none";
const buttonClass = "rounded-md bg-gray-800 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-50";

export default function IntelligenceClassEditor({ row, onClose }: { row: IntelligenceClassRow; onClose: () => void }) {
  const id = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const saving = useRef(false);
  const edit = useEditIntelligenceClass();
  // This mounted editor owns a snapshot. Background list refreshes never replace a draft or its revision.
  const [original] = useState(row);
  const [name, setName] = useState(row.name);
  const [description, setDescription] = useState(row.description);
  const [mappingText, setMappingText] = useState(() => JSON.stringify(row.mapping, null, 2));
  const [advanced, setAdvanced] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);
  const parsed = parseMapping(mappingText);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLInputElement>("input:not([readonly])")?.focus({ preventScroll: true });
    return () => { if (previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, []);

  const close = () => { if (!saving.current) onClose(); };

  const keyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (event.key === "Tab") {
      const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]',
      ) ?? []);
      if (!controls.length) {
        event.preventDefault();
        dialog.current?.focus({ preventScroll: true });
        return;
      }
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
  };

  const setField = (provider: string, key: string, value: string) => {
    if (!parsed.mapping) return;
    const slice = { ...providerFields(parsed.mapping[provider]) };
    if (value === "") delete slice[key];
    else slice[key] = key === "thinking_budget" && /^-?\d+$/.test(value) ? Number(value) : value;
    setMappingText(JSON.stringify({ ...parsed.mapping, [provider]: slice }, null, 2));
    setFatal(null);
  };

  const save = async () => {
    if (saving.current) return;
    const validation = !name.trim() ? "Name is required."
      : parsed.error ?? validateMappingChanges(parsed.mapping!, original.mapping);
    if (validation) { setFatal(validation); return; }
    if (!original.revision) { setFatal("Reload this page to get the latest class revision before saving."); return; }
    setFatal(null);
    saving.current = true;
    try {
      await edit.mutateAsync({
        class_id: original.id, name: name.trim(), description,
        mapping: parsed.mapping!, expected_revision: original.revision,
      });
      onClose();
    } catch (error) {
      setFatal(error instanceof Error ? error.message : String(error));
    } finally {
      saving.current = false;
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
      <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={id + "-title"} aria-describedby={id + "-effect"}
        tabIndex={-1} onKeyDown={keyDown}
        className="flex max-h-[90dvh] w-full max-w-3xl flex-col overflow-hidden rounded-lg border border-gray-700 bg-gray-900 shadow-2xl">
        <header className="flex items-center justify-between gap-4 border-b border-gray-700 px-6 py-4">
          <h2 id={id + "-title"} className="text-lg font-semibold text-gray-100">Edit intelligence class</h2>
          <button type="button" aria-label="Close editor" onClick={close} disabled={edit.isPending}
            className="rounded p-1 text-gray-400 hover:bg-gray-800 disabled:opacity-50">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </header>
        <form onSubmit={(event) => { event.preventDefault(); void save(); }} noValidate className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
            <fieldset disabled={edit.isPending} className="min-w-0 space-y-5 disabled:opacity-70">
              <p id={id + "-effect"} className="text-sm text-gray-400">
                Changes apply to future launches. Running sessions keep their current model and reasoning settings.
              </p>
              <div className="grid gap-4 sm:grid-cols-2">
                <label className="text-xs text-gray-400" htmlFor={id + "-class"}>Class ID
                  <input id={id + "-class"} value={original.id} readOnly className={inputClass + " text-gray-500"} />
                </label>
                <label className="text-xs text-gray-400" htmlFor={id + "-name"}>Name
                  <input id={id + "-name"} value={name} required onChange={(event) => { setName(event.target.value); setFatal(null); }} className={inputClass} />
                </label>
              </div>
              <label className="block text-xs text-gray-400" htmlFor={id + "-description"}>Description
                <textarea id={id + "-description"} rows={2} value={description}
                  onChange={(event) => { setDescription(event.target.value); setFatal(null); }} className={inputClass} />
              </label>

              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-gray-200">Provider settings</h3>
                <button type="button" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)} className={buttonClass}>
                  {advanced ? "Show provider fields" : "Advanced mapping JSON"}
                </button>
              </div>
              {advanced ? (
                <label className="block text-xs text-gray-400" htmlFor={id + "-json"}>Provider mapping JSON
                  <textarea id={id + "-json"} rows={15} spellCheck={false} value={mappingText}
                    onChange={(event) => { setMappingText(event.target.value); setFatal(null); }} className={inputClass + " font-mono text-xs"} />
                </label>
              ) : parsed.mapping ? (
                <div className="grid gap-4 sm:grid-cols-2">
                  {Object.entries(parsed.mapping).sort(([a], [b]) => a.localeCompare(b)).map(([provider, value]) => {
                    const definition = PROVIDERS[provider];
                    const label = definition?.label ?? provider;
                    const slice = providerFields(value);
                    const effort = providerEffort(provider, slice?.model);
                    const selectedEffort = effort ? fieldText(slice?.[effort.key]) : "";
                    return (
                      <fieldset key={provider} className="space-y-3 rounded-md border border-gray-700 p-3">
                        <legend className="px-1 text-sm font-medium text-gray-200">{label}</legend>
                        {slice ? <>
                          <label className="block text-xs text-gray-400">{label} model
                            <input value={fieldText(slice.model)} onChange={(event) => setField(provider, "model", event.target.value)}
                              placeholder="No model override" className={inputClass} />
                          </label>
                          {effort && <label className="block text-xs text-gray-400">{label + " " + effort.label}
                            <select value={selectedEffort} onChange={(event) => setField(provider, effort.key, event.target.value)} className={inputClass}>
                              <option value="">No override</option>
                              {selectedEffort && !effort.values.includes(selectedEffort) && <option value={selectedEffort}>Existing: {selectedEffort}</option>}
                              {effort.values.map((level) => <option key={level} value={level}>{level}</option>)}
                            </select>
                          </label>}
                          {provider === "google" && <label className="block text-xs text-gray-400">Google thinking budget
                            <input inputMode="numeric" value={fieldText(slice.thinking_budget)}
                              onChange={(event) => setField(provider, "thinking_budget", event.target.value)} placeholder="No override" className={inputClass} />
                          </label>}
                        </> : <p className="text-xs text-gray-400">{describeProviderMapping(value)}. Use advanced JSON to change this provider.</p>}
                      </fieldset>
                    );
                  })}
                  {Object.keys(parsed.mapping).length === 0 && <p className="text-sm text-gray-400 sm:col-span-2">No provider mappings. Use advanced JSON to add one.</p>}
                </div>
              ) : <p className="text-sm text-red-300">The mapping JSON needs fixing. Open advanced JSON to edit it.</p>}
              <p className="text-xs text-gray-500">
                Blank fields remove that override. Other options are preserved; use advanced JSON to edit custom options, add providers, or set a null mapping.
                Available reasoning levels depend on the selected model and CLI.
              </p>
            </fieldset>
          </div>
          {fatal && <div role="alert" className="border-t border-red-500/30 bg-red-500/10 px-6 py-3 text-sm text-red-300">
            <p>{fatal}</p>
            {/\b409\b|revision conflict/i.test(fatal) && <p className="mt-1">Your draft is preserved. Cancel and reopen the editor to load the latest saved version.</p>}
          </div>}
          <footer className="flex shrink-0 justify-end gap-2 border-t border-gray-700 bg-gray-900 px-6 py-3">
            <button type="button" onClick={close} disabled={edit.isPending} className={buttonClass}>Cancel</button>
            <button type="submit" disabled={edit.isPending} className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-gray-700">
              {edit.isPending ? "Saving…" : "Save"}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
