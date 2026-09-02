import { useMemo, useState } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchTiles = vi.hoisted(() => vi.fn());
vi.mock("../../../../api/graphLayout", () => ({ fetchTiles }));
import { useLayoutTiles } from "../useLayoutTiles";
import { missingCells } from "../layoutStore";
import { cellsForRect, worldRectFromViewport } from "../units";

const node = (id: string, x: number, y: number) => ({
  id, title: id, status: "READY", priority: 100, is_blocked: false, x, y, w: 1, h: 1, depth: 0,
  container_id: null, kind: "card", context_only: false,
  agg_children: 0, agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0,
});
const ok = (nodes: unknown[], version = 1) => ({ nodes, edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: version });
const params = { variant: "active" as const, expanded: [] as string[] };

beforeEach(() => fetchTiles.mockReset());
afterEach(() => vi.useRealTimers());

describe("useLayoutTiles", () => {
  it("fetches the padded cells for the viewport once and merges", async () => {
    fetchTiles.mockResolvedValue(ok([node("a", 1, 1)]));
    const { result } = renderHook(() => useLayoutTiles("p1", params, { x0: 0, y0: 0, x1: 4, y1: 4 }));
    await waitFor(() => expect(result.current.store.nodes.has("a")).toBe(true));
    expect(fetchTiles).toHaveBeenCalledTimes(1);
    const [, rect] = fetchTiles.mock.calls[0]!;
    expect(rect).toEqual({ x0: -8, y0: -8, x1: 16, y1: 16 });
  });
  it("does not refetch loaded cells and coalesces while in flight", async () => {
    let resolve!: (v: unknown) => void;
    fetchTiles.mockImplementationOnce(() => new Promise((r) => { resolve = r; }));
    fetchTiles.mockResolvedValue(ok([node("b", 20, 1)]));
    const { result, rerender } = renderHook(({ rect }) => useLayoutTiles("p1", params, rect),
      { initialProps: { rect: { x0: 0, y0: 0, x1: 4, y1: 4 } } });
    rerender({ rect: { x0: 16, y0: 0, x1: 20, y1: 4 } });
    expect(fetchTiles).toHaveBeenCalledTimes(1);
    await act(async () => { resolve(ok([node("a", 1, 1)])); });
    await waitFor(() => expect(fetchTiles).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.store.nodes.has("b")).toBe(true));
    rerender({ rect: { x0: 16, y0: 0, x1: 20, y1: 4 } });
    expect(fetchTiles).toHaveBeenCalledTimes(2);
  });
  it("resets the store when params change", async () => {
    fetchTiles.mockResolvedValue(ok([node("a", 1, 1)]));
    const { result, rerender } = renderHook(({ p }) => useLayoutTiles("p1", p, { x0: 0, y0: 0, x1: 4, y1: 4 }),
      { initialProps: { p: params } });
    await waitFor(() => expect(result.current.store.nodes.has("a")).toBe(true));
    fetchTiles.mockResolvedValue(ok([node("c", 1, 1)]));
    rerender({ p: { ...params, expanded: ["e"] } });
    await waitFor(() => expect(result.current.store.nodes.has("c")).toBe(true));
    expect(result.current.store.nodes.has("a")).toBe(false);
  });
  it("refetchVisible reloads the visible cells", async () => {
    fetchTiles.mockResolvedValue(ok([node("a", 1, 1)]));
    const { result } = renderHook(() => useLayoutTiles("p1", params, { x0: 0, y0: 0, x1: 4, y1: 4 }));
    await waitFor(() => expect(fetchTiles).toHaveBeenCalledTimes(1));
    fetchTiles.mockResolvedValue(ok([node("a", 1, 1), node("z", 2, 2)]));
    act(() => result.current.refetchVisible());
    await waitFor(() => expect(result.current.store.nodes.has("z")).toBe(true));
  });
  it("re-fetches cells dropped by a layout_version bump", async () => {
    fetchTiles.mockResolvedValueOnce(ok([node("a", 1, 1)], 1));
    const { result, rerender } = renderHook(({ rect }) => useLayoutTiles("p1", params, rect),
      { initialProps: { rect: { x0: 0, y0: 0, x1: 4, y1: 4 } } });
    await waitFor(() => expect(result.current.store.nodes.has("a")).toBe(true));
    // Response 2 covers only the newly-revealed cells but carries a new
    // layout_version, so mergeTiles throws away everything already loaded.
    fetchTiles.mockResolvedValueOnce(ok([node("b", 20, 1)], 2));
    fetchTiles.mockResolvedValueOnce(ok([node("a", 1, 1)], 2));
    rerender({ rect: { x0: 0, y0: 0, x1: 20, y1: 4 } });
    await waitFor(() => expect(fetchTiles).toHaveBeenCalledTimes(3));
    expect(fetchTiles.mock.calls[1]![1]).toEqual({ x0: 16, y0: -8, x1: 32, y1: 16 });
    expect(fetchTiles.mock.calls[2]![1]).toEqual({ x0: -8, y0: -8, x1: 16, y1: 16 });
    await waitFor(() => {
      expect(result.current.store.nodes.has("a")).toBe(true);
      expect(result.current.store.nodes.has("b")).toBe(true);
    });
    expect(result.current.store.version).toBe(2);
  });
  it("reports pending on 202", async () => {
    fetchTiles.mockResolvedValue({ pending: true });
    const { result } = renderHook(() => useLayoutTiles("p1", params, { x0: 0, y0: 0, x1: 4, y1: 4 }));
    await waitFor(() => expect(result.current.pending).toBe(true));
  });

  it("never asks for a rect wider than the server's 64-unit cap, and still loads every wanted cell", async () => {
    fetchTiles.mockResolvedValue(ok([]));
    // 2560x1440 at the minimum zoom: the padded viewport is ~14x12 cells, far
    // past the 8-cell-per-axis cap the daemon enforces.
    const rect = worldRectFromViewport({ x: 0, y: 0, zoom: 0.15 }, 2560, 1440);
    const { result } = renderHook(() => useLayoutTiles("p1", params, rect));
    await waitFor(() => expect(result.current.loaded).toBe(true));
    const wanted = cellsForRect(rect, 1);
    await waitFor(() => expect(missingCells(result.current.store, wanted)).toEqual([]));
    expect(fetchTiles.mock.calls.length).toBeGreaterThan(1);
    for (const [, r] of fetchTiles.mock.calls as [string, { x0: number; y0: number; x1: number; y1: number }][]) {
      expect(r.x1 - r.x0).toBeLessThanOrEqual(64);
      expect(r.y1 - r.y0).toBeLessThanOrEqual(64);
    }
  });

  it("treats a root response as the whole graph: one fetch, no budget step-down", async () => {
    const many = Array.from({ length: 401 }, (_, i) => node(`n${i}`, i % 20, Math.floor(i / 20)));
    fetchTiles.mockResolvedValue(ok(many));
    const onBudgetExceeded = vi.fn();
    const rootParams = { ...params, root: "r1", maxDepth: 2 };
    const { result, rerender } = renderHook(
      ({ rect }) => useLayoutTiles("p1", rootParams, rect, { onBudgetExceeded }),
      { initialProps: { rect: { x0: 0, y0: 0, x1: 4, y1: 4 } } },
    );
    await waitFor(() => expect(result.current.store.nodes.size).toBe(401));
    rerender({ rect: { x0: 40, y0: 40, x1: 60, y1: 60 } });
    rerender({ rect: { x0: 100, y0: 100, x1: 120, y1: 120 } });
    await waitFor(() => expect(result.current.store.whole).toBe(true));
    expect(fetchTiles).toHaveBeenCalledTimes(1);
    expect(onBudgetExceeded).not.toHaveBeenCalled();
  });

  it("asks the caller to step max_depth down when a response blows the node budget", async () => {
    // All 401 inside cell 0:0, so eviction cannot shrink the merged store.
    const many = Array.from({ length: 401 }, (_, i) => node(`n${i}`, (i % 20) * 0.35, Math.floor(i / 20) * 0.3));
    fetchTiles.mockResolvedValueOnce(ok(many));
    fetchTiles.mockResolvedValue(ok([node("a", 1, 1)]));
    function Harness() {
      const [maxDepth, setMaxDepth] = useState<number>(2);
      const p = useMemo(() => ({ ...params, maxDepth }), [maxDepth]);
      const tiles = useLayoutTiles("p1", p, { x0: 0, y0: 0, x1: 4, y1: 4 },
        { onBudgetExceeded: () => setMaxDepth((d) => Math.max(0, d - 1)) });
      return { ...tiles, maxDepth };
    }
    const { result } = renderHook(() => Harness());
    await waitFor(() => expect(result.current.maxDepth).toBe(1));
    await waitFor(() => expect(fetchTiles).toHaveBeenCalledTimes(2));
    const first = fetchTiles.mock.calls[0]![2] as { maxDepth: number };
    const second = fetchTiles.mock.calls[1]![2] as { maxDepth: number };
    expect(first.maxDepth).toBe(2);
    expect(second.maxDepth).toBe(1);
  });

});
