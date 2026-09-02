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
