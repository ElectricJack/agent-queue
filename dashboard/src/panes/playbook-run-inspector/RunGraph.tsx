import { useMemo } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

const NODE_WIDTH = 176;
const NODE_HEIGHT = 72;
type JsonMap = Record<string, unknown>;

export interface CompiledRunGraph extends JsonMap {
  nodes?: Record<string, JsonMap>;
  pipeline_rules?: Record<string, unknown>;
}

interface TraceEntry { node_id: string; status?: string }
interface RunNodeData extends Record<string, unknown> {
  label: string;
  detail: string;
  entry: boolean;
  terminal: boolean;
  current: boolean;
  status?: string;
}
type RunNode = Node<RunNodeData, "playbookNode">;

function flatNode(node: JsonMap): JsonMap {
  const action = node.action;
  return action && typeof action === "object" && !Array.isArray(action)
    ? { ...(action as JsonMap), ...node }
    : node;
}

function edgeTarget(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function conditionLabel(transition: JsonMap): string {
  if (transition.otherwise) return "otherwise";
  const when = transition.when;
  if (typeof when === "string") return when;
  return when ? JSON.stringify(when) : "condition";
}

function nodeDetail(node: JsonMap): string {
  const flat = flatNode(node);
  if (typeof flat.command === "string") return flat.command;
  if (typeof node.prompt === "string") {
    const first = node.prompt.trim().split("\n")[0] ?? "";
    return first.length > 48 ? `${first.slice(0, 45)}…` : first;
  }
  return node.terminal ? "terminal" : "action";
}

// Exported for deterministic graph-structure tests.
// eslint-disable-next-line react-refresh/only-export-components
export function buildRunGraph(
  graph: CompiledRunGraph,
  currentNode: string | null | undefined,
  trace: TraceEntry[],
): { nodes: RunNode[]; edges: Edge[] } {
  const sourceNodes = graph.nodes ?? {};
  const traceStatus = new Map(trace.map((entry) => [entry.node_id, entry.status]));
  const edges: Edge[] = [];

  for (const [nodeId, rawNode] of Object.entries(sourceNodes)) {
    const node = flatNode(rawNode);
    const success = edgeTarget(node.on_success);
    const failure = edgeTarget(node.on_failure);
    const goto = edgeTarget(node.goto);
    const timeout = edgeTarget(node.on_timeout);
    if (success) edges.push({ id: `${nodeId}:success:${success}`, source: nodeId, target: success, label: "on_success", type: "smoothstep" });
    if (failure) edges.push({ id: `${nodeId}:failure:${failure}`, source: nodeId, target: failure, label: "on_failure", type: "smoothstep", style: { stroke: "#f87171" }, labelStyle: { fill: "#fca5a5" } });
    if (goto) edges.push({ id: `${nodeId}:goto:${goto}`, source: nodeId, target: goto, label: "goto", type: "smoothstep" });
    if (Array.isArray(node.transitions)) {
      node.transitions.forEach((transition, index) => {
        if (!transition || typeof transition !== "object") return;
        const target = edgeTarget((transition as JsonMap).goto);
        if (!target) return;
        edges.push({ id: `${nodeId}:transition:${index}:${target}`, source: nodeId, target, label: conditionLabel(transition as JsonMap), type: "smoothstep" });
      });
    }
    if (timeout && timeout !== goto) {
      edges.push({ id: `${nodeId}:timeout:${timeout}`, source: nodeId, target: timeout, label: "on_timeout", type: "smoothstep" });
    }
  }

  const layout = new dagre.graphlib.Graph();
  layout.setDefaultEdgeLabel(() => ({}));
  layout.setGraph({ rankdir: "TB", nodesep: 24, ranksep: 54 });
  for (const nodeId of Object.keys(sourceNodes)) layout.setNode(nodeId, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const edge of edges) layout.setEdge(edge.source, edge.target);
  dagre.layout(layout);

  const nodes: RunNode[] = Object.entries(sourceNodes).map(([nodeId, rawNode]) => {
    const point = layout.node(nodeId) ?? { x: NODE_WIDTH / 2, y: NODE_HEIGHT / 2 };
    return {
      id: nodeId,
      type: "playbookNode",
      position: { x: point.x - NODE_WIDTH / 2, y: point.y - NODE_HEIGHT / 2 },
      data: {
        label: nodeId,
        detail: nodeDetail(rawNode),
        entry: rawNode.entry === true,
        terminal: rawNode.terminal === true,
        current: nodeId === currentNode,
        status: traceStatus.get(nodeId),
      },
    };
  });
  return { nodes, edges };
}

function PlaybookNode({ data }: NodeProps<RunNode>) {
  const tone = data.current
    ? "border-amber-400 bg-amber-950/80 ring-2 ring-amber-400/30"
    : data.status === "completed"
      ? "border-emerald-600/70 bg-emerald-950/50"
      : data.status === "failed"
        ? "border-red-500/70 bg-red-950/50"
        : "border-gray-700 bg-gray-900";
  return (
    <div data-testid="playbook-graph-node" id={`playbook-node-${data.label}`} data-current={String(data.current)} className={`relative w-44 rounded-md border px-3 py-2 text-xs text-gray-100 shadow ${tone}`}>
      <Handle type="target" position={Position.Top} />
      <div className="flex items-center gap-1 text-[9px] uppercase tracking-wide text-gray-400">
        {data.entry && <span>Entry</span>}
        {data.current && <span className="text-amber-300">Current</span>}
        {data.terminal && <span>Terminal</span>}
      </div>
      <div className="truncate font-mono font-semibold">{data.label}</div>
      <div className="truncate text-[10px] text-gray-400">{data.detail}</div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { playbookNode: PlaybookNode };
interface RuleRow { event: string; entry: string; when?: unknown }

function rulesOf(graph: CompiledRunGraph): RuleRow[] {
  const rows: RuleRow[] = [];
  for (const [event, value] of Object.entries(graph.pipeline_rules ?? {})) {
    const rules = Array.isArray(value) ? value : [value];
    for (const rule of rules) {
      if (typeof rule === "string") rows.push({ event, entry: rule });
      else if (rule && typeof rule === "object") {
        const meta = rule as JsonMap;
        rows.push({ event, entry: String(meta.entry ?? ""), when: meta.when });
      }
    }
  }
  return rows;
}

export default function RunGraph({ graph, currentNode, trace }: { graph: CompiledRunGraph; currentNode?: string | null; trace: TraceEntry[] }) {
  const built = useMemo(() => buildRunGraph(graph, currentNode, trace), [graph, currentNode, trace]);
  const rules = useMemo(() => rulesOf(graph), [graph]);
  return (
    <section aria-label="Playbook graph" className="space-y-2 border-b border-gray-800 p-3">
      {rules.length > 0 && (
        <div data-testid="playbook-rules" className="space-y-1 rounded border border-gray-800 bg-gray-950/70 p-2 text-[10px] text-gray-400">
          <div className="font-medium uppercase tracking-wide text-gray-500">Rules</div>
          {rules.map((rule, index) => (
            <div key={`${rule.event}:${rule.entry}:${index}`}>
              <span className="text-indigo-300">{rule.event}</span><span> → {rule.entry}</span>
              {rule.when !== undefined && <span className="ml-1 font-mono">when {JSON.stringify(rule.when)}</span>}
            </div>
          ))}
        </div>
      )}
      <div className="h-64 rounded border border-gray-800 bg-gray-950" data-testid="playbook-run-graph">
        <ReactFlowProvider>
          <ReactFlow nodes={built.nodes} edges={built.edges} nodeTypes={nodeTypes} fitView nodesDraggable={false} nodesConnectable={false} elementsSelectable={false} proOptions={{ hideAttribution: true }}>
            <Background gap={20} color="#1f2937" />
            <Controls showInteractive={false} />
          </ReactFlow>
        </ReactFlowProvider>
      </div>
      {built.edges.length > 0 && (
        <div data-testid="playbook-edge-legend" className="flex flex-wrap gap-2 text-[10px] text-gray-500">
          {[...new Set(built.edges.map((edge) => String(edge.label ?? "edge")))].map((label) => <span key={label}>{label}</span>)}
        </div>
      )}
    </section>
  );
}
