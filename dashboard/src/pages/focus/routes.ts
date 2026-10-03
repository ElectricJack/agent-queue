/** Canonical focus routes (mobile dashboard spec §4.1). */
export const FOCUS_ROOT = "/focus";

export function focusTaskHref(taskId: string): string {
  return `${FOCUS_ROOT}/tasks/${encodeURIComponent(taskId)}`;
}

/** `started` pins the watched process (the session's `started_at`); see FocusSession. */
export function focusSessionHref(sessionId: string, { started }: { started?: number | null } = {}): string {
  const base = `${FOCUS_ROOT}/sessions/${encodeURIComponent(sessionId)}`;
  return started != null ? `${base}?started=${encodeURIComponent(String(started))}` : base;
}

export function focusReportHref(reportId: string): string {
  return `${FOCUS_ROOT}/reports/${encodeURIComponent(reportId)}`;
}

export function focusReviewHref(reviewId: string): string {
  return `${FOCUS_ROOT}/reviews/${encodeURIComponent(reviewId)}`;
}

export function focusEscalationHref(escalationId: string): string {
  return `${FOCUS_ROOT}/escalations/${encodeURIComponent(escalationId)}`;
}

/** The one "needs you" page; the digest's link lands here (spec §6.1). */
export const FOCUS_INBOX = `${FOCUS_ROOT}/inbox`;

export function focusInboxHref(): string {
  return FOCUS_INBOX;
}

export function focusBatchHref(batchId: string): string {
  return `${FOCUS_ROOT}/batches/${encodeURIComponent(batchId)}`;
}

export const FOCUS_CONVERSATIONS = `${FOCUS_ROOT}/conversations`;

export function focusConversationHref(conversationId: string): string {
  return `${FOCUS_CONVERSATIONS}/${encodeURIComponent(conversationId)}`;
}

export function isFocusPath(pathname: string): boolean {
  return pathname === FOCUS_ROOT || pathname.startsWith(`${FOCUS_ROOT}/`);
}
