import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import type { LocateHit } from "@aq/ts-client";
import { locate, type Variant } from "../../../api/graphLayout";
import type { TaskFilters } from "../taskFilters";

// The toolbar and the canvas sit on opposite sides of the router outlet, so the
// chosen hit travels through a module-level store rather than a shared parent.
let target: LocateHit | null = null;
const listeners = new Set<() => void>();

export function publishJumpTarget(next: LocateHit | null): void {
  target = next;
  for (const listener of [...listeners]) listener();
}

/** The hit the toolbar last asked the canvas to reveal. */
export function useJumpTarget(): LocateHit | null {
  return useSyncExternalStore(
    (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
    () => target,
  );
}

/**
 * Cycles through the server's match positions for the active filters. The
 * daemon answers with coordinates only, so no tile has to be loaded to know
 * where the next match is.
 */
export function useJumpToResult(projectId: string | undefined, variant: Variant, filters: TaskFilters) {
  const [hits, setHits] = useState<LocateHit[]>([]);
  const [index, setIndex] = useState(-1);
  const query = filters.query.trim();
  const active = !!(query || filters.status);

  useEffect(() => {
    setHits([]);
    setIndex(-1);
    publishJumpTarget(null);
    if (!projectId || !active) return;
    let stale = false;
    void locate(projectId, variant, query, filters.status)
      .then((r) => { if (!stale) setHits(r.hits ?? []); })
      .catch(() => { if (!stale) setHits([]); });
    return () => { stale = true; };
  }, [projectId, variant, query, filters.status, active]);

  const next = useCallback(() => {
    if (hits.length === 0) return;
    const at = (index + 1) % hits.length;
    const hit = hits[at];
    setIndex(at);
    // A fresh object even for a repeat hit, so the canvas re-fits when the
    // reader has panned away and asks for the same match again.
    publishJumpTarget(hit ? { ...hit } : null);
  }, [hits, index]);

  return { next, count: hits.length, index };
}
