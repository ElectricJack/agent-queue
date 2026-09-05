# Task 7a implementation report

Status: implemented and verified on 2026-09-05

Base: `22a7f911`

## Outcome

Task 7a now provides the durable two-stage repair state machine and its real
execution-delegate boundaries. It reuses Task 6 parent operations/episodes and
frozen policy/artifact/route data, provides Task 8's terminal-stable conn-owned
batch reservation seam, persists exact stage subjects/evidence/deadlines/writer
identity, and exposes typed `integration_repair_start` and
`integration_repair_dispatch` commands without caller clock, route, policy, or
authority inputs.

The implementation also wires actual ownership transfer, PAUSED-to-READY repair
dispatch, exact live-verifier reuse, repair-delegate runtime close, logical child
filing, and the approved retained-workspace primary-to-debug handoff. Parent
completion remains the only parent boundary that marks a successful stage passed;
`human_required` cannot complete automatically. Root green persists
`awaiting_completion` until Task 9 performs exact promotion, while Task 9's narrow
candidate-binding seam clears stale readiness without resetting the stage clock or
attempt count.

## Milestone reconciliation

1. Operation/schema foundation
   - Reused Task 6's parent operation, episode, append-only completion proof, receipt
     bindings, frozen policy/artifact/route, and operation artifact pins.
   - Added terminal-stable batch operation uniqueness and
     `reserve_batch_operation_on(conn, batch_id, now)`; a clean reservation creates
     no repair stage and pins the operation artifact.
   - Added typed writer binding, trigger/current/success subject identity, absolute
     deadline event identity, retained-workspace provenance, and normalized globally
     single-use stage evidence.
2. Durable service and commands
   - Implemented `start`, `record_result`, `expire`, `due_stages`, and
     `bind_current_batch_subject_on` with exact replay, single-count conclusive
     evidence, infrastructure-noise handling, primary/debug budgets, stable outbox
     identities, and parent/batch-specific human escalation.
   - Added strict registered contracts/adapters and scoped authorization. SESSION is
     denied; PLAYBOOK needs the explicit capability and exact operation project;
     LOCAL/SERVICE still pass persisted object/state validation.
3. Dispatch and execution delegates
   - One deterministic delegate per stage is durably linked while PAUSED before
     external handoff, then guarded to READY only under the resulting repair fence.
   - Stage 0 reuses only the exact live attached verifier and preserves verifier
     authority. Debug always uses a fresh configured route.
   - Runtime origin/fence resolution recognizes only a typed current
     `repair_delegate`; existing-verifier binding does not grant repair role.
   - Successful repair task close verifies the current clean/pushed target work,
     records `integration.repair_delegate_closed`, and bypasses legacy direct/main
     integration, receipts, parent completion, and ordinary completion playbooks.
     Failure close does not push an unreserved parent target or discard work.
4. Retained-workspace handoff
   - Fresh uncached provider stop proof precedes one CAS transaction that rechecks
     operation/stage/predecessor/session/owner/workspace, advances the fence, rebinds
     the exact workspace, persists provenance, and releases only the old exact agent
     claim. There is no free-pool interval or second writer.
   - Debug preparation requires the retained workspace, preference, fence, branch,
     and HEAD to match and deliberately performs no checkout/reset/clean/status
     normalization. Replayed/concurrent handoff reuses the one debugger.
   - Existing-verifier predecessors remain PAUSED with the parent episode/manual hold
     intact and wakeable for final verification; ordinary primary delegates enter a
     terminal BLOCKED handoff state.
5. Filing and completion integration
   - A parent repair delegate defaults to its true parent; batch repair requires the
     explicit project-root path. Quota and discovered-from provenance remain on the
     delegate.
   - Parent repair and ordinary running self-parent filing prove the writer workspace
     clean, committed, and exactly pushed, then use current HEAD as the child's
     immutable base. The authoritative transaction rechecks current stage,
     ownership/workspace binding, and locks parent generation; generation advances
     without changing repair start/deadline/attempts.
   - Guarded parent completion allows active or escalated/debug repair, marks the
     current stage passed only after actual completion, and refuses exhausted
     `human_required` state.
