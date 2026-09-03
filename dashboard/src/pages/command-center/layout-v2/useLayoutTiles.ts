import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchTiles, type TilesParams } from "../../../api/graphLayout";
import {
  dropCarried,
  emptyStore,
  evictFar,
  mergeTiles,
  missingCells,
  nodeCount,
  retainForReflow,
  type LayoutStore,
} from "./layoutStore";
import {
  NODE_BUDGET, boundedBatch, cellRect, cellsForRect, centreCell, type CellKey, type Rect,
} from "./units";

interface Options {
  onBudgetExceeded?: () => void;
}

/**
 * Trailing edge for a viewport-driven fetch. A drag hands the hook a new rect
 * every time it crosses a tile boundary; waiting for the gesture to settle
 * turns a pan across a wide graph into one request instead of a dozen.
 */
export const VIEWPORT_DEBOUNCE_MS = 150;

/**
 * Keeps a `LayoutStore` in sync with the viewport: fetches the cells the
 * viewport covers (padded by one), never refetches a loaded cell, coalesces
 * viewport changes that arrive while a request is in flight, and evicts cells
 * that have drifted far from the viewport.
 */
export function useLayoutTiles(
  projectId: string | undefined,
  params: TilesParams,
  viewportRect: Rect | null,
  opts: Options = {},
) {
  const [store, setStore] = useState<LayoutStore>(emptyStore);
  const [pending, setPending] = useState(false);
  // Distinguishes "nothing here" from "nothing back yet": callers must not
  // claim an empty graph before the first response for these params lands.
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  // storeRef is the authority during a load: `load` is async and must not read
  // a stale render's `store`.
  const storeRef = useRef(store);
  storeRef.current = store;
  const inflight = useRef<AbortController | null>(null);
  const dirty = useRef(false);
  // A failed request must not be retried on its own: the error re-renders the
  // caller, the caller hands back a new rect, and the hook would hammer the
  // daemon. The band the canvas shows offers an explicit Retry instead.
  const failed = useRef(false);
  const wantedRef = useRef<CellKey[]>([]);
  // The very first rect must not wait: nothing is on screen until it lands.
  const panned = useRef(false);
  const centreRef = useRef<CellKey>("0:0");
  const paramsKey = JSON.stringify(params);
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const onBudget = useRef(opts.onBudgetExceeded);
  onBudget.current = opts.onBudgetExceeded;

  const load = useCallback(async () => {
    if (!projectId || failed.current) return;
    const wanted = wantedRef.current;
    const missing = missingCells(storeRef.current, wanted);
    if (missing.length === 0) {
      if (wanted.length > 0) setLoaded(true);
      return;
    }
    if (inflight.current) {
      // Re-evaluate once the current request lands: the viewport may have
      // moved again, and firing a second request now would race the merge.
      dirty.current = true;
      return;
    }
    // The server rejects a rect wider than 64 units, so one pass fetches only
    // the cells nearest the viewport centre that still fit; `dirty` below asks
    // for the rest.
    const batch = boundedBatch(missing, centreRef.current);
    const ac = new AbortController();
    inflight.current = ac;
    try {
      const res = await fetchTiles(projectId, cellRect(batch), paramsRef.current, ac.signal);
      if (ac.signal.aborted) return;
      if ("pending" in res) {
        setPending(true);
        setLoaded(true);
        return;
      }
      setPending(false);
      setLoaded(true);
      setError(null);
      // Under `root` the server ignores the rect and answers with the whole
      // subtree, so evicting by cell would throw away nodes the response just
      // delivered and make every pan re-download them.
      const root = !!paramsRef.current.root;
      const fetched = root
        ? { ...mergeTiles(storeRef.current, batch, res), whole: true }
        : evictFar(mergeTiles(storeRef.current, batch, res), wantedRef.current);
      // Nodes held over from the previous expanded set stop being useful the
      // moment this generation has covered every visible cell: whatever the
      // server did not send back is not on the canvas any more.
      const merged = missingCells(fetched, wantedRef.current).length === 0
        ? dropCarried(fetched)
        : fetched;
      storeRef.current = merged;
      setStore(merged);
      const depth = paramsRef.current.maxDepth ?? null;
      // `max_depth` is ignored under `root` too: stepping it down would only
      // churn the params and refetch the same subtree.
      if (!root && nodeCount(merged) > NODE_BUDGET && (depth === null || depth > 0)) onBudget.current?.();
      // A response carrying a new layout_version makes mergeTiles drop every
      // previously-loaded cell and re-add only this request's, so cells still
      // inside the viewport can come back unloaded. Ask for another pass
      // rather than waiting for the next pan to notice.
      if (missingCells(merged, wantedRef.current).length > 0) dirty.current = true;
    } catch (e) {
      if (!ac.signal.aborted) { failed.current = true; dirty.current = false; setError(e as Error); setLoaded(true); }
    } finally {
      if (inflight.current === ac) inflight.current = null;
      if (dirty.current) {
        dirty.current = false;
        void load();
      }
    }
  }, [projectId]);

  // Params (or project) changed: every cell cached describes a different
  // graph. The DRAWN nodes are kept, though — collapsing a container reflows
  // its siblings, and the canvas animates them from where they were rather
  // than blanking and re-mounting the whole graph. A different PROJECT shares
  // nothing with what is drawn, so that case still starts empty.
  const drawnProject = useRef<string | undefined>(undefined);
  useEffect(() => {
    inflight.current?.abort();
    inflight.current = null;
    dirty.current = false;
    failed.current = false;
    const sameProject = projectId !== undefined && drawnProject.current === projectId;
    drawnProject.current = projectId;
    const fresh = sameProject ? retainForReflow(storeRef.current) : emptyStore();
    storeRef.current = fresh;
    setStore(fresh);
    setLoaded(false);
    void load();
    // paramsKey is the structural identity of `params`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsKey, projectId]);

  useEffect(() => {
    if (!viewportRect) return;
    wantedRef.current = cellsForRect(viewportRect, 1);
    centreRef.current = centreCell(viewportRect);
    // The first rect of a mount is what puts anything on screen at all, so it
    // loads immediately; after that a pan gets the trailing edge, and a
    // gesture that crosses several tiles issues one request at the end rather
    // than one per crossing.
    if (!panned.current) {
      panned.current = true;
      void load();
      return;
    }
    const timer = setTimeout(() => void load(), VIEWPORT_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [viewportRect, load]);

  const refetchVisible = useCallback(() => {
    // Dropping the cells from `loaded` alone would leave nodes that vanished
    // server-side (deleted, or now collapsed into a parent) lingering until
    // eviction, because mergeTiles only adds. Drop their membership too.
    const s = storeRef.current;
    const loadedCells = new Set(s.loaded);
    const cells = new Map(s.cells);
    const nodes = new Map(s.nodes);
    for (const c of wantedRef.current) {
      loadedCells.delete(c);
      for (const id of cells.get(c) ?? []) nodes.delete(id);
      cells.delete(c);
    }
    // `whole` would otherwise make missingCells report nothing to do, so a
    // focused graph could never be reloaded.
    storeRef.current = { ...s, loaded: loadedCells, cells, nodes, whole: false };
    inflight.current?.abort();
    inflight.current = null;
    dirty.current = false;
    failed.current = false;
    setError(null);
    void load();
  }, [load]);

  useEffect(() => () => inflight.current?.abort(), []);

  return useMemo(
    () => ({ store, pending, loaded, error, refetchVisible }),
    [store, pending, loaded, error, refetchVisible],
  );
}
