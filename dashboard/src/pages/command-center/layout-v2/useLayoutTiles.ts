import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchTiles, type TilesParams } from "../../../api/graphLayout";
import {
  emptyStore,
  evictFar,
  mergeTiles,
  missingCells,
  nodeCount,
  type LayoutStore,
} from "./layoutStore";
import { NODE_BUDGET, cellRect, cellsForRect, type CellKey, type Rect } from "./units";

interface Options {
  onBudgetExceeded?: () => void;
}

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
  const [error, setError] = useState<Error | null>(null);
  // storeRef is the authority during a load: `load` is async and must not read
  // a stale render's `store`.
  const storeRef = useRef(store);
  storeRef.current = store;
  const inflight = useRef<AbortController | null>(null);
  const dirty = useRef(false);
  const wantedRef = useRef<CellKey[]>([]);
  const paramsKey = JSON.stringify(params);
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const onBudget = useRef(opts.onBudgetExceeded);
  onBudget.current = opts.onBudgetExceeded;

  const load = useCallback(async () => {
    if (!projectId) return;
    const wanted = wantedRef.current;
    const missing = missingCells(storeRef.current, wanted);
    if (missing.length === 0) return;
    if (inflight.current) {
      // Re-evaluate once the current request lands: the viewport may have
      // moved again, and firing a second request now would race the merge.
      dirty.current = true;
      return;
    }
    const ac = new AbortController();
    inflight.current = ac;
    try {
      const res = await fetchTiles(projectId, cellRect(missing), paramsRef.current, ac.signal);
      if (ac.signal.aborted) return;
      if ("pending" in res) {
        setPending(true);
        return;
      }
      setPending(false);
      setError(null);
      const merged = evictFar(mergeTiles(storeRef.current, missing, res), wantedRef.current);
      storeRef.current = merged;
      setStore(merged);
      const depth = paramsRef.current.maxDepth ?? null;
      if (nodeCount(merged) > NODE_BUDGET && (depth === null || depth > 0)) onBudget.current?.();
    } catch (e) {
      if (!ac.signal.aborted) setError(e as Error);
    } finally {
      if (inflight.current === ac) inflight.current = null;
      if (dirty.current) {
        dirty.current = false;
        void load();
      }
    }
  }, [projectId]);

  // Params (or project) changed: everything cached describes a different graph.
  useEffect(() => {
    inflight.current?.abort();
    inflight.current = null;
    dirty.current = false;
    const fresh = emptyStore();
    storeRef.current = fresh;
    setStore(fresh);
    void load();
    // paramsKey is the structural identity of `params`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsKey, projectId]);

  useEffect(() => {
    if (!viewportRect) return;
    wantedRef.current = cellsForRect(viewportRect, 1);
    void load();
  }, [viewportRect, load]);

  const refetchVisible = useCallback(() => {
    // Dropping the cells from `loaded` alone would leave nodes that vanished
    // server-side (deleted, or now collapsed into a parent) lingering until
    // eviction, because mergeTiles only adds. Drop their membership too.
    const s = storeRef.current;
    const loaded = new Set(s.loaded);
    const cells = new Map(s.cells);
    const nodes = new Map(s.nodes);
    for (const c of wantedRef.current) {
      loaded.delete(c);
      for (const id of cells.get(c) ?? []) nodes.delete(id);
      cells.delete(c);
    }
    storeRef.current = { ...s, loaded, cells, nodes };
    inflight.current?.abort();
    inflight.current = null;
    void load();
  }, [load]);

  useEffect(() => () => inflight.current?.abort(), []);

  return useMemo(
    () => ({ store, pending, error, refetchVisible }),
    [store, pending, error, refetchVisible],
  );
}
