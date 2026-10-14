# Retained workspace stop rework — wise-stone-87

Status: implementation investigation; this document does not authorize live recovery.
Starting source: `116134665b82d7d2f4551f9d3e91dd14ce2c8555`.

The operator's new request covers two lifecycle boundaries: a task that leaves
IN_PROGRESS while retaining a workspace, and a stopped dirty or unpublished
checkout that cannot complete owner handoff. Task status, a clean detached HEAD,
or a stopped historical session is insufficient evidence to release either.

## Required behavior

Supported status changes must retain a recoverable identity for unfinished
cleanup. Stop must be able to finish qualified cleanup after a task has reached
BLOCKED or FAILED without replaying its status transition, reopening it, or
resetting recovery counters. PAUSED cleanup remains owned by the existing pause
snapshot and continuation protocol; a stop cannot silently erase that decision.

Before any release, bind the exact current session instance, task claim epoch,
workspace path and acquisition generation, claim receipt, source repository and
branch, and integration owner fence. Revalidate after external evidence and at
the release transaction. A changed or missing identity, preparation uncertainty,
live operation or job pin, or unknown provider/process liveness retains the
source and exposes the exact refused proof and supported recovery owner.

Clean, published source may release a reusable reservation after those proofs.
Dirty, staged, untracked and unpublished source requires a durable checkpoint
that preserves HEAD, the index, worktree content and refs before reuse. Reuse
must preserve the checkpoint's restoration contract. Integrate the existing
fair-vault-95 implementation only from its proved published branch and SHA;
do not duplicate it or substitute an unproved ref. Releasing a reservation does
not delete a worktree and must actually return an eligible slot to pool capacity.

Every late callback must be harmless to a successor session, task epoch or
workspace generation. No blanket release of non-IN_PROGRESS workspaces is
allowed. Cancellation uncertainty survives status changes and daemon reload.
One existing bounded, scrubbed preparation formatter remains the diagnostic
owner; this work must not add a competing failure-description format.

## Verification and limits

Use disposable PostgreSQL and Git repositories to reproduce both boundaries,
then verify current clean and dirty success cases, exact capacity reclamation,
unknown liveness, publication/checkpoint failure, owner/job/operation pins,
successor epochs and stale callbacks. Run focused and related `aq test` checks.
Production lifecycle changes additionally require the real disposable swarm
scripts on final source or identical production trees. No full-suite run.

Historical Matter causality, foreign host state, live self-healing and incident
clearance remain unproved. Fixture success does not satisfy those original
acceptance gates. Operational steps require fresh qualified evidence and the
existing recovery owner, not force release or metadata deletion.

## Investigation on the starting source

Both `edit_task` and `set_task_status` can move an active task to BLOCKED or
FAILED without invoking workspace cleanup. `Orchestrator._stop_task_locked`
then refuses the task before inspecting its retained session or workspace.
The four-case diagnostic added to `tests/test_task_controls.py` reproduces that
boundary with a clean, detached disposable checkout: available workspace count
remains zero and the exact claim file remains present. Each case fails at the
expected stop refusal. This fixture diagnoses the early status gate only; its
LINK workspace does not establish that a managed slot is safe to reuse. Extend
it with complete source, generation, owner and publication/checkpoint evidence
before treating successful release as an acceptance test.

Existing focused handoff checks pass (nine cases), including successful saving
of a dirty active train writer and refusal of unsafe/reused completed writers.
Dirty content alone is therefore not a universal stop failure. The remaining
work must reproduce the reported dirty retention boundary and repair it without
weakening those refusals.

The local tracking ref
`origin/aq/epic/preserve-pause-checkpoints-after-interrupted-or-detached`
contains candidate `5a3e0d9bdd4db04eae156c48b4f8a639d228da0a`. It offers isolated
index capture and fenced pause persistence, and rejects automatic restoration
of an unproved detached branch. Its mapping to the requested fair-vault-95
delivery, tested revision and integration contract still require coordination.
It has not been merged. The continuation must also check acquisition generation,
job/operation pins and preparation cancellation holds; the candidate's snapshot
field list alone is not proof of those requirements.

Disposable PostgreSQL receipts from this investigation:

- `/tmp/wise87-epoch3-existing-stop.log`: nine passed in 20.65 seconds for
  `tests/test_integration_workspace_handoff.py -k 'stop_task or completed_pool_claim_recovery'`.
- `/tmp/wise87-epoch3-retention-repro.log`: four expected diagnostic failures in
  13.87 seconds for
  `tests/test_task_controls.py -k stop_finishes_retained_workspace_after_status_change`.

No production source has changed and no live recovery has been attempted.
