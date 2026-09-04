import { memo } from "react";
import { ArrowPathIcon } from "@heroicons/react/24/outline";
import type { Node, NodeProps } from "@xyflow/react";
import { NODE_HEIGHT, NODE_WIDTH, type PlaybookNodeData } from "./types";
import { playbookRunning, playbookScope, playbookState } from "./playbooks";

export function PlaybookCard({ data, selected = false, fluid = false }: { data: PlaybookNodeData; selected?: boolean; fluid?: boolean }) {
  const { playbook: p, onOpenPlaybook } = data;
  const active = playbookRunning(p);
  return <button type="button" data-playbook-card data-graph-node-id={`playbook:${p.id}`}
    aria-label={`Open playbook ${p.id}`} aria-pressed={selected}
    style={{ width: fluid ? "100%" : NODE_WIDTH, height: NODE_HEIGHT }}
    className={`nopan flex cursor-grab flex-col rounded-md border p-3 text-left text-xs shadow active:cursor-grabbing focus-visible:outline focus-visible:outline-2 focus-visible:outline-violet-300 ${active ? "border-violet-400 bg-violet-950 text-violet-100" : "border-violet-800 bg-gray-900 text-gray-200"} ${selected ? "outline outline-2 outline-white" : ""}`}
    onClick={event => { event.stopPropagation(); onOpenPlaybook?.(p.id); }}>
    <span className="flex w-full items-center justify-between gap-2 text-[10px] text-violet-300"><span className="inline-flex items-center gap-1"><ArrowPathIcon aria-hidden className={`h-3 w-3 ${active ? "animate-spin motion-reduce:animate-none" : ""}`} />Playbook</span><span>{playbookState(p)}</span></span>
    <span className="mt-2 line-clamp-2 w-full font-medium" title={p.id}>{p.id}</span>
    <span className="mt-1 w-full truncate text-[10px] text-gray-400" title={playbookScope(p)}>{playbookScope(p)}</span>
    <span className="mt-1 w-full truncate text-[10px] text-violet-300" title={(p.triggers ?? []).join(", ")}>{[...new Set(p.triggers ?? [])].join(" · ") || "Manual"}</span>
    <span className="mt-auto text-[10px] text-gray-400">Last run: {p.last_run?.status.replace(/_/g, " ") ?? "never"}{p.enabled === false && active ? " · triggers paused" : ""}</span>
  </button>;
}

function PlaybookNode({ data, selected }: NodeProps<Node<PlaybookNodeData, "playbook">>) {
  return <PlaybookCard data={data} selected={selected} />;
}

export default memo(PlaybookNode);
