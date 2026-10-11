/** The status pill model a card and a frame header share (§3.1, §2.4); the
 *  `StatusPill` component paints it. */
import type { ReviewWait } from "@aq/ts-client";
import { deliveryCardStatus, deliveryHeadline, describeDelivery, type EpicDelivery } from "../../components/epicDeliveryFormat";
import { reviewStateLabel } from "./reviewWaitFormat";
import { STATUS_TREATMENT, treatmentFor, type StatusTreatment } from "./taskStatus";
import type { TaskNodeData } from "./types";
import { DISPLAY_LABEL, DISPLAY_STATUS, type EpicActivity } from "../../components/epicActivityFormat";

export interface StatusPillInput extends EpicActivity {
  /** The stored lifecycle status. */
  status: string;
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
 */
export function statusPillFor({ status, isBlocked, delivery = null, reviewBlock = null, subtasks, display_state, display_reason }: StatusPillInput): StatusPillModel {
  const displayState = display_state ?? delivery?.display_state;
  const displayReason = display_reason ?? delivery?.display_reason;
  const cardStatus = displayState ? DISPLAY_STATUS[displayState] : deliveryCardStatus(delivery, status);
  const treatment = treatmentFor(cardStatus, displayState ? false : Boolean(isBlocked || reviewBlock));
  const subtaskTotal = subtasks?.total ?? 0;
  const text = displayState && (!delivery || ["implementing", "not_tracked"].includes(delivery.state)) ? DISPLAY_LABEL[displayState]
    : delivery ? deliveryHeadline(delivery)
    : treatment === STATUS_TREATMENT.IN_PROGRESS && subtasks && subtaskTotal > 0
      ? `${treatment.pill} · ${Math.round((subtasks.settled / subtaskTotal) * 100)}%`
      : treatment.pill;
  const title = displayState
    ? `${displayReason ?? DISPLAY_LABEL[displayState]} · task status ${status}${delivery ? ` · ${describeDelivery(delivery)}` : ""}`
    : delivery
    ? `${describeDelivery(delivery)} · task status ${status}${delivery.hold === "integration" ? " (held by integration, not paused by anyone)" : ""}`
    : reviewBlock ? `${status} · waiting on review ${reviewBlock.review_id} (${reviewStateLabel(reviewBlock.review_state)})`
    : treatment === STATUS_TREATMENT.BLOCKED && status !== "BLOCKED" ? `${status} · blocked by dependencies or gates`
    : status;
  const working = displayState ? ["active", "collecting", "delivering"].includes(displayState)
    : delivery ? delivery.state === "integrating" || delivery.state === "verifying" : cardStatus === "IN_PROGRESS";
  return { cardStatus, treatment, text, title, working };
}

/** The pill a task card shows, built from the card's own node data. The
 *  canvas reads the same model to count striped cards, so the two can never
 *  disagree about which cards are running. */
export function pillForCard(data: Pick<TaskNodeData, "task" | "delivery" | "reviewWaits" | "subtasks">): StatusPillModel {
  const reviewBlock = (data.reviewWaits ?? []).find((wait) => wait.blocking) ?? null;
  return statusPillFor({
    status: data.task.status, isBlocked: data.task.is_blocked, delivery: data.delivery ?? null,
    display_state: data.task.display_state, display_reason: data.task.display_reason,
    reviewBlock, subtasks: data.subtasks,
  });
}
