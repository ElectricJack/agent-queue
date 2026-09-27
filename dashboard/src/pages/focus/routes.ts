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

export function isFocusPath(pathname: string): boolean {
  return pathname === FOCUS_ROOT || pathname.startsWith(`${FOCUS_ROOT}/`);
}
