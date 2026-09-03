import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import type { GraphNodeDTO, OutcomeExplanationDTO } from "../../api/client";
import { secondaryLine } from "./IntentSections";
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  STEP_KIND_LABELS,
  STEP_KIND_TONES,
  type SemanticGraphNodeData,
} from "./types";

type SemanticStepNodeType = Node<SemanticGraphNodeData, "semanticStep">;

/** The outcome ports of a card: exactly the outcomes that leave it. A terminal
 *  outcome ends the rule and is not a port. */
export function outgoingOutcomes(node: GraphNodeDTO): OutcomeExplanationDTO[] {
  return (node.explanation.outcomes ?? []).filter((outcome) => Boolean(outcome.target_step_id));
}

interface CardProps {
  data: SemanticGraphNodeData;
  selected?: boolean;
}

/** One artifact step. The whole card is a single button so pointer activation
 *  and Enter/Space go through the same accessible control.
 *
 *  Everything visible comes from `explanation` and the typed detail blocks —
 *  never from `advanced.typed_step`. A card that reconstructed meaning from
 *  the canonical step would be a second interpretation of the artifact. */
export function StepNodeCard({ data, selected = false }: CardProps) {
  const { node, onSelect } = data;
  const tone = STEP_KIND_TONES[node.step_kind] ?? STEP_KIND_TONES.command;
  const kindLabel = STEP_KIND_LABELS[node.step_kind] ?? node.step_kind;
  const secondary = secondaryLine(node);
  const ports = outgoingOutcomes(node);
  const badges = node.badges ?? [];
  const diagnostics = node.diagnostics ?? [];

  return (
    <button
      type="button"
      aria-label={`Inspect step ${node.title} (${kindLabel})`}
      aria-pressed={selected}
      data-step-id={node.id}
      data-step-kind={node.step_kind}
      style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}
      className={`nodrag nopan flex cursor-pointer flex-col gap-1 overflow-hidden rounded-md border p-2 text-left text-xs shadow ${tone} ${selected ? "outline outline-2 outline-white" : ""} focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-300`}
      onClick={(event) => {
        event.stopPropagation();
        onSelect?.(node.id);
      }}
    >
      <span className="flex w-full items-center justify-between gap-1">
        <span className="truncate text-[12px] font-semibold" title={node.explanation.title}>
          {node.explanation.title}
        </span>
        {node.entry && (
          <span className="shrink-0 rounded bg-black/40 px-1 text-[9px] uppercase tracking-wide">entry</span>
        )}
      </span>

      <span className="flex w-full items-center gap-1">
        <span className="rounded bg-black/40 px-1 py-0.5 text-[9px] uppercase tracking-wide">{kindLabel}</span>
        <span className="truncate font-mono text-[9px] opacity-70" title={node.id}>
          {node.id}
        </span>
      </span>

      {secondary && (
        <span className="line-clamp-2 w-full text-[10px] leading-4 opacity-90" title={secondary}>
          {secondary}
        </span>
      )}

      {badges.length > 0 && (
        <span className="flex w-full flex-wrap gap-1">
          {badges.map((badge) => (
            <span
              key={`${badge.kind}:${badge.label}`}
              className="rounded bg-white/10 px-1 text-[9px]"
              title={`${badge.label}: ${badge.value}`}
            >
              {badge.label} {badge.value}
            </span>
          ))}
        </span>
      )}

      {diagnostics.length > 0 && (
        <span className="w-full text-[9px] text-amber-200">
          {diagnostics.length} diagnostic{diagnostics.length === 1 ? "" : "s"}
        </span>
      )}

      <span className="mt-auto flex w-full flex-wrap gap-1" aria-label="Outcome ports" role="list">
        {ports.map((outcome) => (
          <span
            key={outcome.outcome}
            role="listitem"
            data-port={outcome.outcome}
            title={`${outcome.label || outcome.outcome} → ${outcome.target_title ?? outcome.target_step_id}`}
            className={`rounded px-1 text-[9px] ${outcome.reserved ? "bg-black/30 opacity-70" : "bg-black/40"}`}
          >
            {outcome.label || outcome.outcome}
          </span>
        ))}
      </span>
    </button>
  );
}

/** xyflow wrapper: one source handle per outcome port, evenly spread along the
 *  bottom edge so an edge visually leaves the outcome it belongs to. */
export default function SemanticStepNode({ data, selected }: NodeProps<SemanticStepNodeType>) {
  const ports = outgoingOutcomes(data.node);
  return (
    <>
      <Handle id="in" type="target" position={Position.Top} isConnectable={false} />
      <StepNodeCard data={data} selected={selected} />
      {ports.map((outcome, index) => (
        <Handle
          key={outcome.outcome}
          id={`out-${outcome.outcome}`}
          type="source"
          position={Position.Bottom}
          isConnectable={false}
          style={{ left: `${((index + 1) / (ports.length + 1)) * 100}%` }}
        />
      ))}
      {ports.length === 0 && (
        <Handle id="out-none" type="source" position={Position.Bottom} isConnectable={false} />
      )}
    </>
  );
}
