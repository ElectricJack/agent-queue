# Task 9b1 implementation report

## Status and scope

- Starting base: `e7741233`; runtime/test commit: `443e518d`.
- No external shared-branch advance occurred while this phase was active.
- Added only `src/integration/candidates.py` and `tests/test_integration_candidates.py` before this report.
- Reused the landed batch/member/candidate/member-result/repair/lease tables. No migration or PostgreSQL scratch database was required.
- `aq prime` was run once as required and returned `task_id is required (no task in scope...)`; no queue mutation was attempted.
- Task9b2 root-to-main promotion, normalized root intents/member receipts, Task10 workflow/CI execution, cleanup, bisection/ejection, and live forge/network work remain excluded.

## Delivered behavior

- `CandidateService.build` resolves only the current leased batch for an active train-mode project and designated repository. It validates a complete immutable ordinal manifest and exact reviewed commit/tree identity.
- Empty replay is a frozen typed terminal result and creates no revision, member result, repair stage, push, or audit PR.
- Zero/one/many behavior is deterministic. Each member is applied in ordinal order through real `merge-tree`/`commit-tree`, with source author/coauthor identity and sealed review provenance retained in deterministic squash commits.
- Exact sealed source OIDs are retained under a batch/ordinal recovery namespace; partial candidate heads are pinned before application progress advances. Member rows are `pending` before Git mutation, then atomically advance the revision after the recovery pin exists.
- The already-reserved Task7a root operation starts immediately before initial construction. Clean construction never dispatches a repair agent.
- Conflicts retain exact batch/revision/ordinal/operation/stage and partial/source/base OIDs, set the batch to repairing, and call the existing Task7a dispatch seam. Repair acceptance requires the exact current stage, ancestry from the pinned partial, an exact non-merge repair commit range, and exactly the intended member path set. Pin-before-result and duplicate acceptance are restart-safe.
- Rebuild pins the old revision before locking/superseding it, rechecks the expected revision, creates N+1 for the same batch, reapplies the sealed manifest plus accepted repair lineage, and uses `bind_current_batch_subject_on` to preserve stage/start/deadline/attempts while invalidating stale success. A due stage expires and dispatches debug immediately; existing busy/configuration outcomes return typed `wait`, and human blocking returns `human_required`.
- Candidate publication uses the Task9a authenticated exact-OID transport with an observed expected-old lease. Same-head replay skips duplicate pushes. The injected `ensure_audit_pr` provider is replay-safe across crashes both before and after PR persistence; batch/revision/head/branch/PR identity is returned for CIService/Task10.

## TDD evidence by slice

### 1. Candidate state/service seam

- RED: `pytest -q tests/test_integration_candidates.py::test_empty_batch_build_replays_typed_terminal_outcome`
  - `1 failed`; `ModuleNotFoundError: No module named 'src.integration.candidates'`.
- GREEN: same command
  - `1 passed, 2 warnings in 0.54s`.

### 2. Ordered actual Git construction

- RED: `pytest -q tests/test_integration_candidates.py::test_many_members_build_in_ordinal_order_without_moving_sources`
  - `1 failed`; the deliberate nonempty seam raised `NotImplementedError`.
- GREEN: same command after the real retained-repository implementation
  - `1 passed, 2 warnings in 0.84s`.
- Actual-Git assertions cover exact ordinal trees, source refs unchanged, deterministic reviewed-head/review-evidence commit metadata, source author/coauthor identity, exact source recovery refs, and all persisted member inputs.

### 3. Conflict and preserved repair lineage

- RED: `pytest -q tests/test_integration_candidates.py::test_conflict_dispatches_and_exact_repair_advances_once`
  - `1 failed`; `ImportError: cannot import name 'CandidateRepairLineage'`.
- GREEN: same command
  - `1 passed, 2 warnings in 1.03s`.
- Follow-up boundary gate: conflict plus all then-defined restart cases
  - `5 passed, 2 warnings in 3.44s`.
- The final test also crashes after the repair recovery pin but before the accepted result, restarts, accepts once, and proves the subsequent duplicate returns `already_accepted` without a skip or double application.

### 4. Crash/restart and bounded rebuild

- RED: `pytest -q tests/test_integration_candidates.py::test_rebuild_reapplies_accepted_repair_and_preserves_budget`
  - `1 failed`; rebuilt revision returned `conflict` instead of replaying accepted repair lineage.
- GREEN: same command
  - `1 passed, 2 warnings in 1.29s`.
