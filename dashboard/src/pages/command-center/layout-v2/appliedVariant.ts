import { useSyncExternalStore } from "react";
import type { Variant } from "../../../api/graphLayout";

// The canvas and the toolbar sit on opposite sides of the router outlet, so
// the variant the daemon actually served travels through a module-level store
// rather than a shared parent — the same shape `useJumpToResult` uses for the
// jump target.
//
// It matters because the server promotes a focused request to the `all`
// layout on its own (the entered container is a stub in, or missing from,
// `active`), and the toolbar's `locate` has to search the same layout the
// canvas is drawing or a search inside such a container finds nothing.
let applied: Variant | null = null;
const listeners = new Set<() => void>();

export function publishAppliedVariant(next: Variant | null): void {
  if (applied === next) return;
  applied = next;
  for (const listener of [...listeners]) listener();
}

/** The variant the last tiles response was served from, or null when the
 *  canvas has not answered (or is not mounted). */
export function useAppliedVariant(): Variant | null {
  return useSyncExternalStore(
    (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    () => applied,
  );
}
