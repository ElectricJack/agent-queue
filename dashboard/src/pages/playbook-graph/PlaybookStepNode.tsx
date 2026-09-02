import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { effectLine } from "./explanation";
import {
  NODE_HEIGHT,
  NODE_TYPE_LABELS,
  NODE_TYPE_TONES,
  NODE_WIDTH,
  type PlaybookGraphNodeData,
} from "./types";

type PlaybookStepNodeType = Node<PlaybookGraphNodeData, "playbookStep">;

interface CardProps {
  data: PlaybookGraphNodeData;
  selected?: boolean;
}

function actionCommand(action: unknown): string | null {
  if (!action || typeof action !== "object" || Array.isArray(action)) return null;
  const command = (action as Record<string, unknown>).command;
  return typeof command === "string" && command.length > 0 ? command : null;
}

/** One compiled step. The whole card is a single button so pointer activation
 *  and Enter/Space go through the same accessible control. */
export function PlaybookStepCard({ data, selected = false }: CardProps) {
  const { node, onSelect } = data;
  const tone = NODE_TYPE_TONES[node.type] ?? NODE_TYPE_TONES.action;
  const typeLabel = NODE_TYPE_LABELS[node.type] ?? node.type;
  /* Contract intent leads when the node has it; `actionCommand` stays as the
   * fallback for an uncontracted action, and the prompt preview behind that. */
  const explanation = node.explanation;
  const preview = explanation?.title ?? actionCommand(node.details.action) ?? node.prompt_preview;
  const firstEffect = explanation?.effects?.[0];
  const detail = firstEffect ? effectLine(firstEffect) : null;

  return (
    <button
      type="button"
      aria-label={`Inspect node ${node.id}`}
      aria-pressed={selected}
      data-graph-node-id={node.id}
      style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}
      className={`nodrag nopan flex cursor-pointer flex-col gap-1 overflow-hidden rounded-md border p-2 text-left text-xs shadow ${tone} ${selected ? "outline outline-2 outline-white" : ""} focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300`}
      onClick={(event) => {
        event.stopPropagation();
        onSelect?.(node.id);
      }}
    >
      <span className="flex w-full items-center justify-between gap-1">
        <span className="truncate font-mono text-[11px] font-semibold" title={node.id}>{node.id}</span>
        <span aria-hidden className="shrink-0 opacity-80">{node.symbol}</span>
      </span>
      <span className="w-full">
        <span className="rounded bg-black/40 px-1 py-0.5 text-[9px] uppercase tracking-wide">{typeLabel}</span>
      </span>
      {preview && (
        <span className={`w-full text-[10px] leading-4 opacity-90 ${detail ? "line-clamp-2" : "line-clamp-3"}`}>
          {preview}
        </span>
      )}
      {detail && (
        <span className="line-clamp-1 w-full text-[10px] leading-4 opacity-70" title={detail}>
          {detail}
        </span>
      )}
      <span className="mt-auto flex w-full items-center gap-1 text-[9px] opacity-75">
        {node.timeout_seconds ? <span className="rounded bg-white/10 px-1">{node.timeout_seconds}s timeout</span> : null}
        {node.out_degree ? <span className="rounded bg-white/10 px-1">{node.out_degree} out</span> : null}
      </span>
    </button>
  );
}

export default function PlaybookStepNode({ data, selected }: NodeProps<PlaybookStepNodeType>) {
  return (
    <>
      <Handle id="in-top" type="target" position={Position.Top} isConnectable={false} />
      <Handle id="in-left" type="target" position={Position.Left} isConnectable={false} />
      <PlaybookStepCard data={data} selected={selected} />
      <Handle id="out-bottom" type="source" position={Position.Bottom} isConnectable={false} />
      <Handle id="out-right" type="source" position={Position.Right} isConnectable={false} />
    </>
  );
}
