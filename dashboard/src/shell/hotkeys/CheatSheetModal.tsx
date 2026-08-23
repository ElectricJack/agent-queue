import { useCheatSheet, useShortcut } from "./useShortcuts";

interface Props {
  open: boolean;
  onClose: () => void;
}

/** `?` toggles this — implemented at the shell root. Escape closes it. */
export default function CheatSheetModal({ open, onClose }: Props) {
  useShortcut("Escape", {
    label: "close cheat sheet",
    onFire: onClose,
    when: () => open,
  });
  const items = useCheatSheet();
  if (!open) return null;
  const bySection = new Map<string, typeof items>();
  for (const it of items) {
    const s = it.opts.section ?? "General";
    if (!bySection.has(s)) bySection.set(s, []);
    bySection.get(s)!.push(it);
  }
  return (
    <div
      role="dialog"
      aria-label="Keyboard shortcuts"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl rounded-lg border border-gray-700 bg-gray-900 p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="mb-4 text-lg font-semibold">Keyboard shortcuts</h2>
        <div className="max-h-[60vh] space-y-4 overflow-y-auto">
          {[...bySection.entries()].map(([section, entries]) => (
            <div key={section}>
              <p className="mb-1 text-xs uppercase text-gray-500">{section}</p>
              <ul className="space-y-0.5">
                {entries.map((it) => (
                  <li
                    key={it.id}
                    className="flex items-center justify-between border-b border-gray-800 py-1 text-sm"
                  >
                    <span className="text-gray-300">{it.opts.label}</span>
                    <kbd className="rounded bg-gray-800 px-2 py-0.5 font-mono text-xs">
                      {it.key}
                    </kbd>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
