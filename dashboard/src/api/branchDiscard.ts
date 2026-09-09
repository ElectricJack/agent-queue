/**
 * The one delete refusal a surface is expected to act on rather than report.
 *
 * Deleting a task in a hierarchy/train project whose subtree already put a
 * branch on the remote is refused until the caller says what should happen to
 * that branch — see
 * `docs/superpowers/specs/2026-09-08-task-deletion-with-materialized-branches-design.md`.
 * The refusal names the branches so we can ask instead of guessing.
 */

export interface DiscardBranch {
  task_id: string;
  branch: string;
  base_sha: string;
}

export type BranchChoice = "keep" | "delete";

const CODE = "hierarchy.branch_discard_required";

function isBranch(value: unknown): value is DiscardBranch {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as DiscardBranch).task_id === "string" &&
    typeof (value as DiscardBranch).branch === "string"
  );
}

/**
 * The branches a failed delete is asking about, or null when the failure was
 * anything else. Returns an empty array only if the server sent the code with
 * no list, which callers should treat as "ask anyway".
 */
export function branchesAwaitingChoice(error: unknown): DiscardBranch[] | null {
  const payload = (error as { payload?: unknown } | null)?.payload;
  if (typeof payload !== "object" || payload === null) return null;
  const body = payload as { code?: unknown; branches?: unknown };
  if (body.code !== CODE) return null;
  return Array.isArray(body.branches) ? body.branches.filter(isBranch) : [];
}