6. Phase boundary
   - No `resolution_reserved` state, conflict-reservation receipt behavior, playbook
     fixture, invocation context, or frozen-route admission was added. Those remain
     Task 7b/7c responsibilities.

## TDD evidence

Representative RED observations (all failed for the intended missing behavior):

- `pytest -q tests/test_integration_repair.py -x`
  - Initial collection failed because `src.integration.repair` did not exist.
- `pytest -q tests/test_integration_repair.py -k 'primary_dispatch_reuses or dispatch_persists' -x`
  - Dispatch initially lacked the handoff callback and then exposed profile-FK and
    manual-pause wake requirements.
- `pytest -q tests/test_integration_repair.py -k 'debug_exhaustion_blocks or debug_dispatch_retains' -x`
  - Failed because repair exhaustion/handoff BLOCKED transitions lacked terminal
    metadata (`None` instead of the repair transition context).
- `pytest -q tests/test_integration_repair.py -k debug_dispatch_retains -x`
  - Failed because retained preparation returned `/tmp/retained` where the caller
    requires branch `aq/parent`; this would have overwritten `task.branch_name`.
- `pytest -q tests/test_integration_repair.py -k running_repair_delegate_files -x`
  - Failed because a stage remained able to file after its branch owner changed.
- First affected-area run (exact command below)
  - `1 failed, 184 passed, 3 skipped`; the failure was an old transfer fixture using
    a parent/episode pair rejected by Task 6's composite FK. The fixture was corrected
    to create its exact parent episode and to include Task 7a's typed writer binding.

Focused GREEN progression:

- `pytest -q tests/test_integration_repair.py -k 'primary_dispatch_reuses or dispatch_persists' -x`
  - `2 passed, 9 deselected` at that milestone.
- `pytest -q tests/test_integration_contracts.py -x`
  - `8 passed`.
- `pytest -q tests/test_integration_parent_completion.py -k parent_completion_pins_exact_verification_for_rollover -x`
  - `1 passed, 18 deselected`.
- `pytest -q tests/test_integration_repair.py -k 'debug_exhaustion_blocks or debug_dispatch_retains' -x`
  - `2 passed, 16 deselected` after terminal metadata was added.
- `pytest -q tests/test_integration_repair.py -k running_parent_files_child -x`
  - `1 passed, 18 deselected`.
- `pytest -q tests/test_integration_repair.py -k debug_dispatch_retains -x`
  - `4 passed, 18 deselected`; real Git states covered tracked edits, untracked files,
    unmerged index entries, and an unpushed commit, with status/index/HEAD/file bytes
    unchanged after exact debug preparation.
- `pytest -q tests/test_integration_repair.py -k running_repair_delegate_files -x`
  - `1 passed, 21 deselected` after ownership/workspace revalidation.
- `pytest -q tests/test_integration_transfer_commands.py -x`
  - `15 passed` after exact Task 6 episode and Task 7a writer-kind fixture repair.
- `pytest -q tests/test_integration_repair.py -x`
  - Final focused result: `22 passed, 3 warnings in 7.80s`.

## Dialect, final test, and lint evidence

- `POSTGRES_TEST_DSN=postgresql://integration_test:integration_test@127.0.0.1:16833/postgres pytest -q tests/test_migration_repair_stages.py -m migration -x`
  - `2 passed, 2 warnings in 2.65s`.
  - Covers SQLite and a uniquely-created disposable PostgreSQL database through
    upgrade -> downgrade -> upgrade. The preinitialized `integration_test` database
    and operator database were not used.
- `aq test tests/test_integration_repair.py tests/test_integration_contracts.py tests/test_integration_parent_completion.py tests/test_integration_transfer_commands.py tests/test_worker_filing.py tests/test_integration_mode.py tests/test_integration_ownership.py tests/test_integration_hierarchy.py -x`
  - Final result: `214 passed, 3 skipped, 11 warnings in 42.51s`.
- `changed_py=$(git diff --name-only -- '*.py'; git ls-files --others --exclude-standard -- '*.py'); ruff check $changed_py && git diff --check`
  - `All checks passed!`; `git diff --check` exited 0.

