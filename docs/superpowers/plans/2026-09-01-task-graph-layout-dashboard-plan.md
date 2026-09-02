# Task Graph Layout Dashboard (Stage 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the client-side grid layout in the Command Center Graph tab with a viewport-paged canvas driven by the Stage 2 layout endpoints, behind a feature flag, with containers, focus mode, breadcrumbs, level of detail, server-side filtering, live refetch, a Tidy button, and a paginated mobile list.

**Architecture:** A pure entity store (`layoutStore.ts`) holds nodes, edges, stubs, and cell membership per project and variant. A hook (`useLayoutTiles.ts`) maps the React Flow viewport to world cells, fetches missing cells with one in-flight request per project, merges, evicts, and resets on version or parameter change. `LayoutCanvas.tsx` renders the store through React Flow with `onlyRenderVisibleElements`, reusing `TaskNode` for cards and adding `ContainerNode`. `Graph.tsx` picks `LayoutCanvas` when the flag is on and keeps `GraphCanvas` as fallback for one release.

**Tech Stack:** React 18, `@xyflow/react` 12, TanStack Query 5, react-router, Vitest + Testing Library, generated `@aq/ts-client`.

**Spec:** `docs/superpowers/specs/2026-09-01-task-graph-spatial-layout-design.md` (sections 6, 7, 9, 10)

**Depends on:** Stage 2 merged and the TS client regenerated. Generated operation names assumed below (verify against `packages/aq-ts-client/src/sdk.gen.ts` first and substitute if they differ): `getExtentApiProjectsProjectIdGraphExtentGet`, `postTilesApiProjectsProjectIdGraphTilesPost`, `postListApiProjectsProjectIdGraphListPost`, `getNodeApiProjectsProjectIdGraphNodeTaskIdGet`, `getLocateApiProjectsProjectIdGraphLocateGet`, `postTidyApiProjectsProjectIdGraphTidyPost`; types `TilesResponse`, `LayoutNode`, `LayoutEdge`, `LayoutStub`, `LayoutWorker`, `ExtentResponse`, `ListResponse`, `NodeResponse`, `LocateResponse`.

## Global Constraints

- One world unit = `NODE_WIDTH` (240 px) wide and `NODE_HEIGHT` (156 px) tall. Every server `x, w` multiplies by 240 and `y, h` by 156.
- Cell size 8 units. Fetch the viewport's cells plus one cell of padding. Evict cells farther than 3 cells from the viewport. At most one `tiles` request in flight per project.
- Level of detail: zoom < 0.35 → `max_depth 0`; zoom < 0.6 → `max_depth 1`; otherwise none. Client node budget 400: exceeding it steps `max_depth` down by one and refetches.
- Any change of `expanded`, `max_depth`, filters, variant, or focus clears that project's store. A `layout_version` change clears it too. Never merge across versions.
- Focus is `?focus=<id>` in the URL; while focused, variant is `all` and Show completed is disabled.
- No node transitions; nothing animates positions.
- Live events refetch the project's visible cells after the existing 500 ms coalescing window.
- Feature flag: `dashboard.graph_layout.enabled` read from the system status payload as `graph_layout_enabled`. Off → existing `GraphCanvas`.
- Tests run with `npm test` in `dashboard/`; type-check with `npm run typecheck`; lint with `npm run lint`.

---

## File structure

| File | Responsibility |
|---|---|
| `dashboard/src/api/graphLayout.ts` | Thin typed wrappers around the generated client; `useLayoutExtent`, `useLayoutNode`, `useTidyLayout` |
| `dashboard/src/pages/command-center/layout-v2/units.ts` | Unit ↔ pixel scale, cell math, LOD thresholds |
| `dashboard/src/pages/command-center/layout-v2/layoutStore.ts` | Pure store: merge, evict, reset, selectors |
| `dashboard/src/pages/command-center/layout-v2/useLayoutTiles.ts` | Viewport → cells → fetch → store |
| `dashboard/src/pages/command-center/layout-v2/flowNodes.ts` | Store → React Flow nodes and edges |
| `dashboard/src/pages/command-center/layout-v2/ContainerNode.tsx` | Group node with header, aggregates, Focus and collapse actions |
| `dashboard/src/pages/command-center/layout-v2/Breadcrumbs.tsx` | Focus path strip |
| `dashboard/src/pages/command-center/layout-v2/LayoutCanvas.tsx` | The canvas |
| `dashboard/src/pages/command-center/layout-v2/MobileLayoutList.tsx` | Paginated list via `list` |
| `dashboard/src/pages/command-center/TaskWorkspace.tsx` | `focus` param, `setFocus` |
| `dashboard/src/pages/command-center/TaskToolbar.tsx` | Tidy button, jump-to-result |
| `dashboard/src/pages/command-center/Graph.tsx` | Flag switch |
| `dashboard/src/pages/command-center/useGraphLive.ts` | Layout refetch on events |
| `src/commands/system_commands.py` | `graph_layout_enabled` in system status |
| `dashboard/src/pages/command-center/layout-v2/__tests__/…` | Tests |

---

### Task 1: Feature flag in system status

**Files:**
- Modify: `src/commands/system_commands.py` (the `_cmd_system_status` result dict; find it with `grep -n "_cmd_system_status" src/commands/system_commands.py`)
- Modify: `src/api/models/system.py` if the status response model enumerates fields (`grep -n "class SystemStatus" src/api/models/system.py`)
- Test: `tests/test_system_commands.py` (append; create the file if absent using `command_handler_factory`)

- [ ] **Step 1: Write the failing test**

```python
async def test_system_status_reports_graph_layout_flag(command_handler_factory):
    h = await command_handler_factory()
    r = await h.execute("system_status", {})
    assert r["success"] and r["graph_layout_enabled"] is False
    h.config.graph_layout.enabled = True
    assert (await h.execute("system_status", {}))["graph_layout_enabled"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_system_commands.py -v -k graph_layout_flag`
Expected: FAIL with `KeyError`.

- [ ] **Step 3: Implement**

In the status result dict add `"graph_layout_enabled": bool(getattr(self.config, "graph_layout", None) and self.config.graph_layout.enabled)`. If the response model is explicit, add `graph_layout_enabled: bool = False`. Regenerate the TS client (`./scripts/regenerate-ts-client.sh`) so `useSystemStatus().data.graph_layout_enabled` is typed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_system_commands.py -v -k graph_layout_flag`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/commands/system_commands.py src/api/models/system.py tests/test_system_commands.py packages/aq-ts-client openapi.json
git commit -m "feat(layout-ui): expose graph_layout_enabled in system status"
```

---

### Task 2: Units, cells, and LOD

**Files:**
- Create: `dashboard/src/pages/command-center/layout-v2/units.ts`
- Test: `dashboard/src/pages/command-center/layout-v2/__tests__/units.test.ts`

**Interfaces:**
- `UNIT_W = 240`, `UNIT_H = 156`, `CELL = 8`.
- `toPx(x: number, y: number): {x: number; y: number}` and `sizePx(w, h)`.
- `worldRectFromViewport(vp: {x, y, zoom}, widthPx, heightPx): Rect` where `Rect = {x0, y0, x1, y1}` in units. React Flow's viewport transform is `screen = world * zoom + translate`, so `world = (screen - translate) / zoom`.
- `cellsForRect(rect: Rect, pad = 1): CellKey[]` with `CellKey = \`${cx}:${cy}\``.
- `cellRect(cells: CellKey[]): Rect` (bounding box of the cells).
- `cellDistance(a: CellKey, b: CellKey): number` (Chebyshev).
- `maxDepthForZoom(zoom: number): number | null` per the thresholds.

- [ ] **Step 1: Write the failing tests**

```ts
import { describe, expect, it } from "vitest";
import { cellDistance, cellRect, cellsForRect, maxDepthForZoom, toPx, worldRectFromViewport } from "../units";

describe("units", () => {
  it("scales units to pixels", () => {
    expect(toPx(2, 3)).toEqual({ x: 480, y: 468 });
  });
  it("maps a viewport to a world rect", () => {
    // zoom 1, translated so world (0,0) is at screen (0,0): 1920x1080 → 8x6.92 units
    const r = worldRectFromViewport({ x: 0, y: 0, zoom: 1 }, 1920, 1080);
    expect(r.x0).toBe(0); expect(r.y0).toBe(0);
    expect(r.x1).toBeCloseTo(8); expect(r.y1).toBeCloseTo(1080 / 156);
    // panned right by 240px at zoom 0.5: world x starts at (0+240)/0.5/240 = 2
    const p = worldRectFromViewport({ x: -240, y: 0, zoom: 0.5 }, 1920, 1080);
    expect(p.x0).toBeCloseTo(2); expect(p.x1).toBeCloseTo(2 + 16);
  });
  it("lists cells with padding", () => {
    expect(cellsForRect({ x0: 0, y0: 0, x1: 1, y1: 1 }, 0)).toEqual(["0:0"]);
    expect(cellsForRect({ x0: 0, y0: 0, x1: 1, y1: 1 }, 1).sort()).toEqual(
      ["-1:-1", "-1:0", "-1:1", "0:-1", "0:0", "0:1", "1:-1", "1:0", "1:1"].sort());
    expect(cellsForRect({ x0: 7.9, y0: 0, x1: 8.1, y1: 1 }, 0)).toEqual(["0:0", "1:0"]);
  });
  it("cell rect and distance", () => {
    expect(cellRect(["0:0", "1:0"])).toEqual({ x0: 0, y0: 0, x1: 16, y1: 8 });
    expect(cellDistance("0:0", "3:-2")).toBe(3);
  });
  it("lod thresholds", () => {
    expect(maxDepthForZoom(0.2)).toBe(0);
    expect(maxDepthForZoom(0.5)).toBe(1);
    expect(maxDepthForZoom(1)).toBeNull();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```ts
import { NODE_HEIGHT, NODE_WIDTH } from "../types";

export const UNIT_W = NODE_WIDTH;
export const UNIT_H = NODE_HEIGHT;
export const CELL = 8;
export const NODE_BUDGET = 400;
export type Rect = { x0: number; y0: number; x1: number; y1: number };
export type CellKey = `${number}:${number}`;

export const toPx = (x: number, y: number) => ({ x: x * UNIT_W, y: y * UNIT_H });
export const sizePx = (w: number, h: number) => ({ width: w * UNIT_W, height: h * UNIT_H });

export function worldRectFromViewport(vp: { x: number; y: number; zoom: number }, widthPx: number, heightPx: number): Rect {
  const x0 = (0 - vp.x) / vp.zoom / UNIT_W;
  const y0 = (0 - vp.y) / vp.zoom / UNIT_H;
  const x1 = (widthPx - vp.x) / vp.zoom / UNIT_W;
  const y1 = (heightPx - vp.y) / vp.zoom / UNIT_H;
  return { x0, y0, x1, y1 };
}

