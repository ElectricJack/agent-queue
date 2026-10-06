/** §3.3: each status is one pill, one border/fill treatment, one reason colour.
 *  Every status the dashboard can render gets a row — the table is the whole
 *  vocabulary, there is no fallback to a default tone (spec §7.1). */
export interface StatusTreatment {
  /** The pill word, which carries the meaning (nothing is colour-only). */
  pill: string;
  /** The soft-fill pill's classes. */
  pillClass: string;
  /** The card's border/fill treatment. */
  cardClass: string;
  /** Why colour a status's reason line in. */
  reasonClass: string;
  /** Whether the IN_PROGRESS diagonal stripes apply. */
  stripe: boolean;
}

export const STATUS_TREATMENT: Record<string, StatusTreatment> = {
  DEFINED: {
    pill: "Not started",
    pillClass: "bg-g-pending-soft text-g-muted",
    cardClass: "border-dashed bg-transparent shadow-none",
    reasonClass: "text-g-muted",
    stripe: false,
  },
  PENDING: {
    pill: "Not started",
    pillClass: "bg-g-pending-soft text-g-muted",
    cardClass: "border-dashed bg-transparent shadow-none",
    reasonClass: "text-g-muted",
    stripe: false,
  },
  READY: {
    pill: "Ready",
    pillClass: "bg-g-ready-soft text-g-ready",
    cardClass: "border-g-ready",
    reasonClass: "text-g-ready",
    stripe: false,
  },
  ASSIGNED: {
    pill: "Assigned",
    pillClass: "bg-g-accent-soft text-g-accent-ink",
    cardClass: "border-g-accent",
    reasonClass: "text-g-accent-ink",
    stripe: false,
  },
  IN_PROGRESS: {
    pill: "In progress",
    pillClass: "bg-g-run-soft text-g-accent-ink",
    cardClass: "border-g-accent",
    reasonClass: "text-g-accent-ink",
    stripe: true,
  },
  WAITING_INPUT: {
    pill: "Needs your call",
    pillClass: "bg-g-call-soft text-g-call",
    cardClass: "border-l-4 border-l-g-call",
    reasonClass: "text-g-call",
    stripe: false,
  },
  PAUSED: {
    pill: "Paused",
    pillClass: "bg-g-paused-soft text-g-paused",
    cardClass: "[filter:saturate(0.6)]",
    reasonClass: "text-g-paused",
    stripe: false,
  },
  BLOCKED: {
    pill: "Blocked",
    pillClass: "bg-g-blocked-soft text-g-blocked",
    cardClass: "border-l-4 border-l-g-blocked",
    reasonClass: "text-g-blocked",
    stripe: false,
  },
  FAILED: {
    pill: "Failed",
    pillClass: "bg-g-failed-soft text-g-failed",
    cardClass: "border-g-failed [box-shadow:0_0_0_1px_var(--g-failed)_inset]",
    reasonClass: "text-g-failed",
    stripe: false,
  },
  COMPLETED: {
    pill: "Done",
    pillClass: "bg-g-done-soft text-g-done",
    cardClass: "bg-g-panel shadow-none",
    reasonClass: "text-g-muted",
    stripe: false,
  },
  CANCELLED: {
    pill: "Cancelled",
    pillClass: "bg-g-pending-soft text-g-muted",
    cardClass: "border-dashed opacity-55",
    reasonClass: "text-g-muted",
    stripe: false,
  },
  CANCELED: {
    pill: "Cancelled",
    pillClass: "bg-g-pending-soft text-g-muted",
    cardClass: "border-dashed opacity-55",
    reasonClass: "text-g-muted",
    stripe: false,
  },
  SKIPPED: {
    pill: "Skipped",
    pillClass: "bg-g-pending-soft text-g-muted",
    cardClass: "border-dashed opacity-55",
    reasonClass: "text-g-muted",
    stripe: false,
  },
};

/** The statuses that may carry an `is_blocked` override to the blocked row. */
const OPEN_STATUSES: readonly string[] = [
  "DEFINED",
  "PENDING",
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
  return STATUS_TREATMENT[cardStatus] ?? STATUS_TREATMENT.DEFINED;
}
