/**
 * How many node components React Flow re-renders when the tiled layout is
 * re-delivered — which is what a live `task.*` burst does to the canvas
 * roughly twice a second while agents are working.
 *
 * `NodeWrapper` is memoised and subscribes to its internal node with a
 * shallow comparator, and `adoptUserNodes` reuses the internal node whenever
 * the user node object is `===` the previous one. So the count below is
 * exactly "how many cards paid for this update".
 *
 * The numbers are printed for the PR; the assertions are the invariants.
 */
import { useState, type ComponentProps } from "react";
import { act } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ReactFlow, ReactFlowProvider, type Node, type NodeProps } from "@xyflow/react";
import TaskNode from "../../TaskNode";
import ContainerNode from "../ContainerNode";
import { emptyStore, mergeTiles, type LayoutStore } from "../layoutStore";
import { toFlowElements, type FlowHandlers } from "../flowNodes";

afterEach(cleanup);

const COUNT = 200;
const handlers: FlowHandlers = { onOpenTask: () => {}, onToggleChildren: () => {}, onFocus: () => {} };
const ctx = { projectId: "p1", offsetY: 0, expanded: new Set<string>(), handlers };

/** A project whose graph is mostly finished work, as the operator described. */
function payload(mutate?: (node: Record<string, unknown>) => void) {
  const nodes: Record<string, unknown>[] = [];
  for (let i = 0; i < COUNT; i++) {
    const node = {
      id: `t${i}`, title: `Task ${i}`, status: i % 4 === 0 ? "READY" : "COMPLETED", priority: 100,
      is_blocked: false, x: i % 8, y: Math.floor(i / 8), w: 1, h: 1, depth: 0, container_id: null,
      kind: "card", context_only: false, agg_children: 0, agg_descendants: 4, agg_completed: 4,
      agg_running: 0, agg_blocked: 0, agg_active: 0, profile_id: "worker", intelligence_class: null,
      assigned_agent_id: null, branch_name: null, pr_url: null, playbook_run_id: null,
    };
    if (i === 0) mutate?.(node);
    nodes.push(node);
  }
  return { nodes, edges: [], stubs: [], stub_overflow: [], workers: [], gates: [], layout_version: 1 };
}

/** A fresh store from a fresh parse, exactly as a refetch off the wire produces. */
const storeFrom = (p: unknown): LayoutStore =>
  mergeTiles(emptyStore(), ["0:0"], JSON.parse(JSON.stringify(p)) as never);

function harness(initial: Node[]) {
  let renders = 0;
  let setNodes: (n: Node[]) => void = () => {};
  const Counting = (props: NodeProps) => {
    renders++;
    return <TaskNode {...(props as ComponentProps<typeof TaskNode>)} />;
  };
  const nodeTypes = { task: Counting, container: ContainerNode };
  function Harness() {
    const [nodes, set] = useState(initial);
    setNodes = set;
    return <ReactFlowProvider>
      <div style={{ width: 1200, height: 800 }}>
        <ReactFlow nodes={nodes} edges={[]} nodeTypes={nodeTypes} />
      </div>
    </ReactFlowProvider>;
  }
  render(<Harness />);
  const mounted = renders;
  return {
    mounted,
    /** Cards re-rendered by pushing `next`. */
    push(next: Node[]) { renders = 0; act(() => setNodes(next)); return renders; },
  };
}

describe("canvas render cost", () => {
  it("re-renders only the cards that changed when the layout is re-delivered", () => {
    const flat = payload();
    const first = toFlowElements(storeFrom(flat), ctx);
    const h = harness(first.nodes);

    // A live refetch that changed nothing: identical content, new objects.
    const same = toFlowElements(storeFrom(flat), ctx, first.cache);
    const unchangedRenders = h.push(same.nodes);

    // One task moved to IN_PROGRESS; nothing else changed.
    const oneChanged = toFlowElements(
      storeFrom(payload((task) => { task.status = "IN_PROGRESS"; })), ctx, same.cache);
    const oneRenders = h.push(oneChanged.nodes);

    console.log(`[perf] nodes=${COUNT} mount=${h.mounted} unchanged-refetch=${unchangedRenders} one-task-changed=${oneRenders}`);
    expect(unchangedRenders).toBe(0);
    expect(oneRenders).toBe(1);
  });

  it("keeps the conversion itself off the critical path", () => {
    const flat = payload();
    const store = storeFrom(flat);
    let cache = toFlowElements(store, ctx).cache;
    const started = performance.now();
    const passes = 50;
    for (let i = 0; i < passes; i++) cache = toFlowElements(storeFrom(flat), ctx, cache).cache;
    const perPass = (performance.now() - started) / passes;
    console.log(`[perf] toFlowElements ${perPass.toFixed(2)}ms per re-delivery of ${COUNT} nodes`);
    // A whole 16ms frame budget for one conversion would defeat the point.
    expect(perPass).toBeLessThan(16);
  });
});
