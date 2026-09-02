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
   * Nodes carried over from the previous expanded set, still drawn at their
   * old positions until this generation re-delivers them. They are what the
   * reflow animates FROM: emptying the store on a toggle would unmount every
   * card and the move would be a jump-cut instead.
   */
  carried: Set<string>;
  /**
   * The response covered the whole graph (a `root` request ignores the rect
   * server-side), so no cell can be missing and nothing may be evicted.
   */
  whole: boolean;
}

export const emptyStore = (): LayoutStore => ({
  version: null, nodes: new Map(), edges: new Map(), stubs: new Map(), edgeCells: new Map(),
  workers: [], gates: [], stubOverflow: new Map(), cells: new Map(), loaded: new Set(),
  carried: new Set(), whole: false,
});

/**
 * Start a fresh generation (a new expanded set, filter or variant) while
 * keeping the drawn nodes on screen. Cell bookkeeping is cleared, so every
 * visible cell is refetched; the retained nodes are marked `carried` and are
 * dropped by `dropCarried` once the new generation has fully landed.
 */
export function retainForReflow(store: LayoutStore): LayoutStore {
  return {
    ...emptyStore(),
    nodes: new Map(store.nodes),
    edges: new Map(store.edges),
    stubs: new Map(store.stubs),
    workers: store.workers,
    gates: store.gates,
    stubOverflow: new Map(store.stubOverflow),
    carried: new Set(store.nodes.keys()),
  };
}

/** Drop the carried nodes this generation never re-delivered. */
export function dropCarried(store: LayoutStore): LayoutStore {
  if (store.carried.size === 0) return store;
  const nodes = new Map([...store.nodes].filter(([id]) => !store.carried.has(id)));
  return pruneAnnotations({ ...store, nodes, carried: new Set() }, [], []);
}

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
    carried: new Set(base.carried),
    whole: base.whole,
  };
  for (const c of cells) { next.loaded.add(c); if (!next.cells.has(c)) next.cells.set(c, new Set()); }
  for (const n of res.nodes ?? []) {
    next.nodes.set(n.id, n);
    next.carried.delete(n.id);
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

/**
 * How many nodes THIS generation has drawn. Nodes carried over from the
 * previous expanded set do not count: they are leftovers on their way out,
 * and counting them could trip the client's node budget mid-toggle and step
 * the level of detail down for a population that never existed.
 */
export const nodeCount = (store: LayoutStore) => store.nodes.size - store.carried.size;

export function evictFar(store: LayoutStore, keep: CellKey[], maxDistance = 3): LayoutStore {
  const near = (c: CellKey) => keep.some((k) => cellDistance(c, k) <= maxDistance);
  const cells = new Map<CellKey, Set<string>>();
  const loaded = new Set<CellKey>();
  for (const [c, ids] of store.cells) if (near(c)) { cells.set(c, ids); loaded.add(c); }
  const referenced = new Set<string>();
  for (const ids of cells.values()) for (const id of ids) referenced.add(id);
  // A carried node belongs to no cell of this generation yet; evicting it
  // would undo the retention the reflow animation depends on.
  const nodes = new Map([...store.nodes].filter(([id]) => referenced.has(id) || store.carried.has(id)));
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
