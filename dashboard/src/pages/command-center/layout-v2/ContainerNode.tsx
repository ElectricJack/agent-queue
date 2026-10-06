import { memo } from "react";
import { LockClosedIcon, MagnifyingGlassPlusIcon } from "@heroicons/react/24/outline";
import { Handle, Position } from "@xyflow/react";
import type { ContainerNodeData } from "../types";
import { ProgressBar } from "../ProgressBar";
import { EpicDeliveryBadge } from "../../../components/EpicDelivery";
import { ReviewWaitBadge } from "../ReviewWaitBadge";
import { UNIT_H } from "./units";

export interface ContainerNodeProps { id: string; data: ContainerNodeData; selected?: boolean }

function ContainerNode({ data, selected }: ContainerNodeProps) {
  const { node, onFocus, onOpenTask } = data;
  const headerPx = 0.35 * UNIT_H * (data.layoutScale ?? 1);
  const isPhase = node.phase_order != null;
  const phaseText = isPhase
    ? `Phase ${node.phase_order}${node.phase_label ? ` · ${node.phase_label}` : ""}`
    : "";
  const phaseHold = node.phase_hold;
  const failedChildren = phaseHold?.failed_children ?? [];
  const failedChildrenTotal = phaseHold?.failed_children_total ?? failedChildren.length;
  const hasBar = (node.agg_descendants ?? 0) > 0;
  const delivery = node.delivery ?? null;
  return (
    <div data-container-id={node.id} className={`h-full w-full rounded-lg border border-g-border bg-g-panel ${selected ? "outline-2 outline-offset-2 outline-g-accent" : ""} ${node.context_only ? "border-dashed" : ""}`}>
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
       * chip is an inline element inside the row; the progress bar is
       * absolutely positioned along the row's own bottom edge so it never
       * adds height.
       */}
      <div className="relative flex items-center gap-2 px-2 text-[11px] text-g-text" style={{ height: headerPx }}>
        {isPhase && (
          <span
            title={phaseText}
            className="flex shrink-0 items-center gap-0.5 truncate rounded bg-g-accent-soft px-1 text-[9px] font-semibold text-g-accent-ink"
            style={{ maxWidth: "5rem" }}
          >
            {node.is_blocked && <LockClosedIcon aria-label="Phase gated" className="h-2.5 w-2.5 shrink-0" />}
            <span className="truncate">{phaseText}</span>
          </span>
        )}
        {phaseHold && (
          <span
            title={`Waiting for failed work: ${failedChildrenTotal} child${failedChildrenTotal === 1 ? "" : "ren"}; ${phaseHold.descendant_blocker_count} incomplete descendant${phaseHold.descendant_blocker_count === 1 ? "" : "s"}`}
            className="flex shrink-0 items-center gap-1 overflow-x-auto rounded bg-g-failed-soft px-1 text-[9px] font-semibold text-g-failed"
          >
            <span className="shrink-0">Waiting for failed work</span>
            {failedChildren.map((child) => (
              <button
                key={child.id}
                type="button"
                aria-label={`Open failed work ${child.id} (${child.status})`}
                title={`${child.id} · ${child.status}`}
                className="nodrag nopan shrink-0 rounded bg-g-failed-soft px-1 font-mono hover:underline"
                onClick={(e) => {
                  e.stopPropagation();
                  onOpenTask?.(child.id, { id: child.id });
                }}
              >
                {child.id} · {child.status}
              </button>
            ))}
            {failedChildrenTotal > failedChildren.length && (
              <span className="shrink-0">+{failedChildrenTotal - failedChildren.length}</span>
            )}
          </span>
        )}
        {/* An epic gated by a review (`--after-review`) says so on its
          * header even while expanded, as a link to the review. */}
        <ReviewWaitBadge waits={node.review_waits ?? []} variant="chip" />
        <button type="button" aria-label={`Open task ${node.title}`} data-task-id={node.id}
          className="nodrag nopan min-w-0 flex-1 truncate text-left font-medium hover:underline"
          onClick={(e) => { e.stopPropagation(); onOpenTask?.(node.id, { id: node.id, playbook_run_id: node.playbook_run_id }); }}>{node.title}</button>
        {/* Delivery is not implementation: the badge says where the epic's
          * delivery stands, the count only how much of it is built. The
          * stored status (an integration hold reads PAUSED) stays in the
          * tooltip and on the task's own controls. */}
        {delivery ? (
          <EpicDeliveryBadge
            delivery={delivery}
            className="max-w-[16rem] shrink text-[10px]"
          />
        ) : (
          <span className="shrink-0 text-[9px] uppercase tracking-wide text-g-muted">{node.status.replace(/_/g, " ")}</span>
        )}
        <span className="shrink-0 rounded bg-g-pending-soft px-1 text-g-text" title="Implementation progress">
          {node.agg_completed}/{node.agg_descendants} {delivery ? "tasks complete" : "done"}
        </span>
        {(node.agg_running ?? 0) > 0 && <span className="shrink-0 text-g-accent-ink">{node.agg_running} running</span>}
        {(node.agg_blocked ?? 0) > 0 && <span className="shrink-0 text-g-blocked">{node.agg_blocked} blocked</span>}
        {/* Entering is the only way into a container; the container already
          * entered gets no control of its own (`onFocus` is withheld). */}
        {onFocus && (
          <button type="button" aria-label={`Enter ${node.title}`} title={`Enter ${node.title}`}
            className="nodrag nopan flex shrink-0 items-center gap-1 rounded px-1 py-0.5 font-medium hover:bg-g-border"
            onClick={(e) => { e.stopPropagation(); onFocus(node.id); }}
            onKeyDown={(e) => { if (e.key !== "Escape") e.stopPropagation(); }}>
            <MagnifyingGlassPlusIcon aria-hidden className="h-3.5 w-3.5" />Enter
          </button>
        )}
        {hasBar && (
          <div className="absolute inset-x-2 bottom-0">
            <ProgressBar
              done={node.agg_completed ?? 0}
              total={node.agg_descendants ?? 0}
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
