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
    expect(byId.e!.type).toBe("container");
    expect(byId.e!.position).toEqual({ x: 0, y: 0 });
    expect(byId.e!.width).toBe(720); expect(byId.e!.height).toBe(312);
    expect(byId.c!.type).toBe("task");
    expect((byId.c!.data as { hierarchy: { expanded: boolean; descendantCount: number } }).hierarchy).toMatchObject({ expanded: false, descendantCount: 3 });
    expect(byId.z!.position).toEqual({ x: 480, y: 624 });
    expect(edges).toHaveLength(1);
    expect(edges[0]).toMatchObject({ source: "c", target: "z", label: "×2", sourceHandle: "out-right", targetHandle: "in-left" });
  });
  it("keeps a wrapped serial chain in two rows and routes the reverse row through left handles", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [
        n("a", "card", 0, 0), n("b", "card", 1.15, 0), n("c", "card", 2.3, 0), n("d", "card", 3.45, 0),
        n("e", "card", 3.5, 1.22), n("f", "card", 2.35, 1.22),
      ],
      edges: [
        { from: "b", to: "a", dep_type: "blocks", description: null, count: 1 },
        { from: "c", to: "b", dep_type: "blocks", description: null, count: 1 },
        { from: "d", to: "c", dep_type: "blocks", description: null, count: 1 },
        { from: "e", to: "d", dep_type: "blocks", description: null, count: 1 },
        { from: "f", to: "e", dep_type: "blocks", description: null, count: 1 },
      ], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const { nodes, edges } = toFlowElements(store, ctx);
    expect(nodes.find((node) => node.id === "e")!.position.y).toBeGreaterThan(nodes.find((node) => node.id === "d")!.position.y);
    expect(edges).toHaveLength(5);
    expect(edges.find((edge) => edge.source === "d" && edge.target === "e")).toMatchObject({ sourceHandle: "out-bottom", targetHandle: "in-top" });
    expect(edges.find((edge) => edge.source === "e" && edge.target === "f")).toMatchObject({ sourceHandle: "out-left", targetHandle: "in-right" });
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
  it("maps gates onto the cards whose task_ids include the node id", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("z", "card", 0, 0)],
      edges: [],
      stubs: [], stub_overflow: [], workers: [],
      gates: [{ id: "g1", task_ids: ["z"], status: "open", gate_type: "approval" }],
      layout_version: 1,
    } as never);
    const { nodes } = toFlowElements(store, ctx);
    const z = nodes.find((x) => x.id === "z");
    expect((z?.data as { gates: unknown[] }).gates).toMatchObject([{ id: "g1" }]);
  });
  it("labels stubs from other projects with the project name and docks them at the left edge", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], { nodes: [n("z", "card", 0, 0)],
      edges: [{ from: "z", to: "peer", dep_type: "blocks", description: null, count: 1 }],
      stubs: [{ id: "peer", project_id: "p2", x: 3, y: 3, w: 1, h: 1, title: "Peer" }],
      stub_overflow: [], workers: [], gates: [], layout_version: 1 } as never);
    const { nodes } = toFlowElements(store, { ...ctx, projectNames: new Map([["p2", "Other"]]) });
    const stub = nodes.find((x) => x.id === "peer")!;
    expect((stub.data as { task: { title: string } }).task.title).toBe("Other · Peer");
    expect(stub.position.x).toBe(-1.2 * 240);
  });
  it("falls back to the project id when the name is unknown and leaves same-project stubs in place", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], { nodes: [n("z", "card", 0, 0)],
      edges: [],
      stubs: [{ id: "peer", project_id: "p2", x: 3, y: 3, w: 1, h: 1, title: "Peer" },
              { id: "mine", project_id: "p1", x: 4, y: 2, w: 1, h: 1, title: "Mine" }],
      stub_overflow: [], workers: [], gates: [], layout_version: 1 } as never);
    const { nodes } = toFlowElements(store, ctx);
    const peer = nodes.find((x) => x.id === "peer")!;
    const mine = nodes.find((x) => x.id === "mine")!;
    expect((peer.data as { task: { title: string } }).task.title).toBe("p2 · Peer");
    expect((mine.data as { task: { title: string } }).task.title).toBe("Mine");
    expect(mine.position.x).toBe(4 * 240);
  });
  it('renders one non-interactive "+N more" marker per stub_overflow entry, beside its anchor', () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("z", "card", 2, 1)],
      edges: [], stubs: [],
      stub_overflow: [{ node_id: "z", direction: "out", more: 7 },
                      { node_id: "z", direction: "in", more: 2 }],
      workers: [], gates: [], layout_version: 1,
    } as never);
    const { nodes } = toFlowElements(store, ctx);
    const out = nodes.find((x) => x.id === "overflow:z|out")!;
    const incoming = nodes.find((x) => x.id === "overflow:z|in")!;
    // Its own type: a marker is not a task, so nothing may open it.
    expect(out.type).toBe("overflowMarker");
    expect(incoming.type).toBe("overflowMarker");
    expect((out.data as { label: string }).label).toBe("+7 more");
    expect((incoming.data as { label: string }).label).toBe("+2 more");
    // Sized as a pill and parked outside the card's edges, never under it.
    expect(out.width).toBe(0.4 * 240);
    expect(out.height).toBeCloseTo(0.3 * 156);
    expect(out.position.x).toBeCloseTo((2 + 1 + 0.05) * 240);
    expect(incoming.position.x).toBeCloseTo((2 - 0.45) * 240);
    // Vertically centred on the anchor, and above every card.
    expect(out.position.y).toBeCloseTo((1 + 0.5 - 0.15) * 156);
    expect(out.zIndex).toBe(200);
    expect(out.selectable).toBe(false);
  });

  it("hands back the same node and edge objects for everything a re-delivery did not change", () => {
    const tiles = {
      nodes: [n("a", "card", 0, 0), n("b", "card", 2, 0), n("c", "container", 0, 4, { w: 3, h: 2 })],
      edges: [{ from: "b", to: "a", dep_type: "blocks", description: null, count: 1 }],
      stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    };
    // A refetch parses fresh objects off the wire, so nothing can be shared
    // by accident: only the content signature can make an element reusable.
    const wire = () => mergeTiles(emptyStore(), ["0:0"], JSON.parse(JSON.stringify(tiles)) as never);
    const first = toFlowElements(wire(), ctx);
    const again = toFlowElements(wire(), ctx, first.cache);
    const byId = (r: { nodes: { id: string }[] }, id: string) => r.nodes.find((x) => x.id === id)!;
    expect(byId(again, "a")).toBe(byId(first, "a"));
    expect(byId(again, "b")).toBe(byId(first, "b"));
    expect(byId(again, "c")).toBe(byId(first, "c"));
    expect(again.edges[0]).toBe(first.edges[0]);

    // One task changes: only that node is rebuilt.
    const changed = { ...tiles, nodes: [n("a", "card", 0, 0, { status: "COMPLETED" }), tiles.nodes[1]!, tiles.nodes[2]!] };
    const third = toFlowElements(
      mergeTiles(emptyStore(), ["0:0"], JSON.parse(JSON.stringify(changed)) as never), ctx, again.cache);
    expect(byId(third, "a")).not.toBe(byId(again, "a"));
    expect(byId(third, "b")).toBe(byId(again, "b"));
    expect(byId(third, "c")).toBe(byId(again, "c"));
    expect(third.edges[0]).toBe(again.edges[0]);
  });

  it("rebuilds everything when the surrounding context changes", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("a", "card", 0, 0)], edges: [], stubs: [], stub_overflow: [],
      workers: [], gates: [], layout_version: 1,
    } as never);
    const first = toFlowElements(store, ctx);
    const moved = toFlowElements(store, { ...ctx, offsetY: 4 }, first.cache);
    expect(moved.nodes[0]).not.toBe(first.nodes[0]);
    expect(moved.nodes[0]!.position.y).toBeCloseTo(4 * 156);
  });

  it("drops to straight unlabelled edges at far zoom", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("a", "card", 0, 0), n("b", "card", 2, 0)],
      edges: [{ from: "b", to: "a", dep_type: "blocks", description: null, count: 3 }],
      stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    expect(toFlowElements(store, ctx).edges[0]).toMatchObject({ type: "smoothstep", label: "\u00d73" });
    const far = toFlowElements(store, { ...ctx, simpleEdges: true }).edges[0]!;
    expect(far.type).toBe("straight");
    expect(far.label).toBeUndefined();
  });
});
