import { memo, type ReactNode } from "react";
import { ArrowDownRightIcon, LockClosedIcon } from "@heroicons/react/24/outline";
import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import { CopyTaskIdButton } from "./CopyTaskIdButton";
import { ProgressBar } from "./ProgressBar";
import { NODE_HEIGHT, NODE_WIDTH, type TaskNodeData } from "./types";
import { ReviewWaitBadge } from "./ReviewWaitBadge";
import { StatusPill } from "./StatusPill";
import { pillForCard } from "./statusPillModel";
import { profileTags } from "./taskCardFormat";
import { taskReason, type Reason } from "./taskReason";

export type { TaskNodeData } from "./types";
type TaskNodeType = Node<TaskNodeData, "task">;

/** The raised card a status without its own fill gets (§3.1). */
const RAISED_FILL = "bg-g-card shadow-[0_1px_2px_rgba(0,0,0,.12)]";
const FOCUS_RING = "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-g-accent";
/** Meta-row tags: a quiet chip whose text stays at body contrast (never `--g-pending`). */
const TAG = "inline-block h-4 min-w-0 max-w-[110px] shrink truncate rounded px-[5px] text-[10px] leading-4";

interface CardProps {
  data: TaskNodeData;
  selected?: boolean;
  fluid?: boolean;
  layoutScale?: number;
}

/**
 * One task as the §3.1 card: title, one status row, an optional progress bar,
 * one reason sentence and a meta row. The open action is a `role="button"`
 * layer under the content; the copy, review and enter controls are siblings
 * painted above it, so no control nests inside another (§4.2).
 */
