/**
 * Delete/archive refusals a surface has to explain rather than retry.
 *
 * `task_delete` and `archive_task` refuse, before any write, a subtree that
 * append-only integration audit rows still reference. Nothing the operator can
 * do in the dashboard changes that answer, so the refusal is neither a
 * question (unlike `hierarchy.branch_discard_required`, see `branchDiscard.ts`)
 * nor a transient failure worth re-issuing — it is a fact about the task that
 * wants saying in plain words.
 */

const INTEGRATION_OWNED = "hierarchy.integration_owned";

/** What the operator is told when integration history holds a task back. */
export const INTEGRATION_HISTORY_MESSAGE =
  "This task is part of the integration history Agent Queue keeps as a permanent " +
  "record, so it cannot be deleted. It will leave the graph on its own once " +
  "archiving tasks with integration history is supported.";

/**
 * The explanation for a delete or archive the daemon refused because
 * integration history names the subtree, or null for any other failure.
 */
export function integrationHistoryRefusal(error: unknown): string | null {
  const payload = (error as { payload?: unknown } | null)?.payload;
  if (typeof payload !== "object" || payload === null) return null;
  return (payload as { code?: unknown }).code === INTEGRATION_OWNED
    ? INTEGRATION_HISTORY_MESSAGE
    : null;
}
