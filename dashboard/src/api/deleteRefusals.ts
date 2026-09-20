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
 *
 * `code` is the contract (`src/api/codegen.py` keeps the full refusal body for
 * these two commands). The `error` prefix is a belt-and-braces fallback: the
 * daemon renders the prose as `"<code>: <detail>"`, so a surface still
 * recognises the refusal if the body is ever narrowed back to `{error}`.
 */
export function integrationHistoryRefusal(error: unknown): string | null {
  const payload = (error as { payload?: unknown } | null)?.payload;
  if (typeof payload !== "object" || payload === null) return null;
  const body = payload as { code?: unknown; error?: unknown };
  if (body.code === INTEGRATION_OWNED) return INTEGRATION_HISTORY_MESSAGE;
  return typeof body.error === "string" && body.error.startsWith(`${INTEGRATION_OWNED}:`)
    ? INTEGRATION_HISTORY_MESSAGE
    : null;
}