- RED: `pytest -q tests/test_integration_candidates.py::test_overdue_rebuild_immediately_dispatches_preserved_debug_budget`
  - `1 failed`; debug stage existed but `repair_task_id` remained `None` because dispatch had not run.
- GREEN: same command
  - `1 passed, 2 warnings in 0.97s`; the real Task7a unconfirmed handoff produces typed `wait` while the debug task/stage are durable.
- Rebuild assertions cover stale expected revision, N/N+1 states, repair parent, main-moved base, old-revision recovery ref, accepted repair reapplication, unchanged `started_at`/`deadline_at`/`attempts`, and clearing the old awaiting-completion success binding.

### 5. Ephemeral ref and audit PR

- RED: `pytest -q 'tests/test_integration_candidates.py::test_build_restarts_at_every_persisted_external_boundary[after_candidate_push]'`
  - `1 failed`; replay returned a stale `pr_url=None` after the push boundary.
- GREEN: focused restart matrix, later extended with the create-before-persist PR boundary.
  - Covers `after_member_mutation`, `after_member_progress`, `after_candidate_push`, `after_audit_pr_create`, and `after_audit_pr_write`.
- Exact assertions prove one authenticated push with the sealed branch, candidate tip, and all-zero expected-absent lease; one logical audit PR; and exact replayed candidate/PR identity.

### 6. Final safety cases

- RED: `pytest -q tests/test_integration_candidates.py::test_changed_reviewed_tree_fails_closed_as_source_moved`
  - `1 failed`; the unvalidated altered tree incorrectly returned `built`.
- GREEN: `pytest -q tests/test_integration_candidates.py::test_changed_reviewed_tree_fails_closed_as_source_moved tests/test_integration_candidates.py::test_rebuild_reapplies_accepted_repair_and_preserves_budget`
  - `2 passed, 2 warnings in 1.55s`.
- Additional focused GREEN coverage includes successful one-member replay, empty replay, reserved-path conflict with no built partial, exact source recovery pins, and stale revision.

## Final verification

- `pytest -q tests/test_integration_candidates.py`
  - `13 passed, 2 warnings in 7.45s`.
- `aq test tests/test_integration_candidates.py tests/test_integration_repair.py tests/test_integration_ci.py tests/test_git_app_auth.py tests/test_github_app.py tests/test_git_manager_async.py`
  - aq slot 1/2, 3 workers; `267 passed, 11 warnings in 18.75s`.
- `ruff check src/integration/candidates.py tests/test_integration_candidates.py`
  - `All checks passed!`
- `ruff format --check src/integration/candidates.py tests/test_integration_candidates.py`
  - both files already formatted.
- `git diff --check`
  - clean.

## Self-review

- Transaction/Git duration: no Git subprocess or forge call is held inside `db.immediate()`. Authority, state transitions, and cursor advancement use short DB transactions plus the hierarchy project lock; external mutations happen between durable checkpoints and are reconciled on replay.
- Ordering: candidate revision and member `pending` state precede commit construction; candidate/source recovery refs precede persisted ordinal advancement; all ordinals and the built head precede remote push; the old revision recovery pin precedes rebuild supersession; PR persistence follows the idempotent provider call.
- Replay identity: recovery refs derive only from batch/revision/ordinal, commit timestamps derive from sealed batch time, commit inputs/messages are frozen, the integration branch is sealed by Task8, remote pushes use exact expected-old OIDs, and PR creation is keyed by batch/branch/head.
- Task7 compatibility: construction uses only `start`, `dispatch`, `expire`, and `bind_current_batch_subject_on`; the focused repair consumer suite remains green. Budget state is never reset on rebuild.
- Task9a compatibility: publication uses `installation_token`, the frozen repository binding, `als_remote_ref`, and `apush_oid_with_app_auth`; the CI/auth/Git compatibility suite remains green.
- Task9b2 exclusion: there is no main ref push, promotion intent, member finalization, receipt fabrication, CI execution, or cleanup in this change.

## Concerns

- The service and real-Git regression file are intentionally substantial because the phase combines durable replay, Git construction, repair continuation, rebuild, and publication. Their public API remains limited to strict typed build/rebuild/repair results plus the injected audit-provider protocol.
- Configured Task7a ownership that cannot complete its handoff returns typed `wait` after durable debug activation; a later caller must retry. This is existing Task7 policy, not a fabricated success.
- Existing deprecation warnings from `pkg_resources` and `audioop` remain unrelated.
