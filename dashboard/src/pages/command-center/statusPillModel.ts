/** The status pill model a card and a frame header share (§3.1, §2.4); the
 *  `StatusPill` component paints it. */
import type { HeldBatchSummary, ReviewWait } from "@aq/ts-client";
import { deliveryCardStatus, deliveryHeadline, describeDelivery, type EpicDelivery } from "../../components/epicDeliveryFormat";
import { reviewStateLabel } from "./reviewWaitFormat";
import { STATUS_TREATMENT, treatmentFor, type StatusTreatment } from "./taskStatus";
import type { TaskNodeData } from "./types";
import { DISPLAY_LABEL, DISPLAY_STATUS, rolledState, type EpicActivity } from "../../components/epicActivityFormat";

export interface StatusPillInput {
  /** The stored lifecycle status. */
  status: string;
  held_batch?: HeldBatchSummary | null;
  isBlocked?: boolean;
  /** An epic's delivery projection, when the daemon sent one. */
  delivery?: EpicDelivery | null;
  /** The first review wait that blocks this task. */
  reviewBlock?: ReviewWait | null;
  subtasks?: { total: number; settled: number } | null;
}

export interface StatusPillModel {
  /** The status the card or frame renders: the delivery projection applied. */
  cardStatus: string;
  treatment: StatusTreatment;
  text: string;
  /** The stored status and why the pill reads otherwise, for the tooltip. */
  title: string;
  /** The pill carries the pulse dot: the task, or its delivery, is moving. */
  working: boolean;
}

/**
 * The one status pill a card and a frame header share (§3.1, §2.4). A
 * delivery projection's status word and tone replace the stored lifecycle:
 * an integration hold is PAUSED in storage but is not a pause anyone chose.
 * A container's rolled-up activity replaces a lifecycle that only says "a
 * child is unfinished"; a batch in flight still outranks it.
 */
export function statusPillFor({ status, held_batch, isBlocked, delivery = null, reviewBlock = null, subtasks, display_state, display_reason, claim_readiness, child_state_counts }: StatusPillInput & EpicActivity): StatusPillModel {
  if (isIngestHeld(held_batch)) {
    return { cardStatus: "WAITING", treatment: treatmentFor("WAITING"),
      text: "Held for fidelity review", title: `Held for fidelity review${held_batch ? ` · batch ${held_batch.batch_id} · revision ${held_batch.structure_revision} · ${held_batch.state}` : ""} · task status ${status}`, working: false };
  }
  if (status === "READY" && !child_state_counts && claim_readiness && !claim_readiness.claimable) {
    return { cardStatus: "WAITING", treatment: treatmentFor("WAITING", false),
      text: claim_readiness.label, title: `${claim_readiness.reason} · task status READY`, working: false };
  }
  const rolled = rolledState({ display_state }, delivery);
  const cardStatus = rolled ? DISPLAY_STATUS[rolled] : deliveryCardStatus(delivery, status);
  const treatment = treatmentFor(cardStatus, !rolled && Boolean(isBlocked || reviewBlock));
  const subtaskTotal = subtasks?.total ?? 0;
  const text = rolled ? DISPLAY_LABEL[rolled]
    : delivery ? deliveryHeadline(delivery)
    : treatment === STATUS_TREATMENT.IN_PROGRESS && subtasks && subtaskTotal > 0
      ? `${treatment.pill} · ${Math.round((subtasks.settled / subtaskTotal) * 100)}%`
      : treatment.pill;
  const title = rolled
    ? `${display_reason ?? DISPLAY_LABEL[rolled]} · task status ${status}${delivery ? ` · ${describeDelivery(delivery)}` : ""}`
    : delivery
    ? `${describeDelivery(delivery)} · task status ${status}${delivery.hold === "integration" ? " (held by integration, not paused by anyone)" : ""}`
    : reviewBlock ? `${status} · waiting on review ${reviewBlock.review_id} (${reviewStateLabel(reviewBlock.review_state)})`
    : treatment === STATUS_TREATMENT.BLOCKED && status !== "BLOCKED" ? `${status} · blocked by dependencies or gates`
    : status;
  const working = rolled ? rolled === "active"
    : delivery ? delivery.state === "integrating" || delivery.state === "verifying" : cardStatus === "IN_PROGRESS";
  return { cardStatus, treatment, text, title, working };
}

export function isIngestHeld(batch: HeldBatchSummary | null | undefined): boolean {
  return !!batch && ["held", "reviewing", "needs_scope"].includes(batch.state);
}

/** The pill a task card shows, built from the card's own node data. The
 *  canvas reads the same model to count striped cards, so the two can never
 *  disagree about which cards are running. */
export function pillForCard(data: Pick<TaskNodeData, "task" | "delivery" | "reviewWaits" | "subtasks">): StatusPillModel {
  const reviewBlock = (data.reviewWaits ?? []).find((wait) => wait.blocking) ?? null;
  return statusPillFor({
    status: data.task.status, isBlocked: data.task.is_blocked, delivery: data.delivery ?? null,
    display_state: data.task.display_state, display_reason: data.task.display_reason,
    claim_readiness: data.task.claim_readiness, child_state_counts: data.task.child_state_counts,
    held_batch: data.task.held_batch,
    reviewBlock, subtasks: data.subtasks,
  });
}
