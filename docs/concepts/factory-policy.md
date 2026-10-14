# Software-factory policy

**Status: normative.** Approved 2026-09-11 (vault spec
`projects/agent-queue/specs/software-factory-policy-simplification-2026-09-11.md`). This page is the
one statement of admission, delivery and recovery. Shipped profiles (`src/profiles/defaults/`), AQ skills
(`src/skills/`), prime templates (`src/prime/templates/`) and remembered guidance reference it instead of restating workflow.
Conflicting instructions are bugs; historical reports, reviews and superseded specs are evidence, not directives.

## A. Admit authorised, executable work

- Preserve the project, user intent, explicit route (profile, intelligence class, provider) and
  placement. An unavailable pinned route waits visibly; never silently substitute it.
- Emergent work defaults to a **child of the task that exposed it**. Another authorised parent or
  explicit root for cross-cutting work is a deliberate, visible choice. Open children block passing
  parent close; resolve them rather than moving them aside.
- Provenance (`discovered-from`) records where work came from; it is not a blocking dependency.
- The supervisor coordinates, never as a queued worker; task-scoped `aq prime` belongs to its worker.
- Ordinary work routes to an eligible `standard-high` worker; deep classes need justification.
- Human approval applies where scope or authority requires it, bound to the exact work it approves.
  Routine in-scope work gets no extra approval gate.
- A spec that supersedes or overlaps approved specs accounts for every predecessor goal as
  `kept`, `changed` or `dropped`, with source and replacement evidence. Dropped goals require
  Jack's explicit sign-off and are flagged in the review; delegated review approval alone
  cannot authorize them. See the [spec supersession checklist](../guides/reviews.md#spec-supersession-checklist).

## B. Deliver one exact candidate through one owner

- A worker close records a pushed checkpoint and actual checks, not delivery to the default branch.
- Each repository/default branch has **one configured publisher**, named by project integration mode.
  Workers, reviewers and specialists may merge or push to the default branch only with explicit task
  delegation and a pull-request-based configured owner.
- Publication validates the exact candidate SHA; a changed candidate invalidates earlier checks. The
  project declares its required checks. A check that could not run is *unavailable*, never a pass.
- Compatible completed sources for one delivery branch collect into one frozen candidate. A base
  conflict and authenticated absence of the exact PR workflow do not require each author to merge
  the default branch first. Source identity and configured review authorization still gate
  admission; genuine red or unavailable observations remain gates. One bounded batch repair carries
  every frozen source as an ancestor, merges all remaining members, regenerates shared artifacts
  once, and validates the complete candidate before publication. See [the conflicting-source
  contract](../specs/design/conflicting-source-batches.md).
- There is **no automatic** per-task reviewer, final reviewer, merge sweep or review-of-review chain,
  nor mandatory multi-pass gate chain. Optional, risk-based review uses one explicit task or gate.
- A daemon refusal (scope check, CI policy, ownership fence, stale claim) is an answer. Report it;
  never route around it with another CLI (`gh pr merge`, a direct push), a `force` flag, or edits to
  state.
- The restored Git-first train uses the exact-head PR gate, root cadence and separate cleanup.
  Controls preserve its checks and publisher ownership; see the [train and promotion policy
  entry](../specs/design/promotion-flow.md).
- [Promotion flows](../guides/promotion-flow.md) pin a PR at S, require the step's checks/review,
  attest S, and publish by fenced fast-forward plus an optional tag.

## C. Recover or retire without losing evidence

- One incident per failure, one responsible owner, bounded retries with the remaining budget visible.
- No-attempt failures use a durable failure-transition generation. Comments, edits and same-status
  reconciliation preserve it; reopening then failing replaces it. Current `FAILED` incidents,
  including aborted daemon-authored promotions, can be held with a recorded diagnosis without changing
  status or counters. Retry requires a stopped attempt on a `BLOCKED` task and all recovery guards.
- An accepted explicit task restart or reopen supersedes its previous recovery incident in the
  READY-write transaction, including holds on already-READY tasks; preserve diagnosis, history and
  recovery budget, and archive the notification. Projection and refused restarts retain holds;
  repair delegates release them only after branch handoff. Pauses, labels, dependencies, human gates
  and integration ownership remain binding.
- Known transient failures retry within budget; unknown causes, exhausted budgets or human-only
  decisions hold with a concrete reason and one durable escalation.
- Shared preparation faults (`slot_reset_failed`, missing/mismatched worktrees, repeated unproved
  own-branch handoffs) are infrastructure incidents. Correlate exact session, claim, workspace path,
  Git branch and ownership evidence; preserve bounded daemon backoff and retained preparations.
  Escalate one incident with evidence and required repair. Never mass-pause tasks to suppress a
  system fault or override an explicit human hold during recovery.
- An explicitly authorized continuous integration policy may replace an exhausted repair session
  with a fresh bounded stage. It preserves previous attempts, dossiers and stopped-writer/fence
  proof; it does not reset the old session's budget. Routine CI/conflict repair continues
  automatically under that policy. Explicit product holds, reviewer rejections and human gates
  remain binding.
- Never fabricate passes, reset counters, delete branches or restart retired operations to appear
  healthy. Show retired work as retired and track resource cleanup separately.

## Testing scope

Workers record exact `aq test` commands for focused and related area checks specified by task authors.
Full-suite runs belong to CI or suite tasks. Use a recorded known-failing baseline, never a new one per
task. Pre-existing failures neither fail the task nor justify weakening or skipping tests.
See [resource gating](../guides/resource-gating.md) for the baseline-note workflow.

## Keeping instructions in line

Shipped profiles and skills are seeded write-if-absent: edit installed profiles in the vault and re-copy
drifted skills with `aq doctor --check skills.installed_drift --fix`. `tests/test_factory_policy_instructions.py`
rejects retired stages, sibling-by-default placement, refusal bypasses and roles that omit this page.
