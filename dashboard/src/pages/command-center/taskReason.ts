import type { EpicDelivery } from "@aq/ts-client";

/** The fields a card already receives that the reason line is derived from.
 *  Titles (blocker, phase) come from nodes in loaded tiles; counts are the
 *  stub fallback (spec §3.4). */
export interface ReasonInput {
  status: string;
  isBlocked?: boolean;
  blockerTitle?: string | null;
  blockerCount?: number;
  nextPhase?: { order: number; label?: string | null } | null;
  profileId?: string | null;
  subtasks?: { total: number; settled: number } | null;
  descDone?: number;
  descTotal?: number;
  descRunning?: number;
  descBlocked?: number;
  reviewWait?: { review_id: string } | null;
  gateType?: string | null;
  prUrl?: string | null;
  delivery?: EpicDelivery | null;
  question?: string | null;
  pauseReason?: string | null;
  failureLine?: string | null;
  supersededBy?: string | null;
}

/** The one sentence under a card's progress bar (spec §3.3 reason column,
 *  §3.4 vocabulary). A status with nothing to say returns `null`; the card
 *  omits the line rather than inventing one. */
export function taskReason(input: ReasonInput): string | null {
  const { status } = input;
  // A review wait or an `is_blocked` open status reads as blocked whatever the
  // stored status is.
  if (input.isBlocked || input.reviewWait || status === "BLOCKED") return blockedReason(input);
  switch (status) {
    case "DEFINED": return definedReason(input);
    case "PENDING": return "Not yet ready";
    case "READY": return "Claimable · next in frontier";
    case "ASSIGNED": return input.profileId ? `Starting · ${input.profileId}` : "Starting";
    case "IN_PROGRESS": return inProgressReason(input);
    case "WAITING_INPUT": return input.question?.split("\n")[0]?.trim() || "Needs your call";
    case "PAUSED": return input.pauseReason?.split("\n")[0]?.trim() || "Paused";
    case "COMPLETED": {
      const pr = prNumber(input.prUrl);
      return pr != null ? `Finished · PR #${pr}` : "Finished";
    }
    case "FAILED": return input.failureLine?.split("\n")[0]?.trim() ?? null;
    case "CANCELLED":
    case "CANCELED": return input.supersededBy ? `Superseded by ${input.supersededBy}` : "Cancelled";
    case "SKIPPED": return null;
    default: return null;
  }
}

function blockedReason(input: ReasonInput): string {
  if (input.reviewWait) return `Waiting on review ${input.reviewWait.review_id}`;
  if (input.gateType) return `Waiting on gate ${input.gateType}`;
  if (input.blockerTitle) return `Waiting on ${input.blockerTitle}`;
  if (input.blockerCount && input.blockerCount > 0) return "Waiting on dependencies";
  return "Blocked";
}

function definedReason(input: ReasonInput): string {
  if (input.blockerTitle) return `After ${input.blockerTitle}`;
  if (input.blockerCount && input.blockerCount > 1) return `After ${input.blockerCount} tasks`;
  if (input.blockerCount === 1) return "After 1 task";
  if (input.nextPhase) {
    return `Before Phase ${input.nextPhase.order}${input.nextPhase.label ? ` · ${input.nextPhase.label}` : ""}`;
  }
  return "Not yet ready";
}

function inProgressReason(input: ReasonInput): string {
  if (input.subtasks && input.subtasks.total > 0) {
    return `${input.subtasks.settled} of ${input.subtasks.total} subtasks`;
  }
  if (input.descTotal && input.descTotal > 0) {
    const parts = [`${input.descDone ?? 0} of ${input.descTotal} done`];
    if (input.descRunning) parts.push(`${input.descRunning} running`);
    if (input.descBlocked) parts.push(`${input.descBlocked} blocked`);
    return parts.join(" · ");
  }
  return null;
}

function prNumber(url: string | null | undefined): number | null {
  if (!url) return null;
  const match = url.match(/\/(pull\/)?(\d+)/);
  return match ? Number(match[2]) : null;
}
