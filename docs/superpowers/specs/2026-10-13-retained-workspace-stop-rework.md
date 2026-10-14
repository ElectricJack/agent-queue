# Retained workspace stop rework — wise-stone-87

Status: implementation under validation; this document does not authorize live recovery.
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

## Initial investigation on the starting source

Both `edit_task` and `set_task_status` can move an active task to BLOCKED or
FAILED without invoking workspace cleanup. `Orchestrator._stop_task_locked`
then refuses the task before inspecting its retained session or workspace.
The initial four-case diagnostic in `tests/test_task_controls.py` reproduced that
boundary with a clean, detached disposable checkout: available workspace count
remains zero and the exact claim file remains present. Each case fails at the
expected stop refusal. That fixture diagnosed the early status gate only; its
LINK workspace does not establish that a managed slot is safe to reuse. Extend
it with complete source, generation, owner and publication/checkpoint evidence
before treating successful release as an acceptance test. The final regression
fixture now uses registered managed worktrees and a disposable bare origin.

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
delivery, tested revision and integration contract initially required coordination.
The supervisor subsequently confirmed it, as recorded below. Cleanup also checks acquisition generation,
job/operation pins and preparation cancellation holds; the candidate's snapshot
field list alone is not proof of those requirements.

Disposable PostgreSQL receipts from this investigation:

- `/tmp/wise87-epoch3-existing-stop.log`: nine passed in 20.65 seconds for
  `tests/test_integration_workspace_handoff.py -k 'stop_task or completed_pool_claim_recovery'`.
- `/tmp/wise87-epoch3-retention-repro.log`: four expected diagnostic failures in
  13.87 seconds for
  `tests/test_task_controls.py -k stop_finishes_retained_workspace_after_status_change`.

These receipts predate the implementation. No live recovery has been attempted.

## Selected implementation

Supervisor message `msg-0f9bda4bf55f4cab9250836957b59fa1` qualifies exact checkpoint
source `5a3e0d9bdd4db04eae156c48b4f8a639d228da0a`; it was merged with the current
cancellation ancestry. The sole source conflict keeps checkout validation first
and checkpoint validation before stale-lock cleanup. Fleet-vault-71 owns kind,
base and pool selection; this change owns lifecycle cleanup only.

For an explicit stop of BLOCKED or FAILED work, persist a separate
`retained_workspace_stop` snapshot without changing the task's status or epoch.
Require the acquisition receipt and exact current session, agent, registered
workspace/base, repository and task-owned branch attachment. Reject missing
proof, changed generations, preparation uncertainty, job pins and integration
operation ownership. Retrying stop uses that same snapshot, never a refreshed
guess at a successor's identity.

Revalidate and hold database identity locks through bounded provider stop,
fresh process inspection, checkpoint persistence, nondestructive detachment and
the final native owner/session/workspace release. Capture uses fair-vault-95's
isolated index. A full receipt is required for this release path; partial salvage
cannot authorize slot reuse. Preserve the original branch-null detached capture;
restoration needs a separate proved task branch ref tied to the captured HEAD,
claim and sentinel. Commit the checkpoint and exact claim-retirement intent
while retaining the reservation, then retire the matching old claim file under
the final release transaction's workspace lock. A crash after unlink leaves the
reservation held and permits a retry only against that durable identity and
checkpoint, with fresh termination proof. This avoids exposing an available
slot with a stale claim after a crash. The reusable registration and source remain.

Fresh local process inspection includes both the exact instance marker and
unmarked processes whose current directory or open descriptors use the source
or its Git directory. Unreadable live same-user processes refuse release.
Transient process teardown permits at most three complete fresh inventories;
retry exhaustion remains an unknown result. Neither a missing marker nor a
historical stopped session is sufficient.

Managed pause snapshots additionally fence the acquisition receipt and generation, job pins,
integration operation ownership and preparation uncertainty. Full checkpoint
failure exhausts the automatic retry budget while retaining ownership. Explicit
`aq task pause <id>` can retry the same source after the missing proof is resolved;
resume does not bypass that proof. A child whose termination could not be proved
records a durable preparation cleanup hold that survives an orchestrator reload.

BLOCKED/FAILED cleanup uses `aq task stop <id>`. It preserves status, epoch and
retry counters and records a `retained_workspace_checkpoint` task context before
release. A completed stop is idempotent only for the same task identity. A changed
identity must use the existing recovery owner; deleting the stop receipt, pause
metadata, claims or locks is not a supported repair.

Successful activation of a newer claim retires a completed old stop receipt
through the claim transaction, keeping its durable checkpoint context. Pending
cleanup receipts remain intact. A late cleanup using the old snapshot fails its
metadata/epoch fence; a new explicit stop can snapshot the new active claim.

## Initial candidate verification receipts

Initial candidate: `fbea160ac4699286014dc6dbe72e59613e3f49a0`, preserving the
required `116134665`, cancellation `18107effe`, and qualified checkpoint
`5a3e0d9` ancestry. All PostgreSQL checks use the disposable test service on
localhost:5534.

- Focused: `aq test tests/test_task_controls.py tests/test_session_runtime_units.py
  tests/test_selection_catalogue.py -q` — 244 passed in 189.62 seconds;
  `/tmp/wise87-lifecycle-focused-final.log`.
- Related area: Git manager/mutex/identity; worktree manager, preparation,
  acquisition, reaper, doctor and capacity; claim commands, session reconciler,
  workspace attachments, integration owner recovery and handoff; preparation
  diagnostics, stranded work, task control API and surface commands — 1,536
  passed and one existing strict xfail in 542.26 seconds;
  `/tmp/wise87-lifecycle-area.log`. The xfail is
  `test_verify_skips_the_automerge_under_worktree_mode`, whose existing marker
  reserves the unfinished phase-three auto-merge behavior.
- Changed-path Ruff, selection catalogue regeneration and `git diff --check`
  passed. No full-suite run or live recovery was performed.

The initial fixture mismatch and impossible synchronous pause interleaving
found during integration were corrected without removing the source fences.
The claim fixtures now carry acquisition receipts. Preparation holds the
workspace lock, so a native pause must serialize behind it; the checkpoint
activation race now uses a supported BLOCKED status transition, and separate
coverage verifies pause/preparation serialization. Final focused and area runs
above supersede those intermediate attempts.

Review then identified the release/claim-retirement crash window described
above. The correction keeps the checkpoint durable before retiring the marker,
and keeps capacity reserved until marker retirement and database release both
finish. Restart regressions cover retry, successor acquisition and lost checkpoint
evidence. Final-source checks for that correction are recorded below when complete.
