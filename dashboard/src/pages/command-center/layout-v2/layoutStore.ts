import type { GraphGate, LayoutEdge, LayoutNode, LayoutStub, LayoutWorker, StubOverflow, TilesResponse } from "@aq/ts-client";
import { CELL, cellDistance, parseCell, type CellKey } from "./units";

export interface LayoutStore {
  version: number | null;
  nodes: Map<string, LayoutNode>;
  edges: Map<string, LayoutEdge>;
  stubs: Map<string, LayoutStub>;
  edgeCells: Map<string, Set<CellKey>>;
  workers: LayoutWorker[];
  gates: GraphGate[];
  /** "+N more" boundary markers, keyed `${node_id}|${direction}`. */
  stubOverflow: Map<string, StubOverflow>;
  cells: Map<CellKey, Set<string>>;
  loaded: Set<CellKey>;
  /**
   * The response covered the whole graph (a `root` request ignores the rect
   * server-side), so no cell can be missing and nothing may be evicted.
   */
  whole: boolean;
}

export const emptyStore = (): LayoutStore => ({
  version: null, nodes: new Map(), edges: new Map(), stubs: new Map(), edgeCells: new Map(),
  workers: [], gates: [], stubOverflow: new Map(), cells: new Map(), loaded: new Set(), whole: false,
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
    edgeCells: new Map([...base.edgeCells].map(([k, v]) => [k, new Set(v)])),
    // Each response only describes the cells it was asked for, so replacing
    // these wholesale would drop the avatars and gate badges of every other
    // loaded cell. Merge by identity and prune against the node set below.
    workers: base.workers, gates: base.gates,
    stubOverflow: new Map(base.stubOverflow),
    cells: new Map([...base.cells].map(([k, v]) => [k, new Set(v)])),
    loaded: new Set(base.loaded),
    whole: base.whole,
  };
  for (const c of cells) { next.loaded.add(c); if (!next.cells.has(c)) next.cells.set(c, new Set()); }
  for (const n of res.nodes ?? []) {
    next.nodes.set(n.id, n);
    for (const c of cells) if (intersectsCell(n, c)) next.cells.get(c)!.add(n.id);
  }
  for (const s of res.stubs ?? []) next.stubs.set(s.id, s);
  for (const o of res.stub_overflow ?? []) next.stubOverflow.set(`${o.node_id}|${o.direction}`, o);
  for (const e of res.edges ?? []) {
    const k = edgeKey(e);
    next.edges.set(k, e);
    const owners = new Set(next.edgeCells.get(k) ?? []);
    for (const c of cells) owners.add(c);
    next.edgeCells.set(k, owners);
  }
  return pruneAnnotations(next, res.workers ?? [], res.gates ?? []);
}

const gateKey = (g: GraphGate) => g.id;

/**
 * Folds `workers`/`gates` from one response into the store by identity and
 * drops every annotation whose anchor is no longer a known node — a docked
 * agent or a gate badge must not outlive the card it points at.
 */
function pruneAnnotations(store: LayoutStore, workers: LayoutWorker[], gates: GraphGate[]): LayoutStore {
  const byAgent = new Map(store.workers.map((w) => [w.agent_id, w]));
  for (const w of workers) byAgent.set(w.agent_id, w);
  const byGate = new Map(store.gates.map((g) => [gateKey(g), g]));
  for (const g of gates) byGate.set(gateKey(g), g);
  return {
    ...store,
    workers: [...byAgent.values()].filter((w) => store.nodes.has(w.docked_at)),
    gates: [...byGate.values()].filter((g) => (g.task_ids ?? []).some((id) => store.nodes.has(id))),
    stubOverflow: new Map([...store.stubOverflow].filter(([, o]) => store.nodes.has(o.node_id))),
  };
}

export function missingCells(store: LayoutStore, wanted: CellKey[]): CellKey[] {
  if (store.whole) return [];
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
  return pruneAnnotations({ ...store, cells, loaded, nodes, edges, edgeCells, stubs }, [], []);
}
