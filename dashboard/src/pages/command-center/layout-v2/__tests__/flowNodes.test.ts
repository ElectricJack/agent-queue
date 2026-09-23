import { describe, expect, it } from "vitest";
import { emptyStore, mergeTiles } from "../layoutStore";
import { enteredBounds, enteredFrame, toFlowElements } from "../flowNodes";

const n = (id: string, kind: string, x: number, y: number, extra = {}) => ({
  id, title: id, status: "READY", priority: 100, is_blocked: false, x, y, w: 1, h: 1, depth: 0,
  container_id: null, kind, context_only: false,
  agg_children: 2, agg_descendants: 3, agg_completed: 1, agg_running: 0, agg_blocked: 0, agg_active: 2, ...extra,
});
const ctx = { projectId: "p1", offsetY: 0, focusId: null, handlers: { onOpenTask: () => {}, onFocus: () => {} } };

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
    expect((byId.c!.data as { hierarchy: { descendantCount: number } }).hierarchy).toMatchObject({ descendantCount: 3 });
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

  it("renders a discovered-from provenance edge dashed, dimmed and without an arrowhead", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("a", "card", 0, 0), n("b", "card", 2, 0)],
      edges: [
        { from: "b", to: "a", dep_type: "blocks", description: null, count: 1 },
        { from: "a", to: "b", dep_type: "discovered-from", description: null, count: 1 },
      ],
      stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const { edges } = toFlowElements(store, ctx);
    const blocks = edges.find((e) => (e.data as { depType: string }).depType === "blocks")!;
    const discovered = edges.find((e) => (e.data as { depType: string }).depType === "discovered-from")!;
    expect(blocks.markerEnd).toBeDefined();
    expect(discovered.markerEnd).toBeUndefined();
    expect(discovered.style).toMatchObject({ strokeDasharray: "2 4" });
  });

  it("carries subtask counts in the card payload, defaulting to zero", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("z", "card", 0, 0, { subtasks_total: 3, subtasks_settled: 1 }), n("y", "card", 1, 0)],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const { nodes } = toFlowElements(store, ctx);
    const z = nodes.find((x) => x.id === "z")!;
    const y = nodes.find((x) => x.id === "y")!;
    expect((z.data as { subtasks: { total: number; settled: number } }).subtasks).toEqual({ total: 3, settled: 1 });
    expect((y.data as { subtasks: { total: number; settled: number } }).subtasks).toEqual({ total: 0, settled: 0 });
  });

  it("rebuilds a card when only its subtask counts change", () => {
    const tiles = {
      nodes: [n("a", "card", 0, 0, { subtasks_total: 2, subtasks_settled: 0 })],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    };
    const wire = () => mergeTiles(emptyStore(), ["0:0"], JSON.parse(JSON.stringify(tiles)) as never);
    const first = toFlowElements(wire(), ctx);
    const changed = { ...tiles, nodes: [n("a", "card", 0, 0, { subtasks_total: 2, subtasks_settled: 1 })] };
    const second = toFlowElements(
      mergeTiles(emptyStore(), ["0:0"], JSON.parse(JSON.stringify(changed)) as never), ctx, first.cache);
    const byId = (r: { nodes: { id: string }[] }, id: string) => r.nodes.find((x) => x.id === id)!;
    expect(byId(second, "a")).not.toBe(byId(first, "a"));
  });

  it("carries phase order/label in the card payload, null when absent", () => {
    const store = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("e", "container", 0, 0, { phase_order: 2, phase_label: "Build" }), n("z", "card", 1, 0)],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const { nodes } = toFlowElements(store, ctx);
    const e = nodes.find((x) => x.id === "e")!;
    const z = nodes.find((x) => x.id === "z")!;
    expect((e.data as { node: { phase_order: number | null } }).node.phase_order).toBe(2);
    expect((z.data as { phase: { order: number; label: string } | null }).phase).toBeNull();
  });

  it("rebuilds a card when only its phase fields change", () => {
    const tiles = {
      nodes: [n("a", "card", 0, 0, { phase_order: 1, phase_label: "Foundation" })],
      edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    };
    const wire = () => mergeTiles(emptyStore(), ["0:0"], JSON.parse(JSON.stringify(tiles)) as never);
    const first = toFlowElements(wire(), ctx);
    const changed = { ...tiles, nodes: [n("a", "card", 0, 0, { phase_order: 1, phase_label: "Renamed" })] };
    const second = toFlowElements(
      mergeTiles(emptyStore(), ["0:0"], JSON.parse(JSON.stringify(changed)) as never), ctx, first.cache);
    const byId = (r: { nodes: { id: string }[] }, id: string) => r.nodes.find((x) => x.id === id)!;
    expect(byId(second, "a")).not.toBe(byId(first, "a"));
  });

  it("keeps every edge's type, style and arrowhead through the full zoom range", () => {
    const root = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [n("epic", "collapsed", 0, 0), n("peer", "card", 3, 0), n("other", "card", 6, 0)],
      edges: [
        { from: "peer", to: "epic", dep_type: "blocks", description: null, count: 3 },
        { from: "other", to: "peer", dep_type: "waits-for", description: null, count: 1 },
      ],
      stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const inside = mergeTiles(emptyStore(), ["0:0"], {
      nodes: [
        n("epic", "container", 0, 0, { w: 5, h: 4 }),
        n("child-a", "card", 0.2, 0.5, { container_id: "epic", depth: 1 }),
        n("child-b", "card", 2, 0.5, { container_id: "epic", depth: 1 }),
        n("child-c", "card", 3.5, 2, { container_id: "epic", depth: 1 }),
      ],
      edges: [
        { from: "child-b", to: "child-a", dep_type: "parent-child", description: null, count: 2 },
        { from: "child-c", to: "child-b", dep_type: "conditional-blocks", description: null, count: 1 },
        { from: "child-c", to: "child-a", dep_type: "discovered-from", description: null, count: 1 },
      ],
      stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const appearance = (edges: ReturnType<typeof toFlowElements>["edges"]) => edges.map((edge) => ({
      id: edge.id, source: edge.source, target: edge.target, type: edge.type,
      style: edge.style, markerEnd: edge.markerEnd, depType: edge.data?.depType,
    }));

    // Both scopes use the same conversion path. The switch below 0.5 may
    // remove count labels, but it must never reclassify or restyle an edge.
    for (const [store, scope] of [[root, ctx], [inside, { ...ctx, focusId: "epic" }]] as const) {
      let result = toFlowElements(store, scope);
      const expected = appearance(result.edges);
      expect(result.edges.some((edge) => edge.label === "×3" || edge.label === "×2")).toBe(true);
      for (const zoom of [2, 1, 0.8, 0.5, 0.49, 0.2, 0.15, 0.49, 0.5, 1, 2]) {
        result = toFlowElements(store, { ...scope, hideEdgeLabels: zoom < 0.5 }, result.cache);
        expect(appearance(result.edges)).toEqual(expected);
        if (zoom < 0.5) expect(result.edges.every((edge) => edge.label === undefined)).toBe(true);
      }
      expect(result.edges.some((edge) => edge.label === "×3" || edge.label === "×2")).toBe(true);
    }
  });
  describe("inside an entered container", () => {
    // The daemon's real answer for smart-meadow: a 3x12 box whose six
    // children end at y=6.33, and a boundary stub at its ROOT-layout position.
    const entered = () => mergeTiles(emptyStore(), ["0:0"], {
      nodes: [
        n("box", "container", 0, 0, { w: 3, h: 12, agg_children: 6 }),
        n("c1", "card", 0.1, 0.45, { container_id: "box", depth: 1 }),
        n("c2", "card", 1.25, 2.89, { container_id: "box", depth: 1 }),
        n("c3", "card", 0.1, 5.33, { container_id: "box", depth: 1 }),
      ],
      edges: [{ from: "c3", to: "far", dep_type: "blocks", description: null, count: 1 }],
      stubs: [{ id: "far", project_id: "p1", x: 6.15, y: 12.22, w: 1, h: 1, title: "Far" },
              { id: "near", project_id: "p1", x: 4, y: 1, w: 1, h: 1, title: "Near" }],
      stub_overflow: [], workers: [], gates: [], layout_version: 1,
    } as never);
    const inside = { ...ctx, focusId: "box" };

    it("crops the frame to the children it holds", () => {
      const frame = enteredFrame(entered(), "box")!;
      expect(frame.x).toBe(0); expect(frame.y).toBe(0);
      expect(frame.w).toBeCloseTo(2.25 + 0.15);
      expect(frame.h).toBeCloseTo(6.33 + 0.15);
      const box = toFlowElements(entered(), inside).nodes.find((x) => x.id === "box")!;
      expect(box.height).toBeCloseTo((6.33 + 0.15) * 312 / 2);
    });
    it("never grows the frame past its persisted box", () => {
      const store = mergeTiles(emptyStore(), ["0:0"], {
        nodes: [n("box", "container", 0, 0, { w: 1.2, h: 1.3 }), n("c", "card", 0.1, 0.25, { container_id: "box" })],
        edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1,
      } as never);
      expect(enteredFrame(store, "box")).toEqual({ x: 0, y: 0, w: 1.2, h: 1.3 });
    });
    it("docks same-project stubs beside the frame in reading order, and edges follow", () => {
      const { nodes, edges } = toFlowElements(entered(), inside);
      const near = nodes.find((x) => x.id === "near")!, far = nodes.find((x) => x.id === "far")!;
      expect(near.position.x).toBeCloseTo((2.4 + 0.6) * 240);
      expect(near.position.y).toBe(0);
      expect(far.position.y).toBeCloseTo(1.2 * 156);
      expect(edges).toHaveLength(1);
    });
    it("fits to the cropped frame and the docked stubs, never the root-layout position", () => {
      const bounds = enteredBounds(entered(), "box", "p1")!;
      expect(bounds.x).toBe(0); expect(bounds.y).toBe(0);
      expect(bounds.w).toBeCloseTo(2.4 + 0.6 + 1);
      expect(bounds.h).toBeCloseTo(6.48);
    });
    it("has no frame until the entered scope has delivered the container", () => {
      const store = entered();
      expect(enteredFrame({ ...store, carried: new Set(["box"]) }, "box")).toBeNull();
      expect(enteredFrame(store, null)).toBeNull();
      expect(enteredFrame(store, "missing")).toBeNull();
    });
    it("leaves stubs where they are outside a container", () => {
      const far = toFlowElements(entered(), ctx).nodes.find((x) => x.id === "far")!;
      expect(far.position).toEqual({ x: 6.15 * 240, y: 12.22 * 156 });
    });
  });
});
