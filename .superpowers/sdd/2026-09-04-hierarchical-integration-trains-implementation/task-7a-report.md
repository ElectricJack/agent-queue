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
