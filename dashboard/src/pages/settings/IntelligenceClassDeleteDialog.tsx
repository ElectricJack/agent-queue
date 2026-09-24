import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { useDeleteIntelligenceClass, type IntelligenceClassRow } from "../../api/hooks";

type Reference = { kind: string; id: string; name: string; lifecycle?: string | null; status?: string | null };

export default function IntelligenceClassDeleteDialog({ row, onClose }: {
  row: IntelligenceClassRow; onClose: () => void;
}) {
  const id = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const busy = useRef(false);
  const remove = useDeleteIntelligenceClass();
  const [error, setError] = useState<string | null>(null);
  const [references, setReferences] = useState<Reference[]>([]);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.querySelector<HTMLButtonElement>("button")?.focus({ preventScroll: true });
    return () => { if (previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, []);

  const close = () => { if (!busy.current) onClose(); };
  const keyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "Tab") {
      const buttons = Array.from(dialog.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
      if (buttons.length && event.shiftKey && document.activeElement === buttons[0]) {
        event.preventDefault();
        buttons[buttons.length - 1]?.focus();
      } else if (buttons.length && !event.shiftKey && document.activeElement === buttons[buttons.length - 1]) {
        event.preventDefault();
        buttons[0]?.focus();
      }
    }
  };

  const confirm = async () => {
    if (busy.current) return;
    if (!row.revision) { setError("Reload the class list before deleting this class."); return; }
    busy.current = true;
    setError(null);
    setReferences([]);
    try {
      await remove.mutateAsync({ class_id: row.id, expected_revision: row.revision });
      onClose();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      const payload = (caught as { payload?: { references?: Reference[] } })?.payload;
      setReferences(Array.isArray(payload?.references) ? payload.references : []);
    } finally {
      busy.current = false;
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
      <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={id + "-title"} tabIndex={-1}
        onKeyDown={keyDown} className="w-full max-w-lg rounded-lg border border-gray-700 bg-gray-900 p-6 shadow-2xl">
        <h2 id={id + "-title"} className="text-lg font-semibold text-gray-100">Delete {row.name}?</h2>
        <p className="mt-2 text-sm text-gray-400">
          Class <code>{row.id}</code> will be retired in the vault. You can restore it by renaming its .md.retired file.
        </p>
        {error && <div role="alert" className="mt-4 rounded border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">
          <p>{error}</p>
          {references.length > 0 && <ul className="mt-2 list-disc pl-5">
            {references.map((ref) => <li key={ref.kind + ref.id}>
              {ref.kind} {ref.id} ({ref.name}){ref.lifecycle ? ` — ${ref.lifecycle}` : ""}{ref.status ? ` — ${ref.status}` : ""}
            </li>)}
          </ul>}
        </div>}
        <div className="mt-6 flex justify-end gap-2">
          <button type="button" onClick={close} disabled={remove.isPending}
            className="rounded bg-gray-800 px-3 py-2 text-sm text-gray-200 hover:bg-gray-700 disabled:opacity-50">Cancel</button>
          <button type="button" onClick={() => { void confirm(); }} disabled={remove.isPending}
            className="rounded bg-red-700 px-3 py-2 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50">
            {remove.isPending ? "Deleting…" : "Delete class"}
          </button>
        </div>
      </div>
    </div>
  );
}
