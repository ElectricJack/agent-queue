/** §3.3: each status is one pill, one border/fill treatment, one reason colour.
 *  Every status the dashboard can render gets a row — the table is the whole
 *  vocabulary, there is no fallback to a default tone (spec §7.1). */
export interface StatusTreatment {
  /** The pill word, which carries the meaning (nothing is colour-only). */
  pill: string;
  /** The soft-fill pill's classes. */
  pillClass: string;
  /** The border colour, or `null` for the card's default (strong on an epic).
   *  Kept apart from `cardClass` so the card never carries two utilities for
   *  one property, whose winner Tailwind would pick, not the class order. */
  borderClass: string | null;
  /** The fill and shadow, or `null` for the raised card default. */
  fillClass: string | null;
  /** The rest of the card's treatment: border style and left bar. */
  cardClass: string;
  /** Why colour a status's reason line in. */
  reasonClass: string;
  /** The title's weight/colour/decoration where the status changes it. */
  titleClass: string;
  /** Whether the IN_PROGRESS diagonal stripes apply. */
  stripe: boolean;
}

/** Cancelled and skipped cards recede by fading their surface, never their
 *  text: whole-card opacity takes muted and dim text to about 3:1, and every
 *  text pair must clear 4.5:1 (§4.2). The card border is decorative. */
const RECEDED = { borderClass: "border-g-border/55", fillClass: "bg-g-card/45 shadow-none" } as const;

export const STATUS_TREATMENT = {
  DEFINED: {
    pill: "Not started",
    pillClass: "bg-g-pending-soft text-g-muted",
    borderClass: null,
    fillClass: "bg-transparent shadow-none",
    cardClass: "border-dashed",
    reasonClass: "text-g-muted",
    titleClass: "text-g-muted",
    stripe: false,
  },
  PENDING: {
    pill: "Not started",
    pillClass: "bg-g-pending-soft text-g-muted",
    borderClass: null,
    fillClass: "bg-transparent shadow-none",
    cardClass: "border-dashed",
    reasonClass: "text-g-muted",
    titleClass: "text-g-muted",
    stripe: false,
  },
  READY: {
    pill: "Ready",
    pillClass: "bg-g-ready-soft text-g-ready",
    borderClass: "border-g-ready",
    fillClass: null,
    cardClass: "",
    reasonClass: "text-g-ready",
    titleClass: "",
    stripe: false,
  },
  ASSIGNED: {
    pill: "Assigned",
    pillClass: "bg-g-accent-soft text-g-accent-ink",
    borderClass: "border-g-accent",
    fillClass: null,
    cardClass: "",
    reasonClass: "text-g-accent-ink",
    titleClass: "",
    stripe: false,
  },
  IN_PROGRESS: {
    pill: "In progress",
    pillClass: "bg-g-run-soft text-g-accent-ink",
    borderClass: "border-g-accent",
    fillClass: null,
    cardClass: "",
    reasonClass: "text-g-accent-ink",
    titleClass: "",
    stripe: true,
  },
  WAITING_INPUT: {
    pill: "Needs your call",
    pillClass: "bg-g-call-soft text-g-call",
    borderClass: null,
    fillClass: null,
    cardClass: "border-l-4 border-l-g-call",
    reasonClass: "text-g-call",
    titleClass: "",
    stripe: false,
  },
  PAUSED: {
    pill: "Paused",
    pillClass: "bg-g-paused-soft text-g-paused",
    borderClass: null,
    fillClass: null,
    cardClass: "[filter:saturate(0.6)]",
    reasonClass: "text-g-paused",
    titleClass: "",
    stripe: false,
  },
  BLOCKED: {
    pill: "Blocked",
    pillClass: "bg-g-blocked-soft text-g-blocked",
    borderClass: null,
    fillClass: null,
    cardClass: "border-l-4 border-l-g-blocked",
    reasonClass: "text-g-blocked",
    titleClass: "",
    stripe: false,
  },
  FAILED: {
    pill: "Failed",
    pillClass: "bg-g-failed-soft text-g-failed",
    borderClass: "border-g-failed",
    fillClass: "bg-g-card [box-shadow:inset_0_0_0_1px_var(--g-failed)]",
    cardClass: "",
    reasonClass: "text-g-failed",
    titleClass: "",
    stripe: false,
  },
  COMPLETED: {
    pill: "Done",
    pillClass: "bg-g-done-soft text-g-done",
    borderClass: null,
    fillClass: "bg-g-panel shadow-none",
    cardClass: "",
    reasonClass: "text-g-muted",
    titleClass: "font-medium text-g-muted",
    stripe: false,
  },
  CANCELLED: {
    pill: "Cancelled",
    pillClass: "bg-g-pending-soft text-g-muted",
    borderClass: RECEDED.borderClass,
    fillClass: RECEDED.fillClass,
    cardClass: "border-dashed",
    reasonClass: "text-g-muted",
    titleClass: "text-g-muted line-through",
    stripe: false,
  },
  CANCELED: {
    pill: "Cancelled",
    pillClass: "bg-g-pending-soft text-g-muted",
    borderClass: RECEDED.borderClass,
    fillClass: RECEDED.fillClass,
    cardClass: "border-dashed",
    reasonClass: "text-g-muted",
    titleClass: "text-g-muted line-through",
    stripe: false,
  },
  SKIPPED: {
    pill: "Skipped",
    pillClass: "bg-g-pending-soft text-g-muted",
    borderClass: RECEDED.borderClass,
    fillClass: RECEDED.fillClass,
    cardClass: "border-dashed",
    reasonClass: "text-g-muted",
    titleClass: "text-g-muted line-through",
    stripe: false,
  },
} satisfies Record<string, StatusTreatment>;

export type KnownStatus = keyof typeof STATUS_TREATMENT;

export function isKnownStatus(status: string): status is KnownStatus {
  return Object.prototype.hasOwnProperty.call(STATUS_TREATMENT, status);
}

/** The statuses whose `is_blocked` flag switches to the blocked row. Not
 *  DEFINED or PENDING: `is_blocked` is set by any unmet predecessor, which for
 *  a task not started yet is ordinary sequencing — its row says "After <blocker>"
 *  (§3.3), and the blocked row there would make that form unreachable. */
const OPEN_STATUSES: readonly string[] = [
  "READY",
  "ASSIGNED",
  "IN_PROGRESS",
  "WAITING_INPUT",
  "PAUSED",
];

/**
 * The row a card's stored status takes: the blocked treatment when the daemon
 * reports the task blocked on any open status (§3.3), otherwise the status's
 * own. `cardStatus` is whatever `deliveryCardStatus` already resolved.
 */
export function treatmentFor(cardStatus: string, isBlocked = false): StatusTreatment {
  if (isBlocked && OPEN_STATUSES.includes(cardStatus)) return STATUS_TREATMENT.BLOCKED;
  return isKnownStatus(cardStatus) ? STATUS_TREATMENT[cardStatus] : unknownTreatment(cardStatus);
}

/** A status the table does not know yet (a daemon newer than the dashboard)
 *  keeps its own word on a neutral pill rather than passing for "Not started". */
function unknownTreatment(status: string): StatusTreatment {
  const word = status.replace(/_/g, " ").toLowerCase();
  return {
    pill: word.charAt(0).toUpperCase() + word.slice(1),
    pillClass: "bg-g-pending-soft text-g-muted",
    borderClass: null,
    fillClass: null,
    cardClass: "",
    reasonClass: "text-g-muted",
    titleClass: "",
    stripe: false,
  };
}
