import { useId } from "react";
import type { RecordEdge, RecordNode } from "../../api/records";
import { KindBadge } from "../knowledge/KnowledgeBadges";

export interface RecordGraphProps {
  nodes: RecordNode[];
  edges: RecordEdge[];
  selectedId?: string | null;
  onSelect: (node: RecordNode, revisionId: string | null) => void;
}

/** Separate record projection with deterministic columns, independent of task layout. */
export default function RecordGraph({ nodes, edges, selectedId, onSelect }: RecordGraphProps) {
  const id = useId().replace(/:/g, "");
  const counters = { task: 0, knowledge: 0 };
  const positions = new Map(nodes.map((node) => [node.recordId, {
    x: node.kind === "task" ? 20 : 440, y: 20 + counters[node.kind]++ * 120,
  }]));
  const height = Math.max(150, 40 + Math.max(counters.task, counters.knowledge) * 120);
  const visibleEdges = [...new Map(edges.filter((edge) => positions.has(edge.source_record_id)
    && positions.has(edge.target_record_id)).map((edge) => [edge.edge_id, edge])).values()];
  const byId = new Map(nodes.map((node) => [node.recordId, node]));
  if (nodes.length === 0) return <p>No records match these filters.</p>;
  return <section aria-label="Record graph" className="space-y-3">
    <ul aria-label="Graph legend" className="flex flex-wrap gap-4 text-xs text-gray-300">
      <li>Task nodes: work</li><li>Knowledge nodes: evidence</li>
      <li>Execution: task dependency, solid arrow</li>
      <li>Informational: record relationship, dashed arrow</li>
    </ul>
    <p className="text-xs text-gray-500">Links between loaded records. Search and load more to expand the view.</p>
    <div className="overflow-x-auto" data-allow-overflow-x>
      <div className="relative" style={{ width: 860, height }}>
        <svg aria-hidden="true" width="860" height={height} className="absolute inset-0">
          <defs><marker id={`${id}-arrow`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
          </marker></defs>
          {visibleEdges.map((edge) => {
            const source = positions.get(edge.source_record_id)!;
            const target = positions.get(edge.target_record_id)!;
            const sameColumn = source.x === target.x;
            const x = source.x + 300;
            return <path key={edge.edge_id} data-edge-domain={edge.domain}
              d={sameColumn ? `M ${x} ${source.y + 40} C ${x + 80} ${source.y + 40}, ${x + 80} ${target.y + 40}, ${x} ${target.y + 40}`
                : `M ${source.x + (source.x < target.x ? 300 : 0)} ${source.y + 40} L ${target.x + (source.x < target.x ? 0 : 300)} ${target.y + 40}`}
              fill="none" stroke={edge.domain === "execution" ? "#38bdf8" : "#c4b5fd"}
              strokeWidth="2" strokeDasharray={edge.domain === "informational" ? "6 5" : undefined}
              markerEnd={`url(#${id}-arrow)`} />;
          })}
        </svg>
        {nodes.map((node) => {
          const position = positions.get(node.recordId)!;
          return <button key={node.recordId} type="button" aria-pressed={selectedId === node.recordId}
            onClick={() => onSelect(node, null)} data-record-kind={node.kind} data-record-row={node.recordId}
            className={`absolute flex w-[300px] flex-col gap-2 border p-3 text-left text-sm hover:border-indigo-400 ${selectedId === node.recordId ? "border-indigo-400 bg-indigo-500/15" : `bg-gray-900 ${node.kind === "task" ? "border-sky-700" : "border-violet-700"}`} ${node.kind === "task" ? "rounded" : "rounded-2xl"}`}
            style={{ left: position.x, top: position.y }}>
            <span className="truncate">{node.title}</span><span className="flex items-center gap-2 text-xs text-gray-400">
              <KindBadge kind={node.kind} />{node.kind === "task" ? node.status : `${node.lifecycle} · ${node.verification}`}
            </span>
          </button>;
        })}
      </div>
    </div>
    <ul aria-label="Record relationships" className="space-y-1 text-xs text-gray-300">
      {visibleEdges.map((edge) => {
        const source = byId.get(edge.source_record_id)!;
        const target = byId.get(edge.target_record_id)!;
        return <li key={edge.edge_id} data-edge-domain={edge.domain}>
          {source.title} → {target.title}: {edge.type.replace(/[_-]/g, " ")} ({edge.domain})
          {edge.target_revision_id && <span> · pinned revision <span className="font-mono">{edge.target_revision_id}</span></span>}
          {edge.availability !== "available" ? <span> · revision unavailable</span>
            : edge.target_revision_id && target.kind === "knowledge" && <button type="button"
              className="ml-2 text-indigo-300 underline" onClick={() => onSelect(target, edge.target_revision_id ?? null)}>
              Open pinned revision of {target.title}
            </button>}
        </li>;
      })}
    </ul>
  </section>;
}
