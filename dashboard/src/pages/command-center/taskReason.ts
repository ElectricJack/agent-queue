import type { EpicDelivery } from "../../components/epicDeliveryFormat";

/** One run of the reason sentence; `strong` runs are the noun a glance needs. */
export interface ReasonSegment {
  text: string;
  strong?: boolean;
}

export interface Reason {
  segments: ReasonSegment[];
  /** The whole sentence as plain text, for titles and accessible names. */
  text: string;
}

/** The fields a card already receives that the reason line is derived from.
 *  Titles come from nodes in the loaded tiles; counts are the fallback when the
 *  other end is not loaded (spec §3.4). */
export interface ReasonInput {
  /** The status the card renders: `deliveryCardStatus` already applied. */
  status: string;
  isBlocked?: boolean;
  /** Open blockers (tasks this one waits on), and the first one's title when loaded. */
  blockerCount?: number;
  blockerTitle?: string | null;
  /** The first loaded task waiting on this one. */
  dependentTitle?: string | null;
  /** Loaded READY tasks the frontier takes first: `null` when a priority tie
   *  makes the order unknowable from the layout (it also sorts by age). */
  readyAhead?: number | null;
  profileId?: string | null;
  subtasks?: { total: number; settled: number } | null;
  /** Collapsed epic counts: the count line is the epic card's reason. */
  childCount?: number;
  descDone?: number;
  descTotal?: number;
  descRunning?: number;
  descBlocked?: number;
  reviewWait?: { review_id: string } | null;
  openGateTypes?: readonly string[];
  prUrl?: string | null;
  delivery?: EpicDelivery | null;
}

/** The statuses whose `is_blocked` flag reads as the blocked row; mirrors
 *  `treatmentFor` (a not-started task's predecessors are its "After" form). */
const OPEN = new Set(["READY", "ASSIGNED", "IN_PROGRESS", "WAITING_INPUT", "PAUSED"]);

/** The one sentence under a card's progress bar (spec §3.3 reason column,
 *  §3.4 vocabulary). Every form is time-free. A status with nothing true to
 *  say returns `null`; the card omits the line rather than inventing one. */
export function taskReason(input: ReasonInput): Reason | null {
  if ((input.childCount ?? 0) > 0) return epicCountReason(input);
  const blocked = input.status === "BLOCKED" || !!input.reviewWait
    || (!!input.isBlocked && OPEN.has(input.status));
  if (blocked) return blockedReason(input);
  switch (input.status) {
    case "DEFINED":
    case "PENDING":
      return notStartedReason(input);
    case "READY":
      if (input.readyAhead === 0) return reason("Claimable · next in frontier");
      if (input.readyAhead != null) return reason(`Claimable · ${input.readyAhead} ahead`);
      return reason("Claimable");
    case "ASSIGNED":
      return reason(input.profileId ? `Starting · ${input.profileId}` : "Starting");
    case "IN_PROGRESS":
      return inProgressReason(input);
    case "WAITING_INPUT":
      return deliveryReason(input.delivery) ?? reason("Waiting on ", strong("your answer"));
    case "PAUSED":
      return deliveryReason(input.delivery);
    case "COMPLETED": {
      const pr = prNumber(input.prUrl);
      return reason(pr != null ? `Finished · PR #${pr}` : "Finished");
    }
    default:
      // FAILED, CANCELLED/CANCELED and SKIPPED: the layout carries no failure
      // summary or supersession, and the pill already names the outcome.
      return null;
  }
}

function blockedReason(input: ReasonInput): Reason {
  if (input.reviewWait) return reason("Waiting on ", strong("your review"), ` of ${input.reviewWait.review_id}`);
  const gate = input.openGateTypes?.[0];
  if (gate) return reason("Waiting on gate ", strong(gate));
  if (input.blockerTitle) return reason("Waiting on ", strong(input.blockerTitle), more(input.blockerCount));
  if ((input.blockerCount ?? 0) > 0) return reason("Waiting on ", strong(tasks(input.blockerCount!)));
  return reason("Waiting on dependencies");
}

function notStartedReason(input: ReasonInput): Reason {
  if (input.blockerTitle) return reason("After ", strong(input.blockerTitle), more(input.blockerCount));
  if ((input.blockerCount ?? 0) > 0) return reason("After ", strong(tasks(input.blockerCount!)));
  if (input.dependentTitle) return reason("Before ", strong(input.dependentTitle));
  return reason("Not yet ready");
}

function inProgressReason(input: ReasonInput): Reason {
  const subtasks = input.subtasks;
  if (subtasks && subtasks.total > 0) return reason(strong(`${subtasks.settled} of ${subtasks.total}`), " subtasks");
  return deliveryReason(input.delivery) ?? reason("Working");
}

function epicCountReason(input: ReasonInput): Reason {
  return epicCountLine({
    done: input.descDone ?? 0, total: input.descTotal ?? 0,
    running: input.descRunning, blocked: input.descBlocked,
  });
}

/** "3 of 6 done · 1 running · 1 blocked": a collapsed epic's reason line and
 *  an epic frame's header count line (§1.2). Zero counts are left out. */
export function epicCountLine(counts: { done: number; total: number; running?: number; blocked?: number }): Reason {
  const tail = [" done"];
  if (counts.running) tail.push(` · ${counts.running} running`);
  if (counts.blocked) tail.push(` · ${counts.blocked} blocked`);
  return reason(strong(`${counts.done} of ${counts.total}`), tail.join(""));
}

/** An epic's delivery projection explains its own hold, when it says why. */
function deliveryReason(delivery: EpicDelivery | null | undefined): Reason | null {
  const why = delivery?.reason?.split("\n")[0]?.trim();
  return why ? reason(why) : null;
}

function strong(text: string): ReasonSegment {
  return { text, strong: true };
}

function reason(...parts: Array<string | ReasonSegment>): Reason {
  const segments = parts.filter((p) => p !== "").map((p) => (typeof p === "string" ? { text: p } : p));
  return { segments, text: segments.map((s) => s.text).join("") };
}

function tasks(n: number): string {
  return n === 1 ? "1 task" : `${n} tasks`;
}

function more(count: number | undefined): string {
  const extra = (count ?? 1) - 1;
  return extra > 0 ? ` and ${extra} more` : "";
}

function prNumber(url: string | null | undefined): number | null {
  const match = url?.match(/\/pull\/(\d+)/);
  return match ? Number(match[1]) : null;
}
