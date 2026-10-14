# Software-factory policy

**Status: normative.** Approved 2026-09-11 (vault spec
`projects/agent-queue/specs/software-factory-policy-simplification-2026-09-11.md`). This is AQ's one
admission, delivery and recovery policy. Shipped profiles (`src/profiles/defaults/`), skills
(`src/skills/`), prime templates (`src/prime/templates/`) and remembered guidance reference it.
Conflicting instructions are bugs; historical reports, reviews and superseded specs are evidence, not directives.

## A. Admit authorised, executable work

- Preserve the project, the user's intent, an explicit route (profile, intelligence class, provider)
  and an explicit placement. An unavailable pinned route waits visibly; it is never silently
  substituted.
- Emergent work defaults to a **child of the task that exposed it**. Choosing another authorised
  parent, or an explicit root for cross-cutting work, is a deliberate and visible choice. An open
  child blocks its parent's successful close; resolve it rather than moving it aside to close the
  source.
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
- Compatible completed sources collect into one frozen candidate per delivery branch. Base conflicts and
  authenticated absence of the exact PR workflow do not require authors to merge the default branch.
  Source identity, configured review authorization and red or unavailable checks gate admission. One bounded
  repair retains every frozen source as an ancestor, merges remaining members, regenerates shared artifacts once and validates the candidate before publication; see [the contract](../specs/design/conflicting-source-batches.md).
- There is **no automatic** per-task reviewer, final reviewer, merge sweep or review-of-review
  chain, and no mandatory multi-pass gate chain. Review is optional and risk-based; when wanted it
  is one explicit task or gate.
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
  reconciliation preserve it; reopening then failing replaces it. Current `FAILED` incidents,
  including aborted daemon-authored promotions, can be held with a recorded diagnosis without changing
  status or counters. Retry requires a stopped attempt on a `BLOCKED` task and all recovery guards.
- An accepted explicit task restart or reopen supersedes its previous recovery incident in the
  READY-write transaction, including holds on already-READY tasks; preserve diagnosis, history and
  recovery budget, and archive the notification. Projection and refused restarts retain holds;
  repair delegates release them only after branch handoff. Pauses, labels, dependencies, human gates
  and integration ownership remain binding.
- A known transient failure retries within the existing budget. An unknown cause, an exhausted
  budget or a human-only decision holds with a concrete reason and one durable escalation.
- Shared preparation faults (`slot_reset_failed`, missing/mismatched worktrees, repeated unproved
  own-branch handoffs) are infrastructure incidents. Correlate exact session, claim, workspace path, Git branch
  and ownership evidence; preserve bounded backoff and retained preparations. Escalate one incident with
  evidence and repair. Never mass-pause tasks to suppress faults or override human recovery holds.
- An explicitly authorized continuous integration policy may replace an exhausted repair session
  with a fresh bounded stage. It preserves previous attempts, dossiers and stopped-writer/fence
  proof; it does not reset the old session's budget. Routine CI/conflict repair continues
  automatically under that policy. Explicit product holds, reviewer rejections and human gates
  remain binding.
- Never fabricate a pass, reset a counter, delete a branch or restart a retired operation to make
  state look healthy. Retired work is shown as retired; resource cleanup is tracked separately.

## Testing scope

Workers use `aq test` for focused and related area checks, recording exact commands. Full-suite runs belong to CI
or tasks about the suite. Compare against a recorded known-failing baseline; do not capture one per task.
A pre-existing failure neither fails the task nor justifies weakening or skipping tests. Task authors specify
focused and area checks. See [resource gating](../guides/resource-gating.md) for the baseline-note workflow.

## Keeping instructions in line

Shipped profiles and skills are seeded write-if-absent: edit installed profiles in the vault and re-copy
drifted skills with `aq doctor --check skills.installed_drift --fix`. `tests/test_factory_policy_instructions.py`
rejects retired stages, sibling-by-default placement, refusal bypasses and roles that omit this page.
