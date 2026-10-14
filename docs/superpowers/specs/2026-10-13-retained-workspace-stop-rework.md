# Retained workspace stop rework — wise-stone-87

Status: local implementation verified; original live acceptance remains unproved.
This document does not authorize live recovery.
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
evidence. A second regression covers restoration, successful successor activation,
old callback refusal and another qualified stop on the same task; activation
retires only a completed receipt and preserves pending cleanup.

## Final candidate verification receipts

Final production source: `e0214151da8b195cc69706efb9e67bed4e4562a3`.
Ancestry checks confirm it contains `116134665b82d7d2f4551f9d3e91dd14ce2c8555`,
`18107effed870a7ee500dc6918455758fc7d7d09` and the supervisor-qualified
`5a3e0d9bdd4db04eae156c48b4f8a639d228da0a`.

- Final combined focused and affected area run: `aq test
  tests/test_task_controls.py tests/test_task_controls_api.py
  tests/test_session_runtime_units.py tests/test_selection_catalogue.py
  tests/test_claim_commands.py tests/test_session_reconciler.py
  tests/test_integration_workspace_handoff.py tests/test_worktree_acquisition.py
  tests/test_worktree_prepare.py tests/test_surface_commands.py -q` —
  **1,001 passed, one existing strict xfail, 14 warnings in 459.29 seconds**;
  `/tmp/wise87-final-combined.log`. PostgreSQL used the disposable test service
  on localhost:5534. The expected failure is the same phase-three auto-merge
  case named above.
- The two successor-claim regressions also passed independently in 22.11 seconds;
  `/tmp/wise87-repeat-claim-v2.log`. Earlier crash-retirement focused coverage
  passed 41 cases, and its full focused run passed 251 cases; the final combined
  run supersedes those earlier lifecycle receipts.
- The broader 1,536-test related area run above covers unchanged Git, reaper,
  integration ownership and preparation-diagnostic paths. No full-suite run.
- Selection catalogue generation, changed-path Ruff and `git diff --check`
  passed for the final implementation.
- Final-source `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` — all
  **20 scenarios passed**, including ordinary train delivery; exit 0,
  `/tmp/wise87-final-e2e.log`. The disposable daemon used port 18995,
  home `/tmp/wise87-retained-e2e-e0214151d`, test PostgreSQL on localhost:5534
  and database `agent_queue_e2e_wise87_retained_e0214151d`. The wrapper stopped
  its daemon after the run. Earlier candidate swarms also passed all 20 scenarios;
  this run is the final-source receipt. No real provider, external messaging or
  operator database/daemon was exercised.

Publication `b730b7d53b564a1305d4dba0c21f8e62f2220e63` adds documentation only
to the verified production source. The feedback follow-up below adds regression
tests and documentation without changing production source.
Historical incident causality, current live self-healing, foreign-host/source
qualification and the original human acceptance remain unproved. The local
repair and fixture receipts do not authorize an original-task PASS or incident
clearance. The supervisor and existing recovery owner retain those gates.

## Pending-feedback disposition

The close gate delivered additional task and session feedback before allowing
completion. Source qualification and the cancelled source wait were already
handled. The new information is attributed operator or Matter-supervisor reporting;
no foreign host, files, task, repository or API was inspected here.

The reports describe stopped historical sessions, missing claims, detached source
identified as another task's HEAD, neutral recovery refs, and canonical task refs
whose original provenance remains unproved. Neither the intended task name nor
an observed foreign HEAD qualifies a canonical task branch. Disposable negative
coverage checks both present and missing claim markers: the foreign HEAD and
neutral recovery ref stay intact, local and remote canonical refs are unchanged,
and no checkpoint or capacity release is authorized. Prior owner-release records
and zero claims do not replace the current source and writer proofs.

The authenticated manual WSL workaround reportedly allowed a batch preparation
on its first try. A later owner report describes actual ext4 task placement,
context thrash and an interrupted member merge. These reports do not qualify a
supported hybrid cutover, current writer termination or original live acceptance.
An interrupted merge cannot pass this implementation's full checkpoint capture:
unmerged index stages cause capture to fail, retaining the reservation. A real-Git
regression verifies that MERGE_HEAD, every index stage, exact index bytes, conflict
content, refs and the claim remain untouched. The recovery owner must preserve
that interrupted source; this path does not abort or replay its merge.