All warnings are pre-existing environment deprecations from `pkg_resources`, legacy
`zope` namespace declarations, and Python's `audioop` import through Discord. The
three skips belong to existing environment-dependent affected-area tests; no Task 7a
test was skipped.

## Files

- Schema/service: `migrations/versions/7a1d5e9f0b2c_durable_repair_stages.py`,
  `src/database/tables.py`, `src/database/base.py`,
  `src/database/queries/integration_state_queries.py`,
  `src/database/queries/task_queries.py`, `src/integration/repair.py`.
- Contracts/adapters: `src/commands/contracts/integration.py`,
  `src/commands/integration_commands.py`, `src/commands/task_commands.py`,
  `src/git/manager.py`.
- Runtime/completion: `src/integration/hierarchy.py`,
  `src/integration/parent_completion.py`, `src/orchestrator/workspace.py`,
  `src/orchestrator/execution.py`.
- Tests: `tests/test_integration_repair.py`,
  `tests/test_migration_repair_stages.py`, `tests/test_integration_contracts.py`,
  `tests/test_integration_parent_completion.py`,
  `tests/test_integration_transfer_commands.py`.
- Durable design: `docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-design.md`
  (§9.1, §9.2, §10.2, and the corresponding retained-workspace invariant wording).

## Self-review and concerns

- Verified the distinction between `writer_kind=repair_delegate` and
  `writer_kind=existing_verifier` across transfer, filing, runtime resolution, close,
  and orphan-pause protection.
- Verified root clock-only start does not dispatch a task; root exact-current green
  waits for deterministic promotion; parent green remains deadline-bounded through
  guarded completion.
- Verified last-completion fields and append-only
  `integration_parent_operation_completions` behavior were not replaced or relaxed.
- Verified terminal repair BLOCKED contexts cannot be undone by ordinary graph
  recovery, and batch human escalation keeps the integration ownership lease.
- No operator DB, protected DB environment, project enablement, external push, or PR
  operation was performed.

Remaining cross-phase dependencies are intentional, not Task 7a gaps: Task 9 must call
the server-owned batch-subject invalidation seam when it binds a new candidate and must
mark root success passed only on exact promotion; Task 7b owns resolution reservations
and receipts; Task 7c owns the playbook fixture, invocation context, and frozen-route
admission. Independent phase review is the next gate.

## Commit

- `0650d4e8` — `feat(integration): add durable repair stages`
- This report's final hash is a documentation-only follow-up commit and is reported to
  the controller in the final task response.

## Fix round 1/5 checkpoint (2026-09-05)

Slice 1 addresses review findings 3, 5, 6, and 7. Repair filing and close now bind
the exact attached session instance, workspace, role-bearing ownership fence, active
operation, stage, and writer kind inside the authoritative transaction. Retained
handoff accepts only the frozen writer-kind/owner-role pair, and stopped proof is
bound to both the requested stopped state and the original session instance token.
Repair delegate completion and its lifecycle-only outbox fact now commit atomically;
a stale ownership/session CAS leaves the task running and emits no close fact.

RED evidence:

- `pytest -q tests/test_integration_repair.py -k 'retained_handoff_rejects or retained_stop_proof_rejects or running_repair_delegate_files or real_task_close' -x`
  - Successive focused REDs exposed: mismatched role handoff dispatching, replacement
    session instance accepted as stopped, reserved/wrong-session repair filing
    accepted, and ownership-fence movement during close still completing the task.

GREEN evidence:

- `pytest -q tests/test_integration_repair.py -k 'batch_repair_delegate_can_file or retained_handoff_rejects or retained_stop_proof_rejects or running_repair_delegate_files or real_task_close' -x`
  - `6 passed, 19 deselected, 3 warnings in 3.38s`.

The warnings are the existing environment deprecations from `pkg_resources`, the
legacy `zope` namespace declaration, and Python's `audioop` import through Discord.
No schema change was needed for this slice.

Slice 2 addresses review findings 1 and 4. A conn-owned parent-subject binder now
pins the exact checkpoint generation and transaction-proved current HEAD, preserves
the stage ordinal/start/deadline/attempts on both replay and movement, preserves
readiness on exact replay, and invalidates stale success only when the subject
actually changes. Repair filing and delegate close invoke it within their
authoritative transactions, so debug inherits the advanced head. The active
`existing_verifier` also receives the same logical parent filing scope only while
its attached verifier-role owner/session/workspace and operation identity agree.

