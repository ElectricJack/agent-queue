# Software-factory policy

**Status: normative.** Approved 2026-09-11 (vault spec
`projects/agent-queue/specs/software-factory-policy-simplification-2026-09-11.md`). This page governs work
admission, delivery and recovery. Shipped profiles (`src/profiles/defaults/`), AQ skills
(`src/skills/`), prime templates (`src/prime/templates/`) and remembered guidance reference it instead of
restating workflow. Conflicts are bugs; historical reports, reviews and superseded specs are evidence, not directives.

## A. Admit authorised, executable work

- Preserve the project, the user's intent, an explicit route (profile, intelligence class, provider)
  and an explicit placement. An unavailable pinned route waits visibly; it is never silently
  substituted.
- Emergent work defaults to a **child of the task that exposed it**. Choosing another authorised
  parent or a root for cross-cutting work is deliberate and visible. An open child blocks its
  parent's successful close; resolve it rather than moving it aside to close the source.
- Provenance (`discovered-from`) records where work came from; it is not a blocking dependency.
- The supervisor coordinates and is never a queued worker. It does not run a task-scoped `aq prime`,
  which belongs to the worker that holds the task.
- Ordinary work routes to an eligible worker at `standard-high`; deep classes are a justified
  exception.
- Human approval applies where scope or authority requires it, bound to the exact work it approves.
  Routine in-scope work gets no extra approval gate.

## B. Deliver one exact candidate through one owner

- A worker close records a pushed checkpoint and the checks actually run. It is not proof that
  anything reached the default branch.
- Each repository/default branch has **one configured publisher**: the project's integration mode
  names it. Workers, reviewers and specialist roles do not merge or push to the default branch
  unless their task explicitly delegates publication and that configured owner is pull-request
  based.
- Publication validates the exact candidate SHA; a changed candidate invalidates earlier checks. The
  project declares its required checks. A check that could not run is *unavailable*, never a pass.
- Compatible completed sources for one delivery branch collect into one frozen candidate. A base
  conflict and authenticated absence of the exact PR workflow do not require each author to merge
  the default branch first. Source identity and configured review authorization still gate
  admission; genuine red or unavailable observations remain gates. One bounded batch repair carries
  every frozen source as an ancestor, merges all remaining members, regenerates shared artifacts
  once, and validates the complete candidate before publication. See [the conflicting-source
  contract](../specs/design/conflicting-source-batches.md).
- There is **no automatic** per-task reviewer, final reviewer, merge sweep or review-of-review
  chain, nor a mandatory multi-pass gate chain. Review is optional and risk-based: one explicit task or gate.
- A daemon refusal (scope check, CI policy, ownership fence, stale claim) is an answer. Report it;
  never route around it with another CLI (`gh pr merge`, a direct push), a `force` flag, or edits to
  state.
- The restored Git-first train uses the exact-head PR gate, root cadence and separate cleanup.
  Controls preserve its checks and publisher ownership; see the [train and promotion policy
  entry](../specs/design/promotion-flow.md).
- [Promotion flows](../guides/promotion-flow.md) pin a PR at S, require the step's checks/review,
  attest S, and publish by fenced fast-forward plus an optional tag.

## C. Recover or retire without losing evidence

- One incident per failure, one responsible owner, bounded retries with the remaining budget
  visible.
- No-attempt failures use a durable failure-transition generation. Comments, edits and same-status
  reconciliation preserve it; reopening then failing replaces it. Current `FAILED` incidents, including
  aborted daemon-authored promotions, can be held with a diagnosis without changing status or counters.
  Retry requires a stopped attempt on a `BLOCKED` task and all recovery guards.
- An accepted explicit task restart or reopen supersedes its previous recovery incident in the
  READY-write transaction, including holds on already-READY tasks; preserve diagnosis, history and
  recovery budget, and archive the notification. Projection and refused restarts retain holds;
  repair delegates release them only after branch handoff. Pauses, labels, dependencies, human gates
  and integration ownership remain binding.
- A known transient failure retries within the existing budget. An unknown cause, an exhausted
  budget or a human-only decision holds with a concrete reason and one durable escalation.
- Shared preparation faults (`slot_reset_failed`, missing or mismatched worktrees, repeated unproved
  own-branch handoffs) are infrastructure incidents. Correlate the exact session, claim, workspace
  path, Git branch and ownership evidence; preserve bounded backoff and retained preparations.
  Escalate one incident with that evidence and the required repair. Do not mass-pause tasks to
  suppress a system fault or override an explicit human hold during recovery.
- An explicitly authorized continuous integration policy may replace an exhausted repair session
  with a fresh bounded stage, preserving attempts, dossiers and stopped-writer/fence proof without
  resetting its budget. Routine CI/conflict repair continues automatically under that policy;
  explicit product holds, reviewer rejections and human gates remain binding.
- Never fabricate a pass, reset a counter, delete a branch or restart a retired operation to make
  state look healthy. Retired work is shown as retired; resource cleanup is tracked separately.

## Testing scope

Workers use `aq test` for focused and related area checks, recording exact commands. Full-suite runs belong to CI or
tasks about the suite. Use a recorded known-failing baseline, never one captured per task; pre-existing failures
neither fail the task nor permit weaker or skipped tests. Authors specify focused and area checks; see [resource gating](../guides/resource-gating.md).

## Keeping instructions in line

Shipped profiles and skills are seeded write-if-absent: edit installed vault profiles and re-copy drifted skills
with `aq doctor --check skills.installed_drift --fix`. `tests/test_factory_policy_instructions.py` rejects
retired stages, sibling-by-default placement, refusal bypasses and roles that omit this page.