A workspace whose `locked_by_task_id` is null but whose agent still holds it has
no authoritative task-to-workspace binding for task-scoped stop. Stop explicitly
refuses it rather than adopting it from a historical session, path or task name.
The null-task holder regression verifies that the agent lock and generation remain,
source is unchanged and available capacity remains zero even with a stopped,
task-null session and absent claim. This is the explicit fail-closed disposition
for the requested operator-path case: task stop is not an orphan-holder release
API. Existing owner/operator reconciliation must establish exact current holder,
generation, source/checkpoint, pin/operation and termination proof before any
retirement. The reported disable/release/repoint sequence is not generalized into
an authorized recovery recipe.

The reported roughly 68-second mounted-drive failure does not identify the timed
operation. In this immutable source, `GitManager._GIT_TIMEOUT` is 120 seconds;
the cited `github.py` 60-second call creates a repository, not a demonstrated
step in the failing repair preparation. The existing bounded preparation failure
formatter records stage, exception cause chain and deepest source frame for an
empty exception. Exact installed source and that failure receipt are needed to
locate the original inner timeout. No timeout was raised on a timing inference,
and the manual WSL success does not isolate the historical cause.

Feedback verification: `aq test tests/test_task_controls.py
tests/test_selection_catalogue.py -q` passed **222 tests**, including all four
new negative cases, with 14 warnings in 148.09 seconds;
`/tmp/wise87-feedback-regressions.log`. Catalogue regeneration, changed-path Ruff
and whitespace checks passed. `src/` and `scripts/` remain byte-identical to
verified production `e0214151da8b195cc69706efb9e67bed4e4562a3`; the final-source
20/20 swarm receipt above therefore still applies. Only tests and documentation
changed after that run.

## Immutable fleet overlap and native combination disposition

Fleet correction `69a45b2594f499a7cc957f6eedd94d26953c1999` was inspected from the
local Git object store. The supervisor reports its exact hosted run 38500239625
green 16/16, final-source swarm 20/20, integration area 387, focused 20 and strict
performance nine. Those are fleet receipts, not independent validation performed
by this worker. Its PR1518 remains a separate candidate: `69a45b259` is not an
ancestor of this branch. The 1,001 tests and 20-scenario run above qualify this
branch's production source only, not a combination with fleet.

The current peer thread received immutable lifecycle source and this overlap
disposition in `msg-e9460e8128ff453da465c306c62f6227`:

- `claim_queries.activate_claim` is the semantic merge boundary. Preserve fleet's
  transaction-bound holder proof, activation rechecks and accepted-CAS-dependent
  CTE writes. Also preserve this branch's retirement of `retained_workspace_stop`
  only for a completed receipt with an older captured claim epoch, after successful
  activation. Pending receipts and durable checkpoint context stay intact. Do not
  add an unconditional stale-key deletion or discard fleet's query-budget work.
- `core.py` changes are in separate functions: retain this branch's stop/pause
  cleanup and fleet's per-kind cap and disabled-base pruning behavior.
- `worktree_manager.py` must retain checkpoint validation before stale-lock
  cleanup, plus fleet's kind-cap and disabled-base reaper protections.
- Keep fleet's exact reserved repair-successor acquisition predicate and its
  current owner/stage/fence/predecessor-instance proof. Ordinary foreign locks
  stay unavailable. This branch does not replace that integration-owner path.
- Keep fleet's `record_holder` preparation fixture correction along with this
  branch's acquisition receipts, crash/repeat-claim and source-preservation
  regressions. Regenerate selection and ownership manifests from final sources.

Both immutable candidates are retained for native integration; no sibling branch,
frozen train or batch was merged or changed here. Any actual combined production
candidate requires focused lifecycle, claim, integration-repair and acquisition
checks, the existing performance/ownership ratchets without relaxed budgets,
and its own final-source disposable swarm. Independent branch receipts cannot
serve as that combined verification. No further idle coordination wait is needed.