RED evidence:

- `pytest -q tests/test_integration_repair.py -k 'parent_subject_binding or primary_dispatch_reuses_only' -x`
  - First RED: `AttributeError` for the missing parent binder. The verifier filing
    regression then exposed the test's attempted mutation of append-only evidence;
    the corrected test inserts new-generation evidence instead.

GREEN evidence:

- `pytest -q tests/test_integration_repair.py -k 'running_repair_delegate_files or real_task_close or parent_subject_binding or primary_dispatch_reuses_only' -x`
  - `4 passed, 22 deselected, 3 warnings in 2.80s`.

Slice 3 addresses review findings 2 and 8. Dispatch replay now recognizes an exact
repair-owned delegate after scheduler launch only when its task is assigned/running
and its live session plus enabled task-locked workspace match the attached ownership
record and fence. It returns `already_dispatched` without waking or transferring.
The batch subject binder now compares the durable subject before mutation, so an
exact current-revision replay preserves green readiness and evidence; only a new
revision invalidates them while retaining the original stage budget.

RED evidence:

- `pytest -q tests/test_integration_repair.py -k 'root_green_waits or dispatch_persists_paused' -x`
  - First RED: exact batch binder replay changed `awaiting_completion` back to
    `active` and cleared its success evidence.

GREEN evidence:

- Same command after implementation: `2 passed, 24 deselected, 3 warnings in 1.23s`.

Slice 4 addresses review findings 9, 10, and 11 and completes finding 1's retained
HEAD requirement. Record/timeout actions are closed `Literal` sets. Start now
distinguishes invalid caller trigger/current subject (`stale`) from corrupt persisted
policy, route/artifact/check, parent checkpoint, batch revision, and target relations
(`invariant_error`) without dereferencing absent rows or leaking exceptions.

Each stage dossier now has the actual manifest identity, exact branch SHA, matching
receipts, required checks/artifact, repair tips, failed-check payloads, evidence/log
references, evidence-derived diagnostic classifications, workflow commands attempted,
and the persisted elapsed/attempt budget. Every accepted evidence record updates that
dossier in the same transaction. Debug receives a top-level copy plus the complete
primary snapshot and its own budget. Retained handoff rewrites debug's exact starting
SHA and task description from the stopped-session HEAD proof while preserving the
workspace contents.

RED evidence:

- `aq test tests/test_integration_repair.py tests/test_integration_contracts.py -k 'start_reports_corrupt or primary_attempt_exhaustion or repair_contracts_expose' -x`
  - `2 failed`: untyped actions accepted arbitrary strings and corrupt policy returned
    `stale`. The first implementation rerun exposed an ordering bug that read the
    checkpoint variable before its locked query; the next TDD iteration corrected it.

GREEN evidence:

- `aq test tests/test_integration_repair.py tests/test_integration_contracts.py -k 'start_reports_corrupt or primary_attempt_exhaustion or repair_contracts_expose or debug_dispatch_retains' -x`
  - `9 passed, 11 warnings in 5.61s`.

Slice 5 addresses review finding 12. Fault injection now covers loss before the
retained CAS, after the ownership/workspace CAS but before wake, and after the READY
commit; each retry converges to exactly one debug task, one fenced owner, and the
same retained workspace. These were targeted verification additions and were green
against the completed CAS/replay implementation.

The scheduler regression drives the production `_execute_task` path through
workspace acquisition, retained preparation, session-row creation, ownership
attachment, fake-provider startup, and a post-launch dispatch replay. It proves the
exact retained workspace and branch HEAD, index, tracked edits, and untracked bytes
survive unchanged while ownership becomes attached to the real running session.

GREEN evidence:

- `pytest -q tests/test_integration_repair.py -k 'retained_dispatch_recovers or scheduler_launches_retained' -x`
  - `4 passed, 29 deselected, 3 warnings in 3.77s`.

## Fix-round finding reconciliation