export function cellsForRect(rect: Rect, pad = 1): CellKey[] {
  const cx0 = Math.floor(rect.x0 / CELL) - pad, cy0 = Math.floor(rect.y0 / CELL) - pad;
  const cx1 = Math.ceil(rect.x1 / CELL) - 1 + pad, cy1 = Math.ceil(rect.y1 / CELL) - 1 + pad;
  const out: CellKey[] = [];
  for (let cx = cx0; cx <= cx1; cx++) for (let cy = cy0; cy <= cy1; cy++) out.push(`${cx}:${cy}`);
  return out;
}

export const parseCell = (key: CellKey) => key.split(":").map(Number) as [number, number];

export function cellRect(cells: CellKey[]): Rect {
  const xs = cells.map((c) => parseCell(c)[0]), ys = cells.map((c) => parseCell(c)[1]);
  return { x0: Math.min(...xs) * CELL, y0: Math.min(...ys) * CELL,
           x1: (Math.max(...xs) + 1) * CELL, y1: (Math.max(...ys) + 1) * CELL };
}

export function cellDistance(a: CellKey, b: CellKey): number {
  const [ax, ay] = parseCell(a), [bx, by] = parseCell(b);
  return Math.max(Math.abs(ax - bx), Math.abs(ay - by));
}

