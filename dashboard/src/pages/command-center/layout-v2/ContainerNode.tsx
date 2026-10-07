import { memo } from "react";
import { ArrowDownRightIcon, ArrowUpIcon, LockClosedIcon } from "@heroicons/react/24/outline";
import { Handle, Position } from "@xyflow/react";
import type { ContainerNodeData } from "../types";
import { ProgressBar } from "../ProgressBar";
import { ReviewWaitBadge } from "../ReviewWaitBadge";
import { StatusPill } from "../StatusPill";
import { statusPillFor } from "../statusPillModel";
import { epicCountLine } from "../taskReason";
import { UNIT_H } from "./units";

export interface ContainerNodeProps { id: string; data: ContainerNodeData; selected?: boolean }

const FOCUS_RING = "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-g-accent";
const HEADER_BUTTON = `nodrag nopan flex h-6 min-w-0 shrink-0 items-center gap-1 rounded-md border border-g-border bg-g-card px-[9px] text-[11px] font-medium text-g-text hover:bg-g-card-hover ${FOCUS_RING}`;

/**
 * An epic as a quiet frame around its children (§2.4): a panel with one
 * header line -- kind, title, count line, then the status pill and the way in
 * (Enter) or, on the entered frame, the way back (Up to the parent).
 */
function ContainerNode({ data, selected }: ContainerNodeProps) {
  const { node, onFocus, onOpenTask, upTarget, onUp } = data;
  const headerPx = 0.35 * UNIT_H * (data.layoutScale ?? 1);
  const isPhase = node.phase_order != null;
  const phaseText = isPhase
    ? `Phase ${node.phase_order}${node.phase_label ? ` · ${node.phase_label}` : ""}`
    : "";
  const phaseHold = node.phase_hold;
  const failedChildren = phaseHold?.failed_children ?? [];
  const failedChildrenTotal = phaseHold?.failed_children_total ?? failedChildren.length;
  const firstFailed = failedChildren[0];
  const reviewWaits = node.review_waits ?? [];
  // Delivery is not implementation: the pill says where the epic's delivery
  // stands, the count line only how much of it is built. The stored status
  // (an integration hold reads PAUSED) stays in the pill's tooltip.
  const pill = statusPillFor({
    status: node.status,
    isBlocked: node.is_blocked,
    delivery: node.delivery ?? null,
    reviewBlock: reviewWaits.find((wait) => wait.blocking) ?? null,
  });
  const total = node.agg_descendants ?? 0;
  const count = epicCountLine({
    done: node.agg_completed ?? 0, total, running: node.agg_running, blocked: node.agg_blocked,
  });
  const border = node.depth === 0 ? "border-g-border-strong" : "border-g-border";
  return (
    <div data-container-id={node.id} className={`h-full w-full rounded-xl border bg-g-panel font-g ${border} ${selected ? "outline-2 outline-offset-2 outline-g-accent" : ""} ${node.context_only ? "border-dashed" : ""}`}>
      <Handle id="in-left" type="target" position={Position.Left} isConnectable={false} />
      <Handle id="in-right" type="target" position={Position.Right} isConnectable={false} />
      <Handle id="in-top" type="target" position={Position.Top} isConnectable={false} />
      {/*
       * Everything below lives in this ONE fixed-height row: the engine only
       * reserves `headerPx` (0.35 * UNIT_H, mirroring `HEADER_H` in
       * src/task_graph/layout/constants.py) before a container's first child
       * row, so a sibling stacked above/below this div -- a second header
       * line, a banner, a progress bar div -- pushes that reserved space and
       * overlaps the first row of children on the real canvas. The phase
       * chip is an inline element inside the row; the progress line is
       * absolutely positioned along the row's own bottom edge so it never
       * adds height.
       */}
      <div className="relative flex items-center gap-2.5 rounded-t-[11px] border-b border-g-border bg-g-panel-head px-3.5 text-g-text" style={{ height: headerPx }}>
        {isPhase ? (
          <span
            title={phaseText}
            className="flex shrink-0 items-center gap-1 truncate rounded-md bg-g-accent-soft px-1.5 text-[10px] font-semibold leading-5 text-g-accent-ink"
            style={{ maxWidth: "8rem" }}
          >
            {node.is_blocked && <LockClosedIcon aria-label="Phase gated" className="h-3 w-3 shrink-0" />}
            <span className="truncate">{phaseText}</span>
          </span>
        ) : (
          <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide text-g-muted">Epic</span>
        )}
        <button type="button" aria-label={`Open task ${node.title}`} data-task-id={node.id} title={node.title}
          className={`nodrag nopan min-w-0 shrink truncate rounded text-left text-[13px] font-semibold hover:underline ${FOCUS_RING}`}
          onClick={(e) => { e.stopPropagation(); onOpenTask?.(node.id, { id: node.id, playbook_run_id: node.playbook_run_id }); }}>{node.title}</button>
        {total > 0 && (
          <span data-count-line title={count.text} className="min-w-0 shrink-[2] truncate whitespace-nowrap text-[12px] tabular-nums text-g-muted">
            {count.segments.map((segment, index) => (
              segment.strong ? <b key={index} className="font-semibold text-g-text">{segment.text}</b> : <span key={index}>{segment.text}</span>
            ))}
          </span>
        )}
        {/* §1.2: a phase held on failed work condenses to "N failed" in the
          * count line; it opens the first failed child, and the tooltip
          * names the rest until the frame's hover card carries them. */}
        {phaseHold && failedChildrenTotal > 0 && (
          <button
            type="button"
            aria-label={`${failedChildrenTotal} failed${firstFailed ? `: open failed work ${firstFailed.id} (${firstFailed.status})` : ""}`}
            title={`Waiting for failed work: ${failedChildren.map((child) => `${child.id} · ${child.status}`).join(", ")}${failedChildrenTotal > failedChildren.length ? ` and ${failedChildrenTotal - failedChildren.length} more` : ""}; ${phaseHold.descendant_blocker_count} incomplete descendant${phaseHold.descendant_blocker_count === 1 ? "" : "s"}`}
            disabled={!firstFailed}
            className={`nodrag nopan shrink-0 rounded text-[12px] font-semibold tabular-nums text-g-failed enabled:hover:underline ${FOCUS_RING}`}
            onClick={(e) => {
              e.stopPropagation();
              if (firstFailed) onOpenTask?.(firstFailed.id, { id: firstFailed.id });
            }}
          >
            {failedChildrenTotal} failed
          </button>
        )}
        <span className="min-w-0 flex-1" />
        {/* An epic gated by a review (`--after-review`) says so on its
          * header even while expanded, as a link to the review. */}
        {reviewWaits.length > 0 && <ReviewWaitBadge waits={reviewWaits} variant="pill" />}
        <StatusPill model={pill} className="max-w-[16rem] shrink" />
        {/* Entering is the only way into a container; the container already
          * entered offers the way back instead (`onFocus` is withheld). */}
        {onFocus ? (
          <button type="button" aria-label={`Enter ${node.title}`} title={`Enter ${node.title}`} className={HEADER_BUTTON}
            onClick={(e) => { e.stopPropagation(); onFocus(node.id); }}
            onKeyDown={(e) => { if (e.key !== "Escape") e.stopPropagation(); }}>
            Enter
            <ArrowDownRightIcon aria-hidden className="h-3 w-3" />
          </button>
        ) : upTarget && onUp ? (
          <button type="button" aria-label={`Up to ${upTarget.title}`} title={`Up to ${upTarget.title}`} className={`${HEADER_BUTTON} max-w-[14rem]`}
            onClick={(e) => { e.stopPropagation(); onUp(upTarget.id); }}
            onKeyDown={(e) => { if (e.key !== "Escape") e.stopPropagation(); }}>
            <ArrowUpIcon aria-hidden className="h-3 w-3 shrink-0" />
            <span className="truncate">Up to {upTarget.title}</span>
          </button>
        ) : null}
        {total > 0 && (
          <div className="absolute inset-x-3.5 bottom-0">
            <ProgressBar
              variant="line"
              done={node.agg_completed ?? 0}
              total={total}
              running={node.agg_running ?? 0}
              blocked={node.agg_blocked ?? 0}
            />
          </div>
        )}
      </div>
      <Handle id="out-left" type="source" position={Position.Left} isConnectable={false} />
      <Handle id="out-right" type="source" position={Position.Right} isConnectable={false} />
      <Handle id="out-bottom" type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

/** Its `data` keeps its identity while the container is unchanged, so a
 *  re-rendered `NodeWrapper` costs nothing here. */
export default memo(ContainerNode);
