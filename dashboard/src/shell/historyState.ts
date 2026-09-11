import { createContext } from "react";

/**
 * What one history entry showed beyond its URL. The route is the entry's own
 * URL; the shell pane is not in the URL, so each entry records it here and
 * Back / Forward put it back.
 */
export interface EntryView {
  /** Position in this tab's dashboard history: 0 for the entry it opened on. */
  depth: number;
  /** The open shell pane, as the pane store takes it; `null` when closed. */
  pane: { view: string; args: unknown } | null;
}

/** Field of `history.state` that holds an entry's {@link EntryView}. */
export const ENTRY_VIEW_FIELD = "aqView";

/**
 * The browser `History` whose entries the router uses, when it is the
 * window's. `main.tsx` provides it for the `BrowserRouter`; under a
 * `MemoryRouter` it is absent, because the router's entries are not the
 * window's and an annotation written there would describe the wrong entry.
 *
 * `history.state` is navigation, not storage: it belongs to the one entry,
 * survives a reload of it, and is gone with the tab.
 */
export const BrowserHistoryContext = createContext<History | null>(null);

/** The router key of the entry `history` is on; an entry with no key is the router's "default". */
function currentKey(history: History): string {
  const key = (history.state as { key?: unknown } | null)?.key;
  return typeof key === "string" ? key : "default";
}

export function parseEntryView(value: unknown): EntryView | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const { depth, pane } = value as { depth?: unknown; pane?: unknown };
  if (typeof depth !== "number" || !Number.isInteger(depth) || depth < 0) return undefined;
  if (pane === null) return { depth, pane: null };
  if (typeof pane !== "object") return undefined;
  const { view, args } = pane as { view?: unknown; args?: unknown };
  if (typeof view !== "string") return undefined;
  return { depth, pane: { view, args: args ?? {} } };
}

/** The view recorded on the browser's current entry, if it is the entry `key` names. */
export function readBrowserEntryView(history: History | null, key: string): EntryView | undefined {
  if (!history || currentKey(history) !== key) return undefined;
  return parseEntryView((history.state as Record<string, unknown> | null)?.[ENTRY_VIEW_FIELD]);
}

/**
 * Records `view` on the browser's current entry when it is the entry `key`
 * names, keeping the router's own fields. Pane args the browser cannot clone
 * are left in memory only.
 */
export function writeBrowserEntryView(history: History | null, key: string, view: EntryView): void {
  if (!history || currentKey(history) !== key) return;
  const state = (history.state as Record<string, unknown> | null) ?? {};
  try {
    history.replaceState({ ...state, [ENTRY_VIEW_FIELD]: view }, "");
  } catch {
    // DataCloneError: the in-memory record still serves this page load.
  }
}
