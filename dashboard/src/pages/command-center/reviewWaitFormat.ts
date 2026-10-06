/** The review states as a reader says them; `in_review` is still pending. */
const STATE_LABEL: Record<string, string> = {
  in_review: "pending",
  changes_requested: "changes requested",
  rejected: "rejected",
  approved: "approved",
  withdrawn: "withdrawn",
};

export const REVIEW_STATE_TONE: Record<string, string> = {
  in_review: "bg-g-accent-soft text-g-accent-ink",
  changes_requested: "bg-g-blocked-soft text-g-blocked",
  rejected: "bg-g-failed-soft text-g-failed",
  approved: "bg-g-done-soft text-g-done",
  withdrawn: "bg-g-pending-soft text-g-muted",
};

export function reviewStateLabel(state: string): string {
  return STATE_LABEL[state] ?? state.replace(/_/g, " ");
}

export function reviewHref(reviewId: string): string {
  return `/reviews/${encodeURIComponent(reviewId)}`;
}
