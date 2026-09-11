# Software-factory policy

**Status: normative.** Approved 2026-09-11 (vault spec
`projects/agent-queue/specs/software-factory-policy-simplification-2026-09-11.md`).
This page is the one statement of how AQ admits, delivers and recovers work.
Role instructions — shipped profiles (`src/profiles/defaults/`), AQ skills
(`src/skills/`), prime templates (`src/prime/templates/`) and remembered
project guidance — reference this page instead of restating workflow. Where an
instruction disagrees with this page, the instruction is the bug. Historical
reports, reviews and superseded specs are evidence, not directives.

## A. Admit authorised, executable work

- Preserve the project, the user's intent, an explicit route (profile,
  intelligence class, provider) and an explicit placement. An unavailable
  pinned route waits visibly; it is never silently substituted.
- Emergent work defaults to a **child of the task that exposed it**. Choosing
  another authorised parent, or an explicit root for cross-cutting work, is a
  deliberate and visible choice. An open child blocks its parent's successful
  close; resolve it rather than moving it aside to close the source.
- Provenance (`discovered-from`) records where work came from; it is not a
  blocking dependency.
- The supervisor coordinates and is never a queued worker. It does not run a
  task-scoped `aq prime`, which belongs to the worker that holds the task.
- Ordinary work routes to an eligible worker at `standard-high`; deep classes
  are a justified exception.
- Human approval applies where scope or authority requires it, bound to the
  exact work it approves. Routine in-scope work gets no extra approval gate.

## B. Deliver one exact candidate through one owner

- A worker close records a pushed checkpoint and the checks actually run. It
  is not proof that anything reached the default branch.
- Each repository/default branch has **one configured publisher**: the
  project's integration mode names it. Workers, reviewers and specialist roles
  do not merge or push to the default branch unless their task explicitly
  delegates publication and that configured owner is pull-request based.
- Publication validates the exact candidate SHA; a changed candidate
  invalidates earlier checks. The project declares its required checks. A
  check that could not run is *unavailable*, never a pass.
- There is **no automatic** per-task reviewer, final reviewer, merge sweep or
  review-of-review chain, and no mandatory multi-pass gate chain. Review is
  optional and risk-based; when wanted it is one explicit task or gate.
- A daemon refusal (scope check, CI policy, ownership fence, stale claim) is
  an answer. Report it; never route around it with another CLI (`gh pr merge`,
  a direct push), a `force` flag, or edits to state.

## C. Recover or retire without losing evidence

- One incident per failure, one responsible owner, bounded retries with the
  remaining budget visible.
- A known transient failure retries within the existing budget. An unknown
  cause, an exhausted budget or a human-only decision holds with a concrete
  reason and one durable escalation.
- Never fabricate a pass, reset a counter, delete a branch or restart a
  retired operation to make state look healthy. Retired work is shown as
  retired; resource cleanup is tracked separately.

## Testing scope

Workers run focused tests for what they change through the resource-governed
runner (`aq test`) and record the exact commands. Full CI belongs to the
project's configured candidate checks, not to every agent's local loop.

## Keeping instructions in line

Shipped profiles and skills are seeded write-if-absent, so a corrected default
does not overwrite an installed copy: installed profiles are edited in the
vault, and drifted skills are re-copied with
`aq doctor --check skills.installed_drift --fix`.
`tests/test_factory_policy_instructions.py` fails if a shipped instruction
reintroduces a retired stage, sibling-by-default placement or a refusal
bypass, or if a shipped role stops referencing this page.