All twelve Important findings from `task-7a-review.md` map to the following shipped
code and regression evidence:

1. Parent subject/head progression: `RepairService.bind_current_parent_subject_on`,
   conn-owned filing/close calls, and retained proof rebinding; covered by
   `test_parent_subject_binding_preserves_budget_and_replay_readiness`, both real
   filer tests, repair close, and retained-workspace tests.
2. Post-launch dispatch replay: exact assigned/running task plus attached live
   session/workspace/fence recognition in `RepairService.dispatch`; covered by the
   extended paused-before-handoff test and actual scheduler launch test.
3. Exact invoking mutation authority: `get_repair_filing_scope` and both filing and
   close CAS bindings include operation/stage/writer/session instance/workspace/fence;
   covered by reserved, wrong-session, moved-fence, and close-race assertions.
4. Existing-verifier logical filing: the scope query accepts only the exact
   `existing_verifier`/`verifier` pair and attached live resources; covered by the
   verifier reuse/fresh-head filing regression.
5. Writer-kind/role correlation: `_writer_role_matches` is used at retained recheck;
   both corrupt cross-pairs are rejected by the parameterized handoff test.
6. Token-bound stop proof: `update_session_instance` binds desired stop and the exact
   process instance before and after provider confirmation, and retained CAS rechecks
   that token; covered by replacement-instance rejection.
7. Atomic close fact: `RepairService.complete_delegate` closes under the exact CAS
   and enqueues `integration.repair_delegate_closed` in the same transaction; covered
   by the ownership-race and single-outbox-row close test. The fact is lifecycle-only,
   never delivery or check success.
8. Batch binder replay: `bind_current_batch_subject_on` mutates only a genuinely new
   subject; covered by same-revision readiness preservation and new-revision budget
   retention in the root-green test.
9. Debug dossier: start/evidence/binder/handoff paths persist manifest, receipts,
   branch SHA, repair tips, failed checks, evidence log references, diagnostic
   classifications, attempted workflows, and stage budgets; primary exhaustion and
   retained HEAD tests assert the debug snapshot.
10. Typed actions: repair-record and timeout value models use exact `Literal` sets;
    contract tests accept every emitted action and reject an arbitrary action.
11. Corrupt starts: frozen policy/route/target/checkpoint/revision corruption maps to
    deterministic `invariant_error`; parameterized tests cover policy, missing parent
    checkpoint, and missing batch revision without an uncaught exception.
12. Crash/actual launch coverage: three injected dispatch-loss boundaries converge on
    replay, and the real scheduler path proves retained acquisition, preparation,
    session creation, ownership attachment, provider start, post-launch replay, and
    byte/index/HEAD preservation.

## Fix-round final verification

- `pytest -q tests/test_integration_repair.py -x`
  - `33 passed, 3 warnings in 12.85s`.
- `aq test tests/test_integration_repair.py tests/test_integration_contracts.py tests/test_integration_parent_completion.py tests/test_integration_transfer_commands.py tests/test_worker_filing.py tests/test_integration_mode.py tests/test_integration_ownership.py tests/test_integration_hierarchy.py tests/test_session_commands.py tests/test_workspace_attachments.py -x`
  - Final affected-area gate: `330 passed, 6 skipped, 11 warnings in 44.72s`.
- After the final exact-resource tightening:
  `pytest -q tests/test_integration_repair.py -k 'dispatch_persists_paused or primary_dispatch_reuses_only or running_repair_delegate_files or real_task_close or scheduler_launches_retained' -x`
  - `5 passed, 28 deselected, 3 warnings in 4.52s`.
- `ruff check src/commands/contracts/integration.py src/commands/session_commands.py src/commands/task_commands.py src/database/base.py src/database/queries/integration_state_queries.py src/database/queries/session_queries.py src/integration/repair.py src/orchestrator/execution.py src/orchestrator/workspace.py tests/test_integration_contracts.py tests/test_integration_repair.py`
  - `All checks passed!`.
- `git diff --check`
  - Exited 0 with no output.

Warnings remain the explicitly deferred environment deprecations from `pkg_resources`,
legacy `zope` namespace declarations, and Python's `audioop` import through Discord.
The six skips are existing environment-dependent affected-area tests. No fix-round
test was skipped. This round made no schema amendment, so no new migration cycle was
required or run.