export function TaskCard({ data, selected = false, fluid = false, layoutScale = 1 }: CardProps) {
  const { task, gates, hierarchy, onOpenTask, onFocus, subtasks, phase, relations, stub } = data;
  const reviewWaits = data.reviewWaits ?? [];
  const reviewBlock = reviewWaits.find((wait) => wait.blocking) ?? null;
  const delivery = data.delivery ?? null;
  const pill = pillForCard(data);
  const { cardStatus, treatment } = pill;
  const epic = hierarchy.childCount > 0;
  const openGates = gates.filter((gate) => gate.status.toLowerCase() === "open");

  const reason = stub ? null : taskReason({
    status: cardStatus,
    isBlocked: Boolean(task.is_blocked),
    ...relations,
    profileId: task.profile_id,
    subtasks: subtasks ?? null,
    childCount: hierarchy.childCount,
    descDone: hierarchy.completedCount,
    descTotal: hierarchy.descendantCount,
    descRunning: hierarchy.runningCount,
    descBlocked: hierarchy.blockedCount,
    reviewWait: reviewBlock,
    openGateTypes: openGates.map((gate) => gate.gate_type),
    prUrl: task.pr_url,
    delivery,
  });

  const kind = phase ? `Phase ${phase.order}${phase.label ? ` · ${phase.label}` : ""}` : epic ? "Epic" : null;
  const border = treatment.borderClass ?? (epic ? "border-g-border-strong" : "border-g-border");
  // A 4px left bar takes 3px of the 12px left padding, so the content keeps its column.
  const padLeft = treatment.cardClass.includes("border-l-4") ? "pl-[9px]" : "pl-3";

  return (
    <div className="relative" style={{ width: fluid ? "100%" : NODE_WIDTH * layoutScale, height: NODE_HEIGHT * layoutScale }}>
      {epic && (
        // §2.5: a collapsed epic is a stack of sheets, its second outline 4px behind.
        <div aria-hidden className="absolute inset-0 translate-x-1 translate-y-1 rounded-[10px] border border-g-border-strong bg-g-panel" />
      )}
      <div
        data-task-card
        data-status={cardStatus}
        data-review-blocked={reviewBlock ? "" : undefined}
        className={`relative flex h-full flex-col rounded-[10px] border pt-[11px] pr-3 pb-[9px] ${padLeft} font-g text-g-text ${border} ${treatment.fillClass ?? RAISED_FILL} ${treatment.cardClass} ${treatment.stripe ? "aq-stripe" : ""} ${hierarchy.contextOnly ? "border-dashed" : ""} ${selected ? "outline outline-2 outline-offset-2 outline-g-accent" : ""}`}
      >
        <div
          role="button"
          tabIndex={0}
          aria-label={`Open task ${task.title}`}
          aria-pressed={selected}
          data-task-id={task.id}
          className={`nopan absolute inset-0 cursor-grab rounded-[10px] active:cursor-grabbing ${FOCUS_RING}`}
          onClick={(event) => {
            if (onOpenTask) {
              event.stopPropagation();
              onOpenTask(task.id, task);
            }
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              event.stopPropagation();
              onOpenTask?.(task.id, task);
            }
          }}
        />
        <div className="pointer-events-none relative flex min-h-0 flex-1 flex-col gap-1.5">
          <p className="line-clamp-2 text-[13.5px] font-semibold leading-[1.25] [text-wrap:balance]" title={task.title}>
            <span className={treatment.titleClass}>{task.title}</span>
          </p>
          <div className="flex min-h-5 min-w-0 items-center gap-2">
            {/* The status word never truncates; a review pill sharing the row
              * gives up its id first. */}
            {!stub && <StatusPill model={pill} className="shrink-0" />}
            {reviewWaits.length > 0 && (
              <span className="pointer-events-auto flex min-w-0">
                <ReviewWaitBadge waits={reviewWaits} variant="pill" />
              </span>
            )}
            <PriorityTag priority={task.priority} />
            {kind && (
              <span className="ml-auto flex min-w-0 shrink items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-g-muted">
                {phase && task.is_blocked && <LockClosedIcon aria-label="Phase gated" className="h-3 w-3 shrink-0" />}
                <span className="truncate" title={kind}>{kind}</span>
              </span>
            )}
          </div>
          {hierarchy.descendantCount > 0 ? (
            <BarHover label={`${hierarchy.completedCount} of ${hierarchy.descendantCount} tasks done`} onOpen={onOpenTask && (() => onOpenTask(task.id, task))}>
              <ProgressBar
                done={hierarchy.completedCount}
                total={hierarchy.descendantCount}
                running={hierarchy.runningCount}
                blocked={hierarchy.blockedCount}
                label={`${hierarchy.completedCount} of ${hierarchy.descendantCount} tasks done`}
              />
            </BarHover>
          ) : subtasks && subtasks.total > 0 ? (
            <BarHover label={`${subtasks.settled} of ${subtasks.total} subtasks settled`} onOpen={onOpenTask && (() => onOpenTask(task.id, task))}>
              <ProgressBar done={subtasks.settled} total={subtasks.total} label={`${subtasks.settled} of ${subtasks.total} subtasks settled`} />
            </BarHover>
          ) : null}
          <div className="mt-auto flex min-w-0 flex-col gap-1.5">
            {reason && <ReasonLine reason={reason} className={treatment.reasonClass} />}
            <div className="flex h-6 min-w-0 items-center gap-1.5 border-t border-g-border pt-[7px] text-[10.5px] text-g-muted">
              <span className="w-[10ch] shrink-0 truncate font-g-mono" title={task.id}>{task.id}</span>
              <CopyTaskIdButton
                taskId={task.id}
                className="nodrag nopan pointer-events-auto relative"
                toneClass="text-g-muted hover:bg-g-card-hover hover:text-g-text"
              />
              <span className="min-w-0 flex-1" />
              <MetaTags data={data} openGateTypes={openGates.map((gate) => gate.gate_type)} />
              {epic && onFocus && (
                <button
                  type="button"
                  aria-label={`Enter ${task.title}`}
                  title={`Enter ${task.title}`}
                  className={`nodrag nopan pointer-events-auto relative flex h-6 shrink-0 items-center gap-1 rounded-md border border-g-border bg-g-card px-[9px] text-[11px] font-medium text-g-text hover:bg-g-card-hover ${FOCUS_RING}`}
                  onClick={(event) => { event.stopPropagation(); onFocus(task.id); }}
                  onKeyDown={(event) => { if (event.key !== "Escape") event.stopPropagation(); }}
                >
                  Enter
                  <ArrowDownRightIcon aria-hidden className="h-3 w-3" />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** The bar's count as a tooltip: only a running card's pill and reason line
 *  say it in words. The content layer ignores the pointer, so the bar takes it
 *  back on a taller strip, and a click there still opens the task. */
function BarHover({ label, onOpen, children }: { label: string; onOpen?: () => void; children: ReactNode }) {
  return (
    <div
      data-bar-hover
      title={label}
      className="nopan pointer-events-auto -my-1 cursor-grab py-1 active:cursor-grabbing"
      onClick={(event) => {
        if (onOpen) {
          event.stopPropagation();
          onOpen();
        }
      }}
    >
      {children}
    </div>
  );
}

function ReasonLine({ reason, className }: { reason: Reason; className: string }) {
  return (
    <p data-reason className={`truncate text-[11.5px] leading-4 ${className}`} title={reason.text}>
      {reason.segments.map((segment, index) => (
        segment.strong ? <b key={index} className="font-semibold">{segment.text}</b> : <span key={index}>{segment.text}</span>
      ))}
    </p>
  );
}

/** §3.1: priority shows only when it is urgent enough to change what you do. */
function PriorityTag({ priority }: { priority?: number }) {
  if (priority == null || priority > 50) return null;
  const tone = priority <= 20 ? "bg-g-failed-soft text-g-failed" : "bg-g-blocked-soft text-g-blocked";
  return (
    <span className={`${TAG} shrink-0 font-bold ${tone}`} title={`Priority ${priority}`}>P{priority}</span>
  );
}

function MetaTags({ data, openGateTypes }: { data: TaskNodeData; openGateTypes: string[] }) {
  const tags: Array<{ key: string; text: string; title: string }> = [];
  if (data.stub?.foreign) tags.push({ key: "foreign", text: "other project", title: "This task lives in another project" });
  for (const tag of profileTags(data.task.profile_id, data.task.intelligence_class)) {
    tags.push({ key: `profile:${tag}`, text: tag, title: data.task.profile_id ?? tag });
  }
  if (openGateTypes.length > 0) {
    tags.push({ key: "gates", text: `${openGateTypes.length} gate${openGateTypes.length === 1 ? "" : "s"}`, title: openGateTypes.join(", ") });
  }
  if (data.hierarchy.contextOnly) tags.push({ key: "context", text: "context only", title: "Shown for context: outside the current filter" });
  if (tags.length === 0) return null;
  return tags.map((tag): ReactNode => (
    <span key={tag.key} className={`${TAG} bg-g-pending-soft text-g-muted`} title={tag.title}>{tag.text}</span>
  ));
}

/**
 * `data` identity is stable across a re-delivered layout for a card that did
 * not change (see `toFlowElements`' structural sharing), so memoising here
 * keeps a re-rendered `NodeWrapper` from repainting the card underneath it.
 */
function TaskNode({ data, selected }: NodeProps<TaskNodeType>) {
  return (
    <>
      <Handle id="in-left" type="target" position={Position.Left} isConnectable={false} />
      <Handle id="in-right" type="target" position={Position.Right} isConnectable={false} />
      <Handle id="in-top" type="target" position={Position.Top} isConnectable={false} />
      <TaskCard data={data} selected={selected} layoutScale={data.layoutScale} />
      <Handle id="out-left" type="source" position={Position.Left} isConnectable={false} />
      <Handle id="out-right" type="source" position={Position.Right} isConnectable={false} />
      <Handle id="out-bottom" type="source" position={Position.Bottom} isConnectable={false} />
    </>
  );
}

export default memo(TaskNode);
