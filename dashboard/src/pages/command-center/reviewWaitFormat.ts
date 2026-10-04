/** The review states as a reader says them; `in_review` is still pending. */
const STATE_LABEL: Record<string, string> = {
  in_review: "pending",
  changes_requested: "changes requested",
  rejected: "rejected",
  approved: "approved",
  withdrawn: "withdrawn",
};

export const REVIEW_STATE_TONE: Record<string, string> = {
  in_review: "bg-violet-500/25 text-violet-100",
  changes_requested: "bg-amber-500/25 text-amber-100",
  rejected: "bg-red-500/25 text-red-100",
  approved: "bg-emerald-500/25 text-emerald-100",
  withdrawn: "bg-gray-500/25 text-gray-200",
};

export function reviewStateLabel(state: string): string {
  return STATE_LABEL[state] ?? state.replace(/_/g, " ");
}

export function reviewHref(reviewId: string): string {
  return `/reviews/${encodeURIComponent(reviewId)}`;
}
