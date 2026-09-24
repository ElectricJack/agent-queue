/**
 * Delete/archive refusals a surface has to explain rather than retry.
 *
 * `task_delete` and `archive_task` check integration state before any write,
 * and each refusal is neither a question (unlike
 * `hierarchy.branch_discard_required`, see `branchDiscard.ts`) nor a transient
 * failure worth re-issuing. They come in two kinds:
 *
 * - **History** (`integration_history_retained`, delete only): append-only
 *   integration audit rows name the subtree. That never changes, so it is a
 *   fact about the task that wants saying in plain words — the daemon's detail
 *   names audit tables, which mean nothing to the operator.
 * - **State** (`integration_owned`, `integration_cleanup_blocked`,
 *   `integration_undelivered`): a running operation, a preserved resource or
 *   undelivered work holds the subtree for now, and the daemon's detail names
 *   the command that clears it — so that detail is what the operator sees.
 */

const INTEGRATION_OWNED = "hierarchy.integration_owned";
const INTEGRATION_CLEANUP_BLOCKED = "hierarchy.integration_cleanup_blocked";
const INTEGRATION_UNDELIVERED = "hierarchy.integration_undelivered";
const INTEGRATION_HISTORY_RETAINED = "hierarchy.integration_history_retained";

/** What the operator is told when integration history holds a task back. */
export const INTEGRATION_HISTORY_MESSAGE =
  "This task is part of the integration history Agent Queue keeps as a permanent " +
  "record, so it cannot be deleted. Archive it instead to remove it from the active graph.";

/**
 * The explanation for a delete the daemon refused because integration
 * history names the subtree, or null for any other failure.
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
  if (body.code === INTEGRATION_HISTORY_RETAINED) return INTEGRATION_HISTORY_MESSAGE;
  return typeof body.error === "string" && body.error.startsWith(`${INTEGRATION_HISTORY_RETAINED}:`)
    ? INTEGRATION_HISTORY_MESSAGE
    : null;
}

/**
 * The explanation for any integration refusal of a delete or archive, or null
 * for any other failure: plain words for history, the daemon's own remedy
 * (minus its `hierarchy.<code>:` prefix) for a state that passes.
 */
export function integrationRemovalRefusal(error: unknown): string | null {
  const history = integrationHistoryRefusal(error);
  if (history !== null) return history;
  const payload = (error as { payload?: unknown } | null)?.payload;
  if (typeof payload !== "object" || payload === null) return null;
  const body = payload as { code?: unknown; error?: unknown };
  if (
    body.code !== INTEGRATION_OWNED &&
    body.code !== INTEGRATION_CLEANUP_BLOCKED &&
    body.code !== INTEGRATION_UNDELIVERED
  ) {
    return null;
  }
  return typeof body.error === "string" ? body.error.replace(/^[^:]+:\s*/, "") : null;
}