Fix-round files are the repair service, integration contract, task/session command,
database session/integration-state query, execution/workspace orchestration, and the
repair/contract tests listed in the lint command above, plus this report. No Task 7b
conflict receipt, Task 7c playbook/invocation/frozen-route admission, or Task 9
candidate rebuild behavior was added. No operator database, protected database
environment, project enablement, external push, or PR action was performed. The
scoped local fix commit hash is reported to the controller after commit creation.

## Fix round 2 — exact debug dossier lineage and late receipts

The sole open rereview finding is addressed. Repair writer Git inspection now occurs
outside database transactions and returns a server-generated proof containing the
persisted subject SHA, proved workspace HEAD, and the complete
`git rev-list --reverse <subject>..<HEAD>` sequence. Clean pushed filing/close and
retained unpushed handoff feed that proof into the existing locked operation,
stage, session, workspace, owner, and fence CAS. A subject change without a writer
proof no longer mislabels a bare tip as an exact repair commit.

Parent subject binding appends the proof's commits in order, rejects a proof bound to
an older subject, preserves an exact-current replay without duplicating or dropping
context, and leaves the stage-start manifest and elapsed/attempt budgets frozen.
Receipts matching the frozen parent operation/episode (or batch) are refreshed at
debug activation and again at retained dispatch immediately before the debug task is
woken, so receipts finalized during primary repair are present in the launched
debugger's persisted dossier and task description.

RED evidence:

- `pytest -q tests/test_integration_repair.py -k debug_dossier_refreshes_exact -x`
  - `1 failed, 33 deselected, 3 warnings in 1.36s`; the binder rejected the new
    `commit_proof` argument, demonstrating the missing lineage contract.

GREEN evidence:

- Same command after the first service/handoff implementation:
  `1 passed, 33 deselected, 3 warnings in 1.42s`.
- After changing the regression to drive the production workspace stop confirmer
  and real Git proof collector: same command initially returned `busy`; moving the
  active-stage-independent primary lookup to the handoff proof boundary produced
  `1 passed, 33 deselected, 3 warnings in 1.27s`.
- Clean filing/close compatibility:
  `pytest -q tests/test_integration_repair.py -k 'primary_dispatch_reuses_only_exact_live_attached_verifier or running_repair_delegate_files_real_child or real_task_close_bypasses' -x`
  - `3 passed, 31 deselected, 3 warnings in 2.68s`.

The new regression creates a real repository with three ordered, unpushed repair
commits, finalizes a matching receipt after primary start, and uses the production
stop confirmer plus retained handoff. It asserts all three commits and the late
receipt reach the debugger before launch; exact-current dispatch/binder replay is
idempotent; an old snapshot is rejected; and manifest membership plus both stage
budgets are unchanged.

Final focused verification:

- `pytest -q tests/test_integration_repair.py -k 'dossier or retained or exhaustion or repair_delegate_files or real_task_close or primary_dispatch_reuses' -x`
  - First run found one negative-test compatibility regression (`10 passed, 1
    failed, 21 deselected`): a missing repair-stage row prevented the replacement
    token race from reaching provider confirmation. The scope check was moved after
    stop confirmation without weakening the final token CAS.
  - Final run: `13 passed, 21 deselected, 3 warnings in 7.67s`.
- `ruff check src/commands/task_commands.py src/database/queries/integration_state_queries.py src/integration/hierarchy.py src/integration/repair.py src/orchestrator/execution.py src/orchestrator/workspace.py tests/test_integration_repair.py`
  - `All checks passed!`.
- `git diff --check`
  - Exited 0 with no output.

The three warnings are the existing deferred environment deprecations for
`pkg_resources`, the legacy `zope` namespace declaration, and Discord's `audioop`
import. No schema changed, so no migration cycle was required. Changed code is
limited to repair dossier/subject binding, the shared Git proof helper, repair scope,
and the existing filing/close/workspace handoff callers, plus the focused repair
regression and this report. No Task 7b, Task 7c, or Task 9 behavior was added. No
operator database, protected database environment, external push, or PR was touched.