export function maxDepthForZoom(zoom: number): number | null {
  if (zoom < 0.35) return 0;
  if (zoom < 0.6) return 1;
  return null;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/command-center/layout-v2
git commit -m "feat(layout-ui): world units, cell math, and LOD thresholds"
```

---

### Task 3: Entity store

**Files:**
- Create: `dashboard/src/pages/command-center/layout-v2/layoutStore.ts`
- Test: `dashboard/src/pages/command-center/layout-v2/__tests__/layoutStore.test.ts`

**Interfaces:**
```ts
export interface LayoutStore {
  version: number | null;
  nodes: Map<string, LayoutNode>;
  edges: Map<string, LayoutEdge>;           // key `${from}|${to}|${dep_type}`
  stubs: Map<string, LayoutStub>;
  workers: LayoutWorker[];
  gates: GraphGate[];
  cells: Map<CellKey, Set<string>>;          // node ids the server returned for that cell
  loaded: Set<CellKey>;                      // cells fetched (even if empty)
}
export const emptyStore = (): LayoutStore;
export function mergeTiles(store, cells: CellKey[], res: TilesResponse): LayoutStore  // returns a NEW store or the same store with version reset if res.layout_version differs
export function evictFar(store, keep: CellKey[], maxDistance = 3): LayoutStore
export function missingCells(store, wanted: CellKey[]): CellKey[]
export function nodeCount(store): number
```
Node membership: a node returned for a request covering cells `C` belongs to every cell in `C` whose rect intersects the node's box. Eviction removes cells outside range and then any node, stub, or edge with no remaining referencing cell (edges reference their endpoint nodes; stubs reference the edge that needs them).

- [ ] **Step 1: Write the failing tests**

```ts
import { describe, expect, it } from "vitest";
import type { TilesResponse } from "@aq/ts-client";
import { emptyStore, evictFar, mergeTiles, missingCells, nodeCount } from "../layoutStore";

const node = (id: string, x: number, y: number, w = 1, h = 1) => ({
  id, title: id, status: "READY", priority: 100, is_blocked: false, x, y, w, h, depth: 0,
  container_id: null, kind: "card", context_only: false,
  agg_children: 0, agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0,
});
const res = (nodes: ReturnType<typeof node>[], extra: Partial<TilesResponse> = {}): TilesResponse =>
  ({ nodes, edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1, ...extra } as TilesResponse);

describe("layoutStore", () => {
  it("assigns nodes to every intersecting cell", () => {
    const s = mergeTiles(emptyStore(), ["0:0", "1:0"], res([node("a", 1, 1), node("wide", 7, 0, 3, 1)]));
    expect([...s.cells.get("0:0")!]).toEqual(["a", "wide"]);
    expect([...s.cells.get("1:0")!]).toEqual(["wide"]);
    expect(missingCells(s, ["0:0", "2:0"])).toEqual(["2:0"]);
    expect(nodeCount(s)).toBe(2);
  });
  it("resets on version change", () => {
    const s1 = mergeTiles(emptyStore(), ["0:0"], res([node("a", 0, 0)]));
    const s2 = mergeTiles(s1, ["1:0"], res([node("b", 9, 0)], { layout_version: 2 }));
    expect(s2.version).toBe(2);
    expect(s2.nodes.has("a")).toBe(false);
    expect(s2.loaded.has("0:0")).toBe(false);
  });
  it("evicts far cells and orphaned entities", () => {
    const far = mergeTiles(emptyStore(), ["10:10"], res([node("far", 81, 81)]));
    const s = mergeTiles(far, ["0:0"], res([node("a", 0, 0)], {
      edges: [{ from: "a", to: "far", dep_type: "blocks", description: null, count: 1 }],
      stubs: [{ id: "far", project_id: "p", x: 81, y: 81, w: 1, h: 1, title: "far" }],
    }));
    const e = evictFar(s, ["0:0"]);
    expect(e.nodes.has("far")).toBe(false);
    expect(e.nodes.has("a")).toBe(true);
    // the edge survives because its stub target is still referenced via cell 0:0's request
    expect(e.edges.size).toBe(1);
    expect(e.stubs.has("far")).toBe(true);
    expect(e.loaded.has("10:10")).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2/__tests__/layoutStore.test.ts`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

```ts
import type { GraphGate, LayoutEdge, LayoutNode, LayoutStub, LayoutWorker, TilesResponse } from "@aq/ts-client";
import { CELL, cellDistance, parseCell, type CellKey } from "./units";

export interface LayoutStore {
  version: number | null;
  nodes: Map<string, LayoutNode>;
  edges: Map<string, LayoutEdge>;
  stubs: Map<string, LayoutStub>;
  edgeCells: Map<string, Set<CellKey>>;
  workers: LayoutWorker[];
  gates: GraphGate[];
  cells: Map<CellKey, Set<string>>;
  loaded: Set<CellKey>;
}

export const emptyStore = (): LayoutStore => ({
  version: null, nodes: new Map(), edges: new Map(), stubs: new Map(), edgeCells: new Map(),
  workers: [], gates: [], cells: new Map(), loaded: new Set(),
});

const edgeKey = (e: LayoutEdge) => `${e.from}|${e.to}|${e.dep_type}`;

function intersectsCell(n: { x: number; y: number; w: number; h: number }, cell: CellKey): boolean {
  const [cx, cy] = parseCell(cell);
  const x0 = cx * CELL, y0 = cy * CELL, x1 = x0 + CELL, y1 = y0 + CELL;
  return n.x < x1 && n.x + n.w > x0 && n.y < y1 && n.y + n.h > y0;
}

export function mergeTiles(store: LayoutStore, cells: CellKey[], res: TilesResponse): LayoutStore {
  const base = store.version !== null && store.version !== res.layout_version ? emptyStore() : store;
  const next: LayoutStore = {
    version: res.layout_version,
    nodes: new Map(base.nodes), edges: new Map(base.edges), stubs: new Map(base.stubs),
    edgeCells: new Map(base.edgeCells), workers: res.workers ?? [], gates: res.gates ?? [],
    cells: new Map(base.cells), loaded: new Set(base.loaded),
  };
  for (const c of cells) { next.loaded.add(c); if (!next.cells.has(c)) next.cells.set(c, new Set()); }
  for (const n of res.nodes ?? []) {
    next.nodes.set(n.id, n);
    for (const c of cells) if (intersectsCell(n, c)) next.cells.get(c)!.add(n.id);
  }
  for (const s of res.stubs ?? []) next.stubs.set(s.id, s);
  for (const e of res.edges ?? []) {
    const k = edgeKey(e);
    next.edges.set(k, e);
    const owners = next.edgeCells.get(k) ?? new Set<CellKey>();
    for (const c of cells) owners.add(c);
    next.edgeCells.set(k, owners);
  }
  return next;
}

export function missingCells(store: LayoutStore, wanted: CellKey[]): CellKey[] {
  return wanted.filter((c) => !store.loaded.has(c));
}

export const nodeCount = (store: LayoutStore) => store.nodes.size;

export function evictFar(store: LayoutStore, keep: CellKey[], maxDistance = 3): LayoutStore {
  const near = (c: CellKey) => keep.some((k) => cellDistance(c, k) <= maxDistance);
  const cells = new Map<CellKey, Set<string>>();
  const loaded = new Set<CellKey>();
  for (const [c, ids] of store.cells) if (near(c)) { cells.set(c, ids); loaded.add(c); }
  const referenced = new Set<string>();
  for (const ids of cells.values()) for (const id of ids) referenced.add(id);
  const nodes = new Map([...store.nodes].filter(([id]) => referenced.has(id)));
  const edges = new Map<string, LayoutEdge>();
  const edgeCells = new Map<string, Set<CellKey>>();
  for (const [k, e] of store.edges) {
    const owners = new Set([...(store.edgeCells.get(k) ?? [])].filter((c) => loaded.has(c)));
    if (owners.size > 0 && (nodes.has(e.from) || nodes.has(e.to))) { edges.set(k, e); edgeCells.set(k, owners); }
  }
  const needed = new Set<string>();
  for (const e of edges.values()) { if (!nodes.has(e.from)) needed.add(e.from); if (!nodes.has(e.to)) needed.add(e.to); }
  const stubs = new Map([...store.stubs].filter(([id]) => needed.has(id)));
  return { ...store, cells, loaded, nodes, edges, edgeCells, stubs };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2/__tests__/layoutStore.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/command-center/layout-v2
git commit -m "feat(layout-ui): entity store with cell membership and eviction"
```

---

### Task 4: API wrappers and `useLayoutTiles`

**Files:**
- Create: `dashboard/src/api/graphLayout.ts`
- Create: `dashboard/src/pages/command-center/layout-v2/useLayoutTiles.ts`
- Test: `dashboard/src/pages/command-center/layout-v2/__tests__/useLayoutTiles.test.tsx`

**Interfaces:**

`api/graphLayout.ts`:
```ts
export interface TilesParams { variant: "all" | "active"; expanded: string[]; root?: string | null; maxDepth?: number | null; q?: string; status?: string; }
export async function fetchTiles(projectId: string, rect: Rect, params: TilesParams, signal?: AbortSignal): Promise<TilesResponse | { pending: true }>
export function useLayoutExtent(projectId: string | undefined, variant: "all" | "active")   // React Query, refetchInterval 2000 while pending
export function useLayoutNode(projectId: string | undefined, taskId: string | null)
export function useTidyLayout(projectId: string)
export async function locate(projectId: string, variant, q, status): Promise<LocateResponse>
```
`fetchTiles` returns `{pending: true}` on HTTP 202 (the client interceptor throws only on non-2xx).

`useLayoutTiles(projectId, params, viewportRect: Rect | null)` returns `{ store, pending, error, refetchVisible(): void, setViewportRect }`. Behaviour:
1. `paramsKey = JSON.stringify(params)`; when it changes, reset the store to empty and drop in-flight (AbortController).
2. On each `viewportRect` change: `wanted = cellsForRect(rect, 1)`; `missing = missingCells(store, wanted)`; if `missing` non-empty and no request in flight, fetch `cellRect(missing)` with params, merge, then `evictFar(store, wanted)`. If a request is in flight, remember `dirty = true` and re-evaluate when it returns.
3. `refetchVisible()` marks all `wanted` cells as not loaded (drop them from `loaded`) and triggers step 2.
4. When `nodeCount(store) > NODE_BUDGET` and `params.maxDepth` is null or > 0, call `onBudgetExceeded()` (a callback prop) so the canvas can lower `maxDepth`.

- [ ] **Step 1: Write the failing tests**

```tsx
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchTiles = vi.hoisted(() => vi.fn());
vi.mock("../../../../api/graphLayout", () => ({ fetchTiles }));
import { useLayoutTiles } from "../useLayoutTiles";

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
  it("reports pending on 202", async () => {
    fetchTiles.mockResolvedValue({ pending: true });
    const { result } = renderHook(() => useLayoutTiles("p1", params, { x0: 0, y0: 0, x1: 4, y1: 4 }));
    await waitFor(() => expect(result.current.pending).toBe(true));
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2/__tests__/useLayoutTiles.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `api/graphLayout.ts`**

```ts
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getExtentApiProjectsProjectIdGraphExtentGet, getLocateApiProjectsProjectIdGraphLocateGet,
  getNodeApiProjectsProjectIdGraphNodeTaskIdGet, postTidyApiProjectsProjectIdGraphTidyPost,
  postTilesApiProjectsProjectIdGraphTilesPost,
  type ExtentResponse, type LocateResponse, type NodeResponse, type TilesResponse,
} from "@aq/ts-client";
import { client } from "./client";
import type { Rect } from "../pages/command-center/layout-v2/units";

export type Variant = "all" | "active";
export interface TilesParams {
  variant: Variant; expanded: string[]; root?: string | null; maxDepth?: number | null; q?: string; status?: string;
}

export async function fetchTiles(projectId: string, rect: Rect, params: TilesParams, signal?: AbortSignal)
  : Promise<TilesResponse | { pending: true }> {
  const r = await postTilesApiProjectsProjectIdGraphTilesPost({
    client, signal, path: { project_id: projectId },
    body: { variant: params.variant, rect, expanded: params.expanded, root: params.root ?? null,
            max_depth: params.maxDepth ?? null, q: params.q ?? "", status: params.status ?? "" },
    throwOnError: true,
  });
  if (r.response.status === 202) return { pending: true };
  return r.data as TilesResponse;
}

export const layoutExtentKey = (pid: string, variant: Variant) => ["layoutExtent", pid, variant] as const;

export function useLayoutExtent(projectId: string | undefined, variant: Variant) {
  return useQuery({
    queryKey: layoutExtentKey(projectId ?? "", variant),
    enabled: !!projectId,
    queryFn: async ({ signal }): Promise<ExtentResponse | { pending: true }> => {
      const r = await getExtentApiProjectsProjectIdGraphExtentGet({
        client, signal, path: { project_id: projectId! }, query: { variant }, throwOnError: true });
      if (r.response.status === 202) return { pending: true };
      return r.data as ExtentResponse;
    },
    refetchInterval: (q) => (q.state.data && "pending" in q.state.data ? 2000 : 60_000),
    staleTime: 30_000,
  });
}

export function useLayoutExtents(projectIds: string[], variant: Variant): (ExtentResponse | { pending: true } | undefined)[] {
  return useQueries({
    queries: projectIds.map((pid) => ({
      queryKey: layoutExtentKey(pid, variant),
      queryFn: async ({ signal }: { signal: AbortSignal }): Promise<ExtentResponse | { pending: true }> => {
        const r = await getExtentApiProjectsProjectIdGraphExtentGet({
          client, signal, path: { project_id: pid }, query: { variant }, throwOnError: true });
        if (r.response.status === 202) return { pending: true };
        return r.data as ExtentResponse;
      },
      refetchInterval: 60_000, staleTime: 30_000,
    })),
    combine: (results) => results.map((r) => r.data),
  });
}

export function useLayoutNode(projectId: string | undefined, taskId: string | null) {
  return useQuery({
    queryKey: ["layoutNode", projectId, taskId],
    enabled: !!projectId && !!taskId,
    queryFn: async ({ signal }): Promise<NodeResponse> => {
      const r = await getNodeApiProjectsProjectIdGraphNodeTaskIdGet({
        client, signal, path: { project_id: projectId!, task_id: taskId! }, query: { variant: "all" }, throwOnError: true });
      return r.data as NodeResponse;
    },
  });
}

export async function locate(projectId: string, variant: Variant, q: string, status: string): Promise<LocateResponse> {
  const r = await getLocateApiProjectsProjectIdGraphLocateGet({
    client, path: { project_id: projectId }, query: { variant, q, status }, throwOnError: true });
  return r.data as LocateResponse;
}

export function useTidyLayout(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const r = await postTidyApiProjectsProjectIdGraphTidyPost({ client, path: { project_id: projectId }, body: {}, throwOnError: true });
      return r.data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["layoutExtent", projectId] }),
  });
}
```

- [ ] **Step 4: Implement `useLayoutTiles.ts`**

```ts
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchTiles, type TilesParams } from "../../../api/graphLayout";
import { emptyStore, evictFar, mergeTiles, missingCells, nodeCount, type LayoutStore } from "./layoutStore";
import { NODE_BUDGET, cellRect, cellsForRect, type CellKey, type Rect } from "./units";

interface Options { onBudgetExceeded?: () => void }

export function useLayoutTiles(projectId: string | undefined, params: TilesParams, viewportRect: Rect | null, opts: Options = {}) {
  const [store, setStore] = useState<LayoutStore>(emptyStore);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const storeRef = useRef(store); storeRef.current = store;
  const inflight = useRef<AbortController | null>(null);
  const dirty = useRef(false);
  const wantedRef = useRef<CellKey[]>([]);
  const paramsKey = JSON.stringify(params);
  const paramsRef = useRef(params); paramsRef.current = params;
  const onBudget = useRef(opts.onBudgetExceeded); onBudget.current = opts.onBudgetExceeded;

  const load = useCallback(async () => {
    if (!projectId) return;
    const wanted = wantedRef.current;
    const missing = missingCells(storeRef.current, wanted);
    if (missing.length === 0) return;
    if (inflight.current) { dirty.current = true; return; }
    const ac = new AbortController();
    inflight.current = ac;
    try {
      const res = await fetchTiles(projectId, cellRect(missing), paramsRef.current, ac.signal);
      if (ac.signal.aborted) return;
      if ("pending" in res) { setPending(true); return; }
      setPending(false); setError(null);
      const merged = evictFar(mergeTiles(storeRef.current, missing, res), wantedRef.current);
      storeRef.current = merged;
      setStore(merged);
      const depth = paramsRef.current.maxDepth ?? null;
      if (nodeCount(merged) > NODE_BUDGET && (depth === null || depth > 0)) onBudget.current?.();
    } catch (e) {
      if (!ac.signal.aborted) setError(e as Error);
    } finally {
      if (inflight.current === ac) inflight.current = null;
      if (dirty.current) { dirty.current = false; void load(); }
    }
  }, [projectId]);

  // Reset on params change.
  useEffect(() => {
    inflight.current?.abort(); inflight.current = null; dirty.current = false;
    const fresh = emptyStore(); storeRef.current = fresh; setStore(fresh);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsKey, projectId]);

  useEffect(() => {
    if (!viewportRect) return;
    wantedRef.current = cellsForRect(viewportRect, 1);
    void load();
  }, [viewportRect, load]);

  const refetchVisible = useCallback(() => {
    const s = storeRef.current;
    const loaded = new Set(s.loaded);
    for (const c of wantedRef.current) loaded.delete(c);
    storeRef.current = { ...s, loaded };
    inflight.current?.abort(); inflight.current = null;
    void load();
  }, [load]);

  useEffect(() => () => inflight.current?.abort(), []);
  return useMemo(() => ({ store, pending, error, refetchVisible }), [store, pending, error, refetchVisible]);
}
```

Note on `refetchVisible`: a refetch replaces node data but keeps membership from the previous fetch until `mergeTiles` re-adds it; nodes that disappeared server-side (deleted, now collapsed) still linger until eviction. Handle that inside `mergeTiles` consumers: in `refetchVisible`, also remove the wanted cells' node ids from `nodes` before loading:

```ts
    const cells = new Map(s.cells);
    const nodes = new Map(s.nodes);
    for (const c of wantedRef.current) { for (const id of cells.get(c) ?? []) nodes.delete(id); cells.delete(c); }
    storeRef.current = { ...s, loaded, cells, nodes };
```

Use this fuller version.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2/__tests__/useLayoutTiles.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/api/graphLayout.ts dashboard/src/pages/command-center/layout-v2
git commit -m "feat(layout-ui): tiles fetching hook with coalescing, reset, and eviction"
```

---

### Task 5: Flow node conversion and `ContainerNode`

**Files:**
- Create: `dashboard/src/pages/command-center/layout-v2/flowNodes.ts`
- Create: `dashboard/src/pages/command-center/layout-v2/ContainerNode.tsx`
- Modify: `dashboard/src/pages/command-center/types.ts` (add `ContainerNodeData`, `StubNodeData`, and `onFocus?: (taskId: string) => void` on `TaskNodeData`)
- Test: `dashboard/src/pages/command-center/layout-v2/__tests__/flowNodes.test.ts`, `__tests__/ContainerNode.test.tsx`

**Interfaces:**
- `toFlowElements(store: LayoutStore, ctx: { projectId: string; offsetY: number; expanded: ReadonlySet<string>; handlers: { onOpenTask, onToggleChildren, onFocus } }): { nodes: Node[]; edges: Edge[] }`.
  - `kind === "card" | "collapsed" | "stub"` → `type: "task"` node using `TaskNodeData` with a `hierarchy` built from aggregates: `childCount = agg_children`, `descendantCount = agg_descendants`, `completedCount = agg_completed`, `runningCount = agg_running`, `blockedCount = agg_blocked`, `expanded = kind !== "collapsed" && kind !== "stub"`, `visibleChildCount = agg_children`, `autoExpanded = false`, `contextOnly = context_only`, `parentId = container_id`, `parentTitle = null`, `depth`. Position `toPx(x, y + offsetY)`, size 240 × 156, `zIndex: 10 + depth`.
  - `kind === "container"` → `type: "container"` node with `ContainerNodeData = { node: LayoutNode; projectId; onFocus; onToggleChildren; onOpenTask }`, position `toPx(x, y + offsetY)`, size `sizePx(w, h)`, `zIndex: depth`, `selectable: false`.
  - stubs → `type: "task"` with `kind` `stub`, status `"PENDING"`, title from stub, `className: "aq-stub"`.
  - edges: `id = \`${from}|${to}|${dep_type}\``, `source: to`, `target: from` (arrow points at the dependent, as today), `type: "smoothstep"`, handles `out-bottom → in-top` when the target's y is greater than the source's, else `out-right → in-left`, `label: count > 1 ? \`×${count}\` : undefined`, `markerEnd: { type: MarkerType.ArrowClosed }`, `style: edgeStyleForType(dep_type)`, `data: { depType }`. Edges whose endpoints are both missing from `nodes` and `stubs` are dropped.
- `ContainerNode`: renders a bordered box with a header band (`HEADER_H` × `UNIT_H` px tall) containing the title, status, `${agg_completed}/${agg_descendants} done`, a collapse button (`aria-label="Collapse children of <title>"`) calling `onToggleChildren(id)`, a Focus button (`aria-label="Focus on <title>"`) calling `onFocus(id)`, and an open button (`aria-label="Open task <title>"`, `data-task-id`) calling `onOpenTask(id)`. Four handles like `TaskNode`.

- [ ] **Step 1: Write the failing tests**

`flowNodes.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { emptyStore, mergeTiles } from "../layoutStore";
import { toFlowElements } from "../flowNodes";

const n = (id: string, kind: string, x: number, y: number, extra = {}) => ({
  id, title: id, status: "READY", priority: 100, is_blocked: false, x, y, w: 1, h: 1, depth: 0,
  container_id: null, kind, context_only: false,
  agg_children: 2, agg_descendants: 3, agg_completed: 1, agg_running: 0, agg_blocked: 0, agg_active: 2, ...extra,
});
const ctx = { projectId: "p1", offsetY: 0, expanded: new Set<string>(), handlers: { onOpenTask: () => {}, onToggleChildren: () => {}, onFocus: () => {} } };

describe("toFlowElements", () => {
  it("maps kinds to node types and scales positions", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("e", "container", 0, 0, { w: 3, h: 2 }), n("c", "collapsed", 1, 4), n("z", "card", 2, 4)],
      edges: [{ from: "z", to: "c", dep_type: "blocks", description: null, count: 2 }],
      stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const { nodes, edges } = toFlowElements(store, ctx);
    const byId = Object.fromEntries(nodes.map((x) => [x.id, x]));
    expect(byId.e.type).toBe("container");
    expect(byId.e.position).toEqual({ x: 0, y: 0 });
    expect(byId.e.width).toBe(720); expect(byId.e.height).toBe(312);
    expect(byId.c.type).toBe("task");
    expect((byId.c.data as { hierarchy: { expanded: boolean; descendantCount: number } }).hierarchy).toMatchObject({ expanded: false, descendantCount: 3 });
    expect(byId.z.position).toEqual({ x: 480, y: 624 });
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({ source: "c", target: "z", label: "×2", sourceHandle: "out-right", targetHandle: "in-left" });
  });
  it("renders stubs as dashed task nodes and drops edges with no endpoints", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("z", "card", 0, 0)],
      edges: [{ from: "z", to: "far", dep_type: "blocks", description: null, count: 1 },
              { from: "gone", to: "gone2", dep_type: "blocks", description: null, count: 1 }],
      stubs: [{ id: "far", project_id: "p1", x: 5, y: 0, w: 1, h: 1, title: "Far" }],
      stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const { nodes, edges } = toFlowElements(store, ctx);
    expect(nodes.find((x) => x.id === "far")?.className).toBe("aq-stub");
    expect(edges).toHaveLength(1);
  });
  it("applies the project offset", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], { nodes: [n("z", "card", 0, 1)], edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1 } as never);
    expect(toFlowElements(store, { ...ctx, offsetY: 10 }).nodes[0]!.position.y).toBe(11 * 156);
  });
});
```

`ContainerNode.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
vi.mock("@xyflow/react", () => ({ Handle: () => null, Position: { Top: "t", Bottom: "b", Left: "l", Right: "r" } }));
import ContainerNode from "../ContainerNode";

const node = { id: "e", title: "Epic", status: "IN_PROGRESS", priority: 100, is_blocked: false, x: 0, y: 0, w: 3, h: 2, depth: 0,
  container_id: null, kind: "container", context_only: false, agg_children: 3, agg_descendants: 5, agg_completed: 2, agg_running: 1, agg_blocked: 0, agg_active: 3 };

describe("ContainerNode", () => {
  it("shows aggregates and wires collapse, focus, and open", async () => {
    const onFocus = vi.fn(), onToggleChildren = vi.fn(), onOpenTask = vi.fn();
    render(<ContainerNode id="e" data={{ node, projectId: "p1", onFocus, onToggleChildren, onOpenTask }} selected={false} /> as never);
    expect(screen.getByText("2/5 done")).toBeInTheDocument();
    expect(screen.getByText("1 running")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Focus on Epic" }));
    await userEvent.click(screen.getByRole("button", { name: "Collapse children of Epic" }));
    await userEvent.click(screen.getByRole("button", { name: "Open task Epic" }));
    expect(onFocus).toHaveBeenCalledWith("e");
    expect(onToggleChildren).toHaveBeenCalledWith("e");
    expect(onOpenTask).toHaveBeenCalledWith("e");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2/__tests__/flowNodes.test.ts src/pages/command-center/layout-v2/__tests__/ContainerNode.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

`types.ts` additions:
```ts
export interface ContainerNodeData extends Record<string, unknown> {
  node: import("@aq/ts-client").LayoutNode;
  projectId: string;
  onFocus?: (taskId: string) => void;
  onToggleChildren?: (taskId: string) => void;
  onOpenTask?: (taskId: string) => void;
}
```
and on `TaskNodeData`: `onFocus?: (taskId: string) => void;`.

`flowNodes.ts`:
```ts
import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type { LayoutNode } from "@aq/ts-client";
import { edgeStyleForType } from "../layout";
import type { ContainerNodeData, TaskNodeData } from "../types";
import type { LayoutStore } from "./layoutStore";
import { sizePx, toPx } from "./units";

export interface FlowHandlers { onOpenTask: (id: string) => void; onToggleChildren: (id: string) => void; onFocus: (id: string) => void }
export interface FlowContext { projectId: string; offsetY: number; expanded: ReadonlySet<string>; handlers: FlowHandlers }

function taskData(n: LayoutNode, ctx: FlowContext): TaskNodeData {
  const { x: _x, y: _y, w: _w, h: _h, depth, container_id, kind, context_only, agg_children, agg_descendants, agg_completed, agg_running, agg_blocked, agg_active: _a, ...task } = n;
  return {
    task, gates: [], projectId: ctx.projectId,
    hierarchy: {
      parentId: container_id ?? null, parentTitle: null, depth, childCount: agg_children,
      visibleChildCount: agg_children, descendantCount: agg_descendants, completedCount: agg_completed,
      runningCount: agg_running, blockedCount: agg_blocked, expanded: kind !== "collapsed" && kind !== "stub",
      autoExpanded: false, contextOnly: context_only,
    },
    onOpenTask: ctx.handlers.onOpenTask, onToggleChildren: ctx.handlers.onToggleChildren, onFocus: ctx.handlers.onFocus,
  };
}

export function toFlowElements(store: LayoutStore, ctx: FlowContext): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const pos = new Map<string, { x: number; y: number }>();
  for (const n of store.nodes.values()) {
    const position = toPx(n.x, n.y + ctx.offsetY);
    pos.set(n.id, { x: n.x, y: n.y });
    if (n.kind === "container") {
      const data: ContainerNodeData = { node: n, projectId: ctx.projectId, ...ctx.handlers };
      nodes.push({ id: n.id, type: "container", position, ...sizePx(n.w, n.h), zIndex: n.depth, selectable: false, draggable: false, connectable: false, data });
    } else {
      nodes.push({ id: n.id, type: "task", position, ...sizePx(1, 1), zIndex: 10 + n.depth, draggable: false, connectable: false, data: taskData(n, ctx) });
    }
  }
  for (const s of store.stubs.values()) {
    if (store.nodes.has(s.id)) continue;
    pos.set(s.id, { x: s.x, y: s.y });
    const stub: LayoutNode = { id: s.id, title: s.title ?? s.id, status: "PENDING", priority: 100, is_blocked: false, x: s.x, y: s.y, w: 1, h: 1, depth: 0,
      container_id: null, kind: "stub", context_only: true, agg_children: 0, agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0 } as LayoutNode;
    nodes.push({ id: s.id, type: "task", className: "aq-stub", position: toPx(s.x, s.y + ctx.offsetY), ...sizePx(1, 1), zIndex: 5, draggable: false, connectable: false, data: taskData(stub, ctx) });
  }
  const edges: Edge[] = [];
  for (const e of store.edges.values()) {
    const from = pos.get(e.from), to = pos.get(e.to);
    if (!from || !to) continue;
    const vertical = from.y > to.y + 0.5;
    edges.push({
      id: `${e.from}|${e.to}|${e.dep_type}`, source: e.to, target: e.from, type: "smoothstep",
      sourceHandle: vertical ? "out-bottom" : "out-right", targetHandle: vertical ? "in-top" : "in-left",
      label: e.count > 1 ? `×${e.count}` : undefined, markerEnd: { type: MarkerType.ArrowClosed },
      style: edgeStyleForType(e.dep_type), data: { depType: e.dep_type },
    });
  }
  return { nodes, edges };
}
```

`ContainerNode.tsx`:
```tsx
import { ChevronDownIcon, MagnifyingGlassPlusIcon } from "@heroicons/react/24/outline";
import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import type { ContainerNodeData } from "../types";
import { UNIT_H } from "./units";

const HEADER_PX = 0.35 * UNIT_H;

export default function ContainerNode({ data, selected }: NodeProps<Node<ContainerNodeData, "container">>) {
  const { node, onFocus, onToggleChildren, onOpenTask } = data;
  return (
    <div data-container-id={node.id} className={`h-full w-full rounded-lg border border-white/15 bg-white/[0.03] ${selected ? "outline outline-2 outline-white" : ""} ${node.context_only ? "border-dashed" : ""}`}>
      <Handle id="in-left" type="target" position={Position.Left} isConnectable={false} />
      <Handle id="in-top" type="target" position={Position.Top} isConnectable={false} />
      <div className="flex items-center gap-2 px-2 text-[11px] text-gray-200" style={{ height: HEADER_PX }}>
        <button type="button" aria-label={`Open task ${node.title}`} data-task-id={node.id}
          className="nodrag nopan min-w-0 flex-1 truncate text-left font-medium hover:underline"
          onClick={(e) => { e.stopPropagation(); onOpenTask?.(node.id); }}>{node.title}</button>
        <span className="shrink-0 text-[9px] uppercase tracking-wide opacity-70">{node.status.replace(/_/g, " ")}</span>
        <span className="shrink-0 rounded bg-white/10 px-1">{node.agg_completed}/{node.agg_descendants} done</span>
        {node.agg_running > 0 && <span className="shrink-0 text-indigo-300">{node.agg_running} running</span>}
        {node.agg_blocked > 0 && <span className="shrink-0 text-amber-300">{node.agg_blocked} blocked</span>}
        <button type="button" aria-label={`Focus on ${node.title}`} className="nodrag nopan rounded p-0.5 hover:bg-white/10"
          onClick={(e) => { e.stopPropagation(); onFocus?.(node.id); }}><MagnifyingGlassPlusIcon className="h-3.5 w-3.5" /></button>
        <button type="button" aria-label={`Collapse children of ${node.title}`} aria-expanded className="nodrag nopan rounded p-0.5 hover:bg-white/10"
          onClick={(e) => { e.stopPropagation(); onToggleChildren?.(node.id); }}><ChevronDownIcon className="h-3.5 w-3.5" /></button>
      </div>
      <Handle id="out-right" type="source" position={Position.Right} isConnectable={false} />
      <Handle id="out-bottom" type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}
```

Also add a Focus button to `TaskNode.tsx`'s footer for `collapsed` cards: next to the expand button, when `data.onFocus` is defined and `hierarchy.childCount > 0`, render `<button aria-label={\`Focus on ${task.title}\`} …>` calling `onFocus(task.id)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard && npx vitest run src/pages/command-center`
Expected: PASS, including the existing `TaskNode`-related tests.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/command-center
git commit -m "feat(layout-ui): flow element conversion and container node"
```

---

### Task 6: Focus in the workspace context and breadcrumbs

**Files:**
- Modify: `dashboard/src/pages/command-center/taskFilters.ts` (read/write `focus` param)
- Modify: `dashboard/src/pages/command-center/TaskWorkspace.tsx` (`focusId`, `setFocus`)
- Create: `dashboard/src/pages/command-center/layout-v2/Breadcrumbs.tsx`
- Test: `dashboard/src/pages/command-center/__tests__/taskFilters.test.ts` (append), `layout-v2/__tests__/Breadcrumbs.test.tsx`

**Interfaces:**
- `TaskFilters` gains `focus: string` (empty = none). `readTaskFilters` reads `?focus=`; `writeTaskFilters` writes it or deletes it when empty.
- `useTaskWorkspace()` gains `focusId: string | null` and `setFocus(id: string | null)`. While `focusId` is set, `setShowCompleted` is ignored and `filters.showCompleted` reads as `true`.
- `Breadcrumbs({ projectName, ancestors: {id,title}[], current: {id,title} | null, onSelect(id: string | null) })` renders `Project › A › B › Current`; each crumb except the current is a button; the project crumb calls `onSelect(null)`.

- [ ] **Step 1: Write the failing tests**

Append to `taskFilters.test.ts`:
```ts
it("round-trips the focus param", () => {
  const p = writeTaskFilters(new URLSearchParams(), { query: "", status: "", showCompleted: false, focus: "e1" });
  expect(p.get("focus")).toBe("e1");
  expect(readTaskFilters(p).focus).toBe("e1");
  expect(writeTaskFilters(p, { query: "", status: "", showCompleted: false, focus: "" }).has("focus")).toBe(false);
});
```

`Breadcrumbs.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Breadcrumbs from "../Breadcrumbs";

describe("Breadcrumbs", () => {
  it("renders the path and navigates", async () => {
    const onSelect = vi.fn();
    render(<Breadcrumbs projectName="agent-queue" ancestors={[{ id: "e", title: "Epic" }]} current={{ id: "p", title: "Pkg" }} onSelect={onSelect} />);
    expect(screen.getByText("Pkg")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Epic" }));
    expect(onSelect).toHaveBeenCalledWith("e");
    await userEvent.click(screen.getByRole("button", { name: "agent-queue" }));
    expect(onSelect).toHaveBeenCalledWith(null);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard && npx vitest run src/pages/command-center`
Expected: the two new tests FAIL.

- [ ] **Step 3: Implement**

`taskFilters.ts`: add `focus: string` to `TaskFilters`; in `readTaskFilters` add `focus: params.get("focus") ?? ""`; in `writeTaskFilters`, `if (filters.focus) next.set("focus", filters.focus); else next.delete("focus");`. Update every literal `TaskFilters` in tests and `clearFilters` to include `focus: ""` (clearFilters keeps focus untouched: pass `focus: current.focus`).

`TaskWorkspace.tsx`: 
```ts
  const focusId = filters.focus || null;
  const setFocus = useCallback((id: string | null) => update({ focus: id ?? "" }), [update]);
```
and in `setShowCompleted`, return early when `readTaskFilters(previous).focus`. Expose `focusId` and `setFocus` in the context value and interface. Where `filters` is exposed, derive `showCompleted: filters.showCompleted || !!focusId`.

`Breadcrumbs.tsx`:
```tsx
interface Crumb { id: string; title: string }
interface Props { projectName: string; ancestors: Crumb[]; current: Crumb | null; onSelect: (id: string | null) => void }

export default function Breadcrumbs({ projectName, ancestors, current, onSelect }: Props) {
  const crumbs: (Crumb | null)[] = [null, ...ancestors];
  return (
    <nav aria-label="Focus path" className="flex shrink-0 flex-wrap items-center gap-1 border-b border-gray-800 px-4 py-1 text-xs text-gray-300">
      {crumbs.map((c, i) => (
        <span key={c?.id ?? "root"} className="flex items-center gap-1">
          {i > 0 && <span aria-hidden className="text-gray-600">›</span>}
          <button type="button" className="rounded px-1 hover:bg-white/10 hover:underline" onClick={() => onSelect(c?.id ?? null)}>{c ? c.title : projectName}</button>
        </span>
      ))}
      {current && <span className="flex items-center gap-1"><span aria-hidden className="text-gray-600">›</span><span aria-current="page" className="px-1 font-medium text-white">{current.title}</span></span>}
    </nav>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard && npx vitest run src/pages/command-center && npm run typecheck`
Expected: PASS and clean types.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/command-center
git commit -m "feat(layout-ui): focus URL param and breadcrumbs"
```

---

### Task 7: `LayoutCanvas`

**Files:**
- Create: `dashboard/src/pages/command-center/layout-v2/LayoutCanvas.tsx`
- Test: `dashboard/src/pages/command-center/layout-v2/__tests__/LayoutCanvas.test.tsx`

**Interfaces:**
- Props: `{ projectIds: string[]; projectNames: Map<string,string>; variant: Variant; filters: TaskFilters; focusId: string | null; setFocus(id|null); selectedTaskId?: string|null; onTaskClick(id); onBackgroundClick?(); playbooks?: PlaybookSummary[]; selectedPlaybookId?: string|null; onPlaybookClick?(id) }`.
- Renders `<ReactFlow onlyRenderVisibleElements nodeTypes={{ task, playbook, container }} …>` with the same interaction props as `GraphCanvas` (non-draggable, pan on scroll, minZoom 0.15, maxZoom 2).
- Internals:
  - `useGraphHierarchy`'s expanded set is reused through a smaller hook: extract `readExpandedTaskIds`/`persistExpandedTaskIds` and a `useExpandedTaskIds()` hook from `useGraphHierarchy.ts` (export them) so both canvases share the localStorage key.
  - One `useLayoutTiles` per project (a `ProjectLayer` child component per project id, each with its own `offsetY`), collected into one node/edge array. Offsets come from `useLayoutExtent`: project *i* starts at `sum(extent_h of projects < i) + 2*i` units, with a header band node (`type: "playbook"`-style label) at each offset when there is more than one project.
  - Viewport tracking: `onMove` (throttled with `requestAnimationFrame`) computes the world rect via `worldRectFromViewport` per project (subtract that project's `offsetY`) and sets `maxDepth` from `maxDepthForZoom(zoom)`; a budget callback lowers `maxDepthOverride` by one.
  - Params to `useLayoutTiles`: `{ variant: focusId ? "all" : variant, expanded: [...expanded], root: focusId, maxDepth, q: filters.query.trim(), status: filters.status }`.
  - Focus: when `focusId` changes, `useLayoutNode` fetches the node; on data, `fitBounds` to its box (converted with `toPx`/`sizePx` and the project offset) and render `<Breadcrumbs>` above the flow. Dependencies leaving the subtree already arrive as stubs.
  - Pending: show "Laying out…" overlay while any project layer reports `pending`.
  - Keyboard navigation, selection decoration, and the relation legend are copied from `GraphCanvas` unchanged.
  - Workers: render `AgentAvatarLayer` with `agents` synthesized from the union of layers' `store.workers` as `{ id: agent_id, name, current_task_id: docked_at, profile_id: null, session_id: null }` and `visibleTaskById` as identity (docking is already resolved server-side).
  - Playbook header band: `prependPlaybookRows`'s logic is replaced by placing playbook cards in a row above `offsetY = 0` at `y = -1.5` units (one row, 4 per line), reusing `PlaybookNode`.

- [ ] **Step 1: Write the failing tests**

The React Flow mock from `GraphCanvas.test.tsx` is reused; extend it with `useReactFlow: () => ({ fitBounds: fitBounds })` and `onMove` capture. Mock `../useLayoutTiles` and `../../../../api/graphLayout`.

```tsx
import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const flow = vi.hoisted(() => ({ current: null as null | { nodes: { id: string; type?: string }[]; onMove?: (e: unknown, vp: { x: number; y: number; zoom: number }) => void; onlyRenderVisibleElements?: boolean } }));
const fitBounds = vi.hoisted(() => vi.fn());
vi.mock("@xyflow/react", () => ({
  MarkerType: { ArrowClosed: "arrowclosed" },
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  ReactFlowProvider: ({ children }: { children: ReactNode }) => children,
  useReactFlow: () => ({ fitBounds }),
  ReactFlow: (props: never) => { flow.current = props; const p = props as unknown as { nodes: { id: string }[]; children: ReactNode }; return <div>{p.nodes.map((n) => <div key={n.id} data-testid={`node-${n.id}`} />)}{p.children}</div>; },
  Background: () => null, Handle: () => null, Controls: () => null, Panel: ({ children }: { children: ReactNode }) => <aside>{children}</aside>,
  ViewportPortal: ({ children }: { children: ReactNode }) => <>{children}</>, useNodes: () => [],
}));
const tiles = vi.hoisted(() => ({ store: null as unknown, pending: false, error: null, refetchVisible: vi.fn(), params: null as unknown }));
vi.mock("../useLayoutTiles", () => ({ useLayoutTiles: (_p: string, params: unknown) => { tiles.params = params; return tiles; } }));
const layoutNode = vi.hoisted(() => ({ data: undefined as unknown }));
vi.mock("../../../../api/graphLayout", () => ({
  useLayoutExtents: (ids: string[]) => ids.map(() => ({ layout_version: 1, extent_w: 10, extent_h: 10, node_count: 3 })),
  useLayoutNode: () => layoutNode, locate: vi.fn(), useTidyLayout: () => ({ mutate: vi.fn() }),
}));
import { emptyStore, mergeTiles } from "../layoutStore";
import LayoutCanvas from "../LayoutCanvas";

const n = (id: string, kind: string, x: number, y: number, extra = {}) => ({ id, title: id, status: "READY", priority: 100, is_blocked: false, x, y, w: 1, h: 1, depth: 0, container_id: null, kind, context_only: false, agg_children: 1, agg_descendants: 1, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 1, ...extra });
const filters = { query: "", status: "", showCompleted: false, focus: "" };
const base = { projectIds: ["p1"], projectNames: new Map([["p1", "P1"]]), variant: "active" as const, filters, focusId: null, setFocus: vi.fn(), onTaskClick: vi.fn() };

beforeEach(() => {
  tiles.store = mergeTiles(emptyStore(), ["0:0"], { nodes: [n("e", "collapsed", 0, 0), n("z", "card", 2, 0)], edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1 } as never);
  fitBounds.mockReset(); layoutNode.data = undefined; localStorage.clear();
});

describe("LayoutCanvas", () => {
  it("renders server nodes with visibility culling and sends viewport-derived params", () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    expect(screen.getByTestId("node-e")).toBeInTheDocument();
    expect(flow.current?.onlyRenderVisibleElements).toBe(true);
    expect(tiles.params).toMatchObject({ variant: "active", expanded: [], root: null, q: "", status: "" });
  });
  it("lowers max depth when zoomed out", () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    flow.current!.onMove!(null, { x: 0, y: 0, zoom: 0.2 });
    expect((tiles.params as { maxDepth: number }).maxDepth).toBe(0);
  });
  it("focus forces variant all, sets root, fits bounds, and shows breadcrumbs", () => {
    layoutNode.data = { node: { ...n("e", "container", 0, 0, { w: 3, h: 2 }) }, ancestors: [], layout_version: 1 };
    render(<MemoryRouter><LayoutCanvas {...base} focusId="e" /></MemoryRouter>);
    expect(tiles.params).toMatchObject({ variant: "all", root: "e" });
    expect(fitBounds).toHaveBeenCalledWith({ x: 0, y: 0, width: 720, height: 312 }, expect.anything());
    expect(screen.getByRole("navigation", { name: "Focus path" })).toHaveTextContent("P1");
  });
  it("toggling a collapsed card adds it to expanded params", async () => {
    render(<MemoryRouter><LayoutCanvas {...base} /></MemoryRouter>);
    const node = flow.current!.nodes.find((x) => x.id === "e") as unknown as { data: { onToggleChildren: (id: string) => void } };
    node.data.onToggleChildren("e");
    await screen.findByTestId("node-e");
    expect((tiles.params as { expanded: string[] }).expanded).toEqual(["e"]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2/__tests__/LayoutCanvas.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement**

First, in `useGraphHierarchy.ts`, export `useExpandedTaskIds()`:
```ts
export function useExpandedTaskIds() {
  const [expandedTaskIds, setExpandedTaskIds] = useState<ReadonlySet<string>>(readExpandedTaskIds);
  useEffect(() => persistExpandedTaskIds(expandedTaskIds), [expandedTaskIds]);
  const toggleExpanded = useCallback((id: string) => setExpandedTaskIds((prev) => { const next = new Set(prev); if (!next.delete(id)) next.add(id); return next; }), []);
  return { expandedTaskIds, toggleExpanded };
}
```
and have `useGraphHierarchy` call it.

`LayoutCanvas.tsx`:
```tsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, Panel, ReactFlow, ReactFlowProvider, useReactFlow, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import TaskNode from "../TaskNode";
import PlaybookNode from "../PlaybookNode";
import AgentAvatarLayer from "../AgentAvatarLayer";
import ContainerNode from "./ContainerNode";
import Breadcrumbs from "./Breadcrumbs";
import { edgeStyleForType } from "../layout";
import { useExpandedTaskIds } from "../useGraphHierarchy";
import { useLayoutExtents, useLayoutNode, type TilesParams, type Variant } from "../../../api/graphLayout";
import { useLayoutTiles } from "./useLayoutTiles";
import { toFlowElements } from "./flowNodes";
import { maxDepthForZoom, sizePx, toPx, worldRectFromViewport, type Rect } from "./units";
import type { GraphAgent, GraphViewProps, TaskNodeData } from "../types";
import type { TaskFilters } from "../taskFilters";
import { NODE_HEIGHT, NODE_WIDTH } from "../types";

const nodeTypes = { task: TaskNode, playbook: PlaybookNode, container: ContainerNode };
const PROJECT_GAP = 2;

export interface LayoutCanvasProps extends Pick<GraphViewProps, "onTaskClick" | "onBackgroundClick" | "selectedTaskId" | "playbooks" | "selectedPlaybookId" | "onPlaybookClick"> {
  projectIds: string[]; projectNames: Map<string, string>; variant: Variant; filters: TaskFilters;
  focusId: string | null; setFocus: (id: string | null) => void;
}

interface LayerProps { projectId: string; offsetY: number; params: TilesParams; viewport: { x: number; y: number; zoom: number } | null;
  size: { w: number; h: number }; expanded: ReadonlySet<string>; handlers: { onOpenTask: (id: string) => void; onToggleChildren: (id: string) => void; onFocus: (id: string) => void };
  onBudgetExceeded: () => void; onElements: (pid: string, nodes: Node[], edges: Edge[], workers: GraphAgent[], pending: boolean) => void; refetchRef: React.MutableRefObject<Map<string, () => void>> }

function ProjectLayer({ projectId, offsetY, params, viewport, size, expanded, handlers, onBudgetExceeded, onElements, refetchRef }: LayerProps) {
  const rect = useMemo<Rect | null>(() => {
    if (!viewport || size.w === 0) return null;
    const r = worldRectFromViewport(viewport, size.w, size.h);
    return { x0: r.x0, y0: r.y0 - offsetY, x1: r.x1, y1: r.y1 - offsetY };
  }, [viewport, size, offsetY]);
  const { store, pending, refetchVisible } = useLayoutTiles(projectId, params, rect, { onBudgetExceeded });
  useEffect(() => { refetchRef.current.set(projectId, refetchVisible); return () => { refetchRef.current.delete(projectId); }; }, [projectId, refetchVisible, refetchRef]);
  useEffect(() => {
    const { nodes, edges } = toFlowElements(store, { projectId, offsetY, expanded, handlers });
    const workers: GraphAgent[] = store.workers.map((w) => ({ id: w.agent_id, name: w.name, current_task_id: w.docked_at, profile_id: null, session_id: null }));
    onElements(projectId, nodes, edges, workers, pending);
  }, [store, pending, projectId, offsetY, expanded, handlers, onElements]);
  return null;
}

function Inner(props: LayoutCanvasProps) {
  const { projectIds, projectNames, variant, filters, focusId, setFocus, onTaskClick, onBackgroundClick, selectedTaskId, playbooks = [], selectedPlaybookId, onPlaybookClick } = props;
  const { expandedTaskIds, toggleExpanded } = useExpandedTaskIds();
  const { fitBounds } = useReactFlow();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [viewport, setViewport] = useState<{ x: number; y: number; zoom: number } | null>({ x: 0, y: 0, zoom: 1 });
  const [depthOverride, setDepthOverride] = useState<number | null>(null);
  const [layers, setLayers] = useState<Map<string, { nodes: Node[]; edges: Edge[]; workers: GraphAgent[]; pending: boolean }>>(new Map());
  const refetchRef = useRef(new Map<string, () => void>());
  const raf = useRef<number | null>(null);

  useEffect(() => {
    const el = wrapRef.current; if (!el) return;
    setSize({ w: el.clientWidth, h: el.clientHeight });
    const ro = new ResizeObserver(([e]) => { if (e) setSize({ w: e.contentRect.width, h: e.contentRect.height }); });
    ro.observe(el); return () => ro.disconnect();
  }, []);

  const onMove = useCallback((_: unknown, vp: { x: number; y: number; zoom: number }) => {
    if (raf.current !== null) cancelAnimationFrame(raf.current);
    raf.current = requestAnimationFrame(() => { raf.current = null; setViewport(vp); });
    if (typeof requestAnimationFrame !== "function") setViewport(vp);
  }, []);
  const zoomDepth = maxDepthForZoom(viewport?.zoom ?? 1);
  const maxDepth = depthOverride !== null ? Math.min(depthOverride, zoomDepth ?? Infinity) : zoomDepth;
  useEffect(() => { setDepthOverride(null); }, [viewport?.zoom]);
  const onBudgetExceeded = useCallback(() => setDepthOverride((d) => Math.max(0, (d ?? (zoomDepth ?? 2)) - 1)), [zoomDepth]);

  // Project offsets from extents.
  const extents = useLayoutExtents(projectIds, focusId ? "all" : variant);
  const offsets = useMemo(() => {
    const out = new Map<string, number>(); let y = 0;
    projectIds.forEach((pid, i) => { out.set(pid, y); const e = extents[i]; y += (e && !("pending" in e) ? e.extent_h : 0) + (projectIds.length > 1 ? PROJECT_GAP : 0); });
    return out;
  }, [projectIds, extents]);

  const params = useMemo<TilesParams>(() => ({
    variant: focusId ? "all" : variant, expanded: [...expandedTaskIds].sort(), root: focusId,
    maxDepth: maxDepth === Infinity ? null : maxDepth, q: filters.query.trim(), status: filters.status,
  }), [variant, focusId, expandedTaskIds, maxDepth, filters.query, filters.status]);

  const [localSelectedId, setLocalSelectedId] = useState<string | null>(null);
  const selectedId = selectedPlaybookId ? `playbook:${selectedPlaybookId}` : selectedTaskId === undefined ? localSelectedId : selectedTaskId;
  const openTask = useCallback((id: string) => { setLocalSelectedId(id); onTaskClick(id); }, [onTaskClick]);
  const handlers = useMemo(() => ({ onOpenTask: openTask, onToggleChildren: toggleExpanded, onFocus: (id: string) => setFocus(id) }), [openTask, toggleExpanded, setFocus]);
  const onElements = useCallback((pid: string, nodes: Node[], edges: Edge[], workers: GraphAgent[], pending: boolean) =>
    setLayers((prev) => new Map(prev).set(pid, { nodes, edges, workers, pending })), []);

  const nodes = useMemo(() => {
    const playbookNodes: Node[] = playbooks.map((playbook, i) => ({
      id: `playbook:${playbook.id}`, type: "playbook", position: toPx(i % 4, -1.5 - Math.floor(i / 4) * 1.3), width: NODE_WIDTH, height: NODE_HEIGHT,
      draggable: false, connectable: false, data: { playbook, onOpenPlaybook: onPlaybookClick },
    }));
    const headers: Node[] = projectIds.length > 1 ? projectIds.map((pid) => ({
      id: `project:${pid}`, type: "playbook", position: toPx(0, (offsets.get(pid) ?? 0) - 0.8), width: NODE_WIDTH, height: 40, selectable: false, draggable: false,
      data: { playbook: { id: pid, name: projectNames.get(pid) ?? pid } }, className: "aq-project-header",
    })) : [];
    const all = [...playbookNodes, ...headers, ...projectIds.flatMap((pid) => layers.get(pid)?.nodes ?? [])];
    return all.map((n) => ({ ...n, selected: n.id === selectedId }));
  }, [playbooks, projectIds, offsets, layers, selectedId, onPlaybookClick, projectNames]);
  const edges = useMemo(() => projectIds.flatMap((pid) => layers.get(pid)?.edges ?? []), [projectIds, layers]);
  const workers = useMemo(() => projectIds.flatMap((pid) => layers.get(pid)?.workers ?? []), [projectIds, layers]);
  const pending = projectIds.some((pid) => layers.get(pid)?.pending);
  const relationTypes = useMemo(() => [...new Set(edges.map((e) => String(e.data?.depType)))].sort(), [edges]);

  // Focus: fit to the container and show breadcrumbs.
  const focusProject = projectIds[0];
  const { data: focusNode } = useLayoutNode(focusId ? focusProject : undefined, focusId);
  useEffect(() => {
    if (!focusId || !focusNode) return;
    const off = offsets.get(focusProject ?? "") ?? 0;
    const p = toPx(focusNode.node.x, focusNode.node.y + off); const s = sizePx(focusNode.node.w, focusNode.node.h);
    fitBounds({ x: p.x, y: p.y, width: s.width, height: s.height }, { padding: 0.1, duration: 0 });
  }, [focusId, focusNode, fitBounds, offsets, focusProject]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {focusId && <Breadcrumbs projectName={projectNames.get(focusProject ?? "") ?? "Project"}
        ancestors={focusNode?.ancestors.map((a) => ({ id: a.id, title: a.title })) ?? []}
        current={focusNode ? { id: focusNode.node.id, title: focusNode.node.title } : { id: focusId, title: focusId }} onSelect={setFocus} />}
      <div ref={wrapRef} role="region" aria-label="Task graph" tabIndex={0} className="relative min-h-0 flex-1 outline-none">
        {projectIds.map((pid) => <ProjectLayer key={pid} projectId={pid} offsetY={offsets.get(pid) ?? 0} params={params} viewport={viewport} size={size}
          expanded={expandedTaskIds} handlers={handlers} onBudgetExceeded={onBudgetExceeded} onElements={onElements} refetchRef={refetchRef} />)}
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} colorMode="dark" onlyRenderVisibleElements
          defaultViewport={{ x: 0, y: 0, zoom: 1 }} minZoom={0.15} maxZoom={2} onMove={onMove}
          nodesDraggable={false} nodesConnectable={false} nodesFocusable={false} edgesFocusable={false} elementsSelectable={false}
          deleteKeyCode={null} selectionKeyCode={null} disableKeyboardA11y nodeClickDistance={5} panOnScroll zoomOnScroll={false}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, node) => { if (node.type === "playbook") onPlaybookClick?.(String((node.data.playbook as { id: string }).id)); else openTask(node.id); }}
          onPaneClick={() => { setLocalSelectedId(null); onBackgroundClick?.(); }}>
          <Background gap={24} color="#1f2937" />
          <Controls position="bottom-right" showInteractive={false} />
          <AgentAvatarLayer agents={workers} />
          {relationTypes.length > 0 && <Panel position="bottom-left">
            <details className="max-w-xs rounded border border-gray-700 bg-gray-950/95 px-3 py-2 text-[10px] text-gray-300">
              <summary className="cursor-pointer">Dependencies · arrows point to dependent tasks</summary>
              <ul className="mt-2 space-y-1">{relationTypes.map((t) => <li key={t} className="flex items-center gap-2"><svg aria-hidden width="28" height="10"><path d="M0 5h25m-4-3 4 3-4 3" fill="none" style={edgeStyleForType(t)} /></svg>{t}</li>)}</ul>
            </details></Panel>}
        </ReactFlow>
        {pending && <div role="status" className="pointer-events-none absolute inset-0 flex items-center justify-center bg-gray-950/70 text-sm text-gray-300">Laying out…</div>}
        {!pending && nodes.length === 0 && <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-gray-500">No tasks or playbooks match these filters.</p>}
      </div>
    </div>
  );
}

export default function LayoutCanvas(props: LayoutCanvasProps) {
  return <ReactFlowProvider><Inner {...props} /></ReactFlowProvider>;
}
```

Also port the keyboard navigation (`onKeyDown` with `nearestIn`) from `GraphCanvas.tsx` onto the wrapper `div` verbatim; it depends only on `nodes`, `focusId` (rename the local to `kbFocusId`), and `selectedId`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard && npx vitest run src/pages/command-center/layout-v2 && npm run typecheck && npm run lint`
Expected: PASS and clean.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src
git commit -m "feat(layout-ui): LayoutCanvas with viewport paging, LOD, focus, and multi-project offsets"
```

---

### Task 8: Live refetch, toolbar Tidy and jump-to-result, mobile list, and the flag switch

**Files:**
- Modify: `dashboard/src/pages/command-center/useGraphLive.ts`
- Modify: `dashboard/src/pages/command-center/TaskToolbar.tsx`
- Create: `dashboard/src/pages/command-center/layout-v2/MobileLayoutList.tsx`
- Modify: `dashboard/src/pages/command-center/Graph.tsx`
- Test: `__tests__/useGraphLive.test.tsx` (append), `layout-v2/__tests__/MobileLayoutList.test.tsx`, `__tests__/Graph.integration.test.tsx` (append)

**Interfaces:**
- A module-level registry in `layout-v2/liveRegistry.ts`: `registerLayoutRefetch(projectId, fn): () => void` and `refetchLayout(projectId)`; `ProjectLayer` registers its `refetchVisible` (replacing the `refetchRef` prop from Task 7), and `useGraphLive`'s `schedule` timer additionally calls `refetchLayout(pid)` for each project it refreshes.
- `TaskToolbar` gains a **Tidy layout** button (visible only when `graphLayoutEnabled`), which calls `useTidyLayout(projectId).mutate()` after `window.confirm("Tidy re-arranges every node in this project. Continue?")`, and a **Next result** button that appears when `filters.query` or `filters.status` is set; clicking calls `locate`, cycles an index, fetches `node/{id}` through `useLayoutNode`'s query function, and pans via `fitBounds` on the container box. Wire this through a small `useJumpToResult(projectId, variant, filters)` hook exposing `{ next(), count }`; `LayoutCanvas` subscribes to a `jumpTarget` prop `{ id, x, y, w, h } | null` and fits to it.
- `MobileLayoutList({ projectId, variant, filters, expanded, toggleExpanded, onTaskClick, selectedTaskId })` renders `TaskCard fluid` for each node from `list`, loads the next page when the sentinel at the end intersects (IntersectionObserver), and uses `mergeTiles`-free simple state.
- `Graph.tsx`: `const layoutV2 = useSystemStatus().data?.graph_layout_enabled === true;` If true render `LayoutCanvas` (desktop) or `MobileLayoutList` (mobile), passing `variant = filters.showCompleted ? "all" : "active"`; else the existing components. The status strip shows `node_count` from `useLayoutExtents` instead of `graph.tasks.length` when v2 is on; `useProjectGraphs` is not called when v2 is on.

- [ ] **Step 1: Write the failing tests**

Append to `useGraphLive.test.tsx` (follow the file's existing event-emission helper):
```tsx
it("refetches the layout for the event's project after the coalescing window", async () => {
  const refetch = vi.fn();
  const unregister = registerLayoutRefetch("p1", refetch);
  renderWithStream(["p1"]);           // the file's existing helper that mounts useGraphLive and returns the emitter
  emit({ event_type: "task.updated", project_id: "p1", task_id: "t" });
  await vi.advanceTimersByTimeAsync(600);
  expect(refetch).toHaveBeenCalledTimes(1);
  unregister();
});
```

`MobileLayoutList.test.tsx`:
```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
const list = vi.hoisted(() => vi.fn());
vi.mock("../../../../api/graphLayout", () => ({ fetchList: list }));
import MobileLayoutList from "../MobileLayoutList";

const n = (id: string) => ({ id, title: id, status: "READY", priority: 100, is_blocked: false, x: 0, y: 0, w: 1, h: 1, depth: 0, container_id: null, kind: "card", context_only: false, agg_children: 0, agg_descendants: 0, agg_completed: 0, agg_running: 0, agg_blocked: 0, agg_active: 0 });

describe("MobileLayoutList", () => {
  it("renders the first page and loads more on demand", async () => {
    list.mockResolvedValueOnce({ nodes: [n("a"), n("b")], next_cursor: "c1", layout_version: 1 })
        .mockResolvedValueOnce({ nodes: [n("c")], next_cursor: null, layout_version: 1 });
    render(<MobileLayoutList projectId="p1" variant="active" filters={{ query: "", status: "", showCompleted: false, focus: "" }} expanded={new Set()} toggleExpanded={() => {}} onTaskClick={() => {}} />);
    expect(await screen.findByText("a")).toBeInTheDocument();
    (await screen.findByRole("button", { name: "Load more" })).click();
    expect(await screen.findByText("c")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });
});
```
(Render a visible "Load more" button in addition to the IntersectionObserver sentinel so the behaviour is testable in jsdom.)

Append to `Graph.integration.test.tsx`, mocking `useSystemStatus` to return `{ data: { graph_layout_enabled: true } }` and `LayoutCanvas` to a stub that records its props:
```tsx
it("uses the layout canvas when the flag is on and maps Show completed to the variant", async () => {
  renderGraphAt("/projects/p1/graph?completed=1");   // the file's existing router helper
  await screen.findByTestId("layout-canvas");
  expect(layoutCanvasProps.current).toMatchObject({ projectIds: ["p1"], variant: "all" });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard && npx vitest run src/pages/command-center`
Expected: the three new tests FAIL.

- [ ] **Step 3: Implement**

`liveRegistry.ts`:
```ts
const registry = new Map<string, Set<() => void>>();
export function registerLayoutRefetch(projectId: string, fn: () => void): () => void {
  const set = registry.get(projectId) ?? new Set(); set.add(fn); registry.set(projectId, set);
  return () => { set.delete(fn); if (set.size === 0) registry.delete(projectId); };
}
export function refetchLayout(projectId: string) { for (const fn of registry.get(projectId) ?? []) fn(); }
```
In `useGraphLive.ts`'s timer callback, after `invalidateQueries`, add `refetchLayout(pid);`. In `ProjectLayer`, replace the `refetchRef` effect with `useEffect(() => registerLayoutRefetch(projectId, refetchVisible), [projectId, refetchVisible])`.

`api/graphLayout.ts`: add
```ts
export async function fetchList(projectId: string, body: { variant: Variant; expanded: string[]; q: string; status: string; cursor: string | null; limit: number }): Promise<ListResponse> {
  const r = await postListApiProjectsProjectIdGraphListPost({ client, path: { project_id: projectId }, body, throwOnError: true });
  return r.data as ListResponse;
}
```

`MobileLayoutList.tsx`:
```tsx
import { useEffect, useRef, useState } from "react";
import type { LayoutNode } from "@aq/ts-client";
import { fetchList, type Variant } from "../../../api/graphLayout";
import { TaskCard } from "../TaskNode";
import type { TaskFilters } from "../taskFilters";
import { toFlowElements } from "./flowNodes";
import { emptyStore, mergeTiles } from "./layoutStore";
import type { TaskNodeData } from "../types";

interface Props { projectId: string; variant: Variant; filters: TaskFilters; expanded: ReadonlySet<string>; toggleExpanded: (id: string) => void; onTaskClick: (id: string) => void; selectedTaskId?: string | null }

export default function MobileLayoutList({ projectId, variant, filters, expanded, toggleExpanded, onTaskClick, selectedTaskId }: Props) {
  const [nodes, setNodes] = useState<LayoutNode[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const busy = useRef(false);
  const key = JSON.stringify({ projectId, variant, expanded: [...expanded].sort(), q: filters.query, status: filters.status });

  async function loadPage(after: string | null, reset: boolean) {
    if (busy.current) return; busy.current = true;
    try {
      const page = await fetchList(projectId, { variant, expanded: [...expanded], q: filters.query.trim(), status: filters.status, cursor: after, limit: 50 });
      setNodes((prev) => reset ? page.nodes : [...prev, ...page.nodes]);
      setCursor(page.next_cursor ?? null); setDone(!page.next_cursor);
    } finally { busy.current = false; }
  }
  useEffect(() => { setDone(false); void loadPage(null, true); }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinel.current; if (!el || done || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => { if (e?.isIntersecting) void loadPage(cursor, false); });
    io.observe(el); return () => io.disconnect();
  }, [cursor, done]); // eslint-disable-line react-hooks/exhaustive-deps

  const store = mergeTiles(emptyStore(), [], { nodes, edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 0 } as never);
  const { nodes: flow } = toFlowElements(store, { projectId, offsetY: 0, expanded, handlers: { onOpenTask: onTaskClick, onToggleChildren: toggleExpanded, onFocus: () => {} } });
  return (
    <div role="region" aria-label="Task list" className="h-full space-y-3 overflow-y-auto p-3">
      {flow.map((n) => {
        const node = store.nodes.get(n.id)!;
        const data: TaskNodeData = n.type === "container"
          ? { task: node, gates: [], projectId,
              hierarchy: { parentId: node.container_id ?? null, parentTitle: null, depth: node.depth, childCount: node.agg_children,
                visibleChildCount: node.agg_children, descendantCount: node.agg_descendants, completedCount: node.agg_completed,
                runningCount: node.agg_running, blockedCount: node.agg_blocked, expanded: true, autoExpanded: false, contextOnly: node.context_only },
              onOpenTask: onTaskClick, onToggleChildren: toggleExpanded }
          : (n.data as TaskNodeData);
        return <div key={n.id} style={{ marginLeft: node.depth * 12 }}>
          <TaskCard fluid selected={selectedTaskId === n.id} data={data} />
        </div>;
      })}
      {!done && <button type="button" className="w-full rounded border border-gray-700 py-2 text-xs" onClick={() => void loadPage(cursor, false)}>Load more</button>}
      <div ref={sentinel} />
    </div>
  );
}
```

`TaskToolbar.tsx`: add props `graphLayoutEnabled?: boolean`, `projectId?: string`, `onNextResult?: () => void`, `resultCount?: number`; render `Tidy layout` and `Next result (N)` buttons accordingly. Implement `useJumpToResult` in `layout-v2/useJumpToResult.ts`:
```ts
export function useJumpToResult(projectId: string | undefined, variant: Variant, filters: TaskFilters) {
  const [hits, setHits] = useState<LocateResponse["hits"]>([]); const [i, setI] = useState(-1);
  const [target, setTarget] = useState<LocateResponse["hits"][number] | null>(null);
  const active = !!(filters.query.trim() || filters.status);
  useEffect(() => { setHits([]); setI(-1); setTarget(null); if (!projectId || !active) return;
    void locate(projectId, variant, filters.query.trim(), filters.status).then((r) => setHits(r.hits)); }, [projectId, variant, filters.query, filters.status, active]);
  const next = useCallback(() => { if (hits.length === 0) return; const n = (i + 1) % hits.length; setI(n); setTarget(hits[n]!); }, [hits, i]);
  return { next, count: hits.length, target };
}
```
`LayoutCanvas` accepts `jumpTarget` and, in an effect, `fitBounds` to it (with the project offset) and adds its `container_id` chain to `expanded` is not required: the server already reveals matching descendants while filtering.

`Graph.tsx`: add the flag branch described in Interfaces; `variant = filters.showCompleted || !!focusId ? "all" : "active"`; pass `projectNames` from `projects`; wire `useJumpToResult(projectId, variant, filters)` into the toolbar (the toolbar is rendered by the parent shell; if it lives outside `Graph.tsx`, lift `onNextResult`/`resultCount` through `TaskWorkspace` context instead) and `jumpTarget` into `LayoutCanvas`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard && npm test && npm run typecheck && npm run lint`
Expected: PASS and clean.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src
git commit -m "feat(layout-ui): live refetch, tidy and jump-to-result, mobile list, flag switch"
```

---

### Task 9: Cross-project stubs and manual verification

**Files:**
- Modify: `dashboard/src/pages/command-center/layout-v2/flowNodes.ts`
- Test: `layout-v2/__tests__/flowNodes.test.ts` (append)

- [ ] **Step 1: Test**

```ts
it("labels stubs from other projects with the project name and places them at the container edge", () => {
  const store = mergeTiles(emptyStore(), ["0:0"], { nodes: [n("z", "card", 0, 0)],
    edges: [{ from: "z", to: "peer", dep_type: "blocks", description: null, count: 1 }],
    stubs: [{ id: "peer", project_id: "p2", x: 3, y: 3, w: 1, h: 1, title: "Peer" }],
    stub_overflow: [], workers: [], gates: [], layout_version: 1 } as never);
  const { nodes } = toFlowElements(store, { ...ctx, projectNames: new Map([["p2", "Other"]]) });
  const stub = nodes.find((x) => x.id === "peer")!;
  expect((stub.data as { task: { title: string } }).task.title).toBe("Other · Peer");
});
```

- [ ] **Step 2: Implement**

Add optional `projectNames?: Map<string,string>` to `FlowContext`. In the stub loop, when `s.project_id !== ctx.projectId`, set `title = \`${ctx.projectNames?.get(s.project_id) ?? s.project_id} · ${s.title}\`` and position the stub at `toPx(-1.2, s.y + ctx.offsetY)` (a labeled port at the project's left edge) since its coordinates are in the peer project's frame. Pass `projectNames` from `LayoutCanvas`.

- [ ] **Step 3: Manual verification with the real daemon**

1. Set `dashboard.graph_layout.enabled: true` in `~/.agent-queue/config.yaml`, run `aq graph layout-rebuild --project <id>` for a project, start the daemon and `npm run dev` in `dashboard/`.
2. Open `/projects/<id>/graph`. Confirm: containers render with headers; no parent-child edges; dependency edges route between cards; panning fetches new cells (watch the network tab: one `tiles` request per pan burst); zooming out to 0.2 collapses to top-level containers.
3. Click Focus on an epic: URL gains `?focus=`, view fits the container, breadcrumbs show, completed children are visible, Show completed is disabled.
4. Toggle Show completed off focus: nodes switch layouts without any animation.
5. Complete a task via CLI (`aq task close <id>`): within a few seconds the card disappears from the active layout and the container header counts update.
6. Click Tidy layout, confirm, wait for the job in `extent`, and confirm a full refetch.
7. Record findings in the PR description.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src
git commit -m "feat(layout-ui): cross-project stub labels"
```

---

### Task 10: Stage wrap-up

- [ ] **Step 1:** `cd dashboard && npm test && npm run typecheck && npm run lint && npm run build` → clean.
- [ ] **Step 2:** `pytest tests/ -n auto -q` → PASS.
- [ ] **Step 3:** Update `CLAUDE.md` graph layout line: "Dashboard: `dashboard/src/pages/command-center/layout-v2/` (store, tiles hook, LayoutCanvas), enabled by `dashboard.graph_layout.enabled`; `GraphCanvas` and `layout.ts` grid remain as fallback for one release."
- [ ] **Step 4:** Open a follow-up task in the queue titled "Remove grid layout fallback and legacy /graph endpoint" referencing spec §10 step 3, so the removal is not forgotten.
- [ ] **Step 5:** Commit `docs: document layout dashboard`.

---

## Self-review against the spec

- §6.1 rendering: Tasks 5, 7 (server coordinates, group containers, `onlyRenderVisibleElements`, no transitions).
- §6.2 LOD: Task 2 thresholds, Task 4 budget callback, Task 7 override.
- §6.3 store and cells: Tasks 3, 4 (padding, one in flight, eviction distance, resets on params and version).
- §6.4 live updates: Task 8 (`refetchLayout` per project after the coalescing window).
- §6.5 collapse: expanded set shared via `useExpandedTaskIds`, gaps accepted.
- §6.6 focus: Tasks 6, 7 (URL param, forced `all`, Show completed disabled, `node/{id}` fit and breadcrumbs).
- §6.7 search and filters: server `q`/`status` on tiles (Task 7), jump to next result (Task 8).
- §6.8 all-projects: Task 7 offsets and headers; cross-project stubs Task 9.
- §6.9 mobile: Task 8.
- §7 pending state: Task 7 overlay with the extent poll from Task 4's `useLayoutExtent`.
- §9 dashboard tests: Tasks 3, 4, 5, 6, 7, 8. Mounted-node-count perf assertion (§9) is covered by the client budget of 400 in Task 4 rather than a browser test; the manual check in Task 9 step 2 verifies it live.
- §10 step 3 rollout: Task 8 flag switch; removal deferred by the follow-up task in Task 10.
