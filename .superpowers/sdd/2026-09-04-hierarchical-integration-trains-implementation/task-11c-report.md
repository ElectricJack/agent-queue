# Task 11c report — operational backend

Status: DONE_WITH_CONCERNS

Base reviewed: `1bf8d00888e20cbe93491fbbef9ad83434c15b57`.
Scope followed: `operational-scope-override.md`; protection inspection, scratch
probes, isolation certification, broad recovery/race matrices, final branch review,
production mutation, and migration-copy work were not performed.

## Delivered checklist

- [x] Functional, read-only preflight over the designated exact GitHub HTTPS
  repository, typed parent/root policy, exact stored artifacts and ready project
  activations, explicit primary/debug/verifier profiles and intelligence classes,
  pull-request review mode, and historical merge gates.
- [x] Status returns current `generation`, effective/desired modes, draining,
  structured blockers, exact `blocker_digest`, and certification
  `{"status":"not_performed", ...}`.
- [x] Hierarchy-locked generation-CAS cutover commits mode, desired mode, drain
  state, transition audit, waiver consumption/applicability, legacy suppression,
  and schedule projection atomically. External functional checks precede the lock;
  locked database identities/fingerprint are revalidated.
- [x] Observe and hierarchy flush are read-only eligibility; disabled and draining
  flushes do not schedule; train flush coalesces the existing durable request.
  `IntegrationScheduler.mark_due` enforces mode/drain before creating a schedule.
- [x] Disable with active frozen work retains the effective managed mode, sets
  desired disabled + draining, blocks new schedules/seals, suppresses catch-up,
  and completes automatically from the bounded daemon reconciliation loop before
  restoring the recorded legacy routing policy.
- [x] Legacy `pr_merge` is rejected before filesystem/forge/CI for hierarchy/train.
  Project merge sweep is filtered at activation source and durable destination
  selection; only `default-pipeline/per-branch-final-review` is filtered in engine
  dispatch. Frozen operation-owner destinations remain eligible.
- [x] Re-enabling an activation cannot bypass suppression. The ordinary
  `per-task-review` path remains live.
- [x] Managed `task.completed` -> shipped default pipeline -> reused reviewer task
  -> reviewer completion -> exact graph/head/generation-bound review evidence ->
  delivery promotion is covered without ordinary `pr_merge`.
- [x] Status is same-project readable; flush retains explicit project/capability
  authority; enable, waive-history, resume, abort, retry-cleanup, and sensitive
  project configuration require LOCAL authority even for elevated sessions.
  Rollout fields are explicitly rejected by `edit_project`; deletion refuses live
  mode, draining, or active integration work.
- [x] Human resume/abort resolve owning project server-side, require
  `human_required`, and fail closed on unresolved candidate/ref/resolution,
  attestation, promotion, live-writer, and irreversible cleanup facts. Resume uses
  the existing `integration.repair_exhausted` dispatch seam; abort changes no Git
  state and retains audit/work. Cleanup retry only requeues the exact existing safe
  identities and never clears an irreversible marker.
- [x] Actual orchestrator construction supplies GitHub App repository binding,
  playbook runtime, attestation, promotion, cleanup, Git transport, profile, and
  intelligence-class functional checks to the control service and installs drain
  reconciliation in the sole integration loop.
- [x] Typed command contracts and generic adapters are registered for every
  operational command; structured blockers survive the generic result envelope.

## Public command handoff for Task 11d

All commands use the existing generic command envelope.

- `integration_status(project_id: str)` -> `status | not_found`; value includes
  `project_id`, `effective_mode`, `desired_mode`, `generation`, `draining`, `ready`,
  `rollout_ready`, `blockers`, `blocker_digest`, and `certification`.
- `integration_flush(project_id: str)` -> `due | not_due | coalesced | eligibility |
  disabled | draining | not_found`; a due/coalesced result carries request identity,
  sequence, trigger, requested time, and next due time.
- `integration_enable(project_id: str, mode: disabled|observe|hierarchy|train,
  expected_generation: int, reason: str, waiver_id?: str)` -> `enabled | draining |
  blocked | stale | not_found`; blocked/stale results retain blockers/digest where
  available, and success returns new generation/modes/draining.
- `integration_waive_history(project_id: str, reason: str, blocker_digest: sha256)` ->
  `waived | stale | not_waivable | not_found`; success returns immutable `waiver_id`.
- `integration_resume(operation_id: str)` -> `resumed | ambiguous | invalid_state |
  not_found`; success returns project, state, stage, and deadline.
- `integration_abort(operation_id: str, reason: str)` -> `aborted | ambiguous |
  invalid_state | not_found`.
- `integration_retry_cleanup(batch_id: str)` -> `requeued | ambiguous |
  nothing_to_retry | not_found`; success returns exact count.
- Guarded configuration remains `edit_project(project_id,
  integration_repository_id?, hierarchical_integration_policy?,
  expected_integration_generation, reason?)`. No rollout mode field is accepted.

The CLI should show stale generation as returned and require the operator to pass an
explicit new `expected_generation`; status is the source of the current generation.
History waiver must use the current status `blocker_digest` exactly.

## RED/GREEN evidence

Initial focused RED:

- `pytest -q tests/test_integration_operational_controls.py -x` -> collection error,
  `ModuleNotFoundError: No module named 'src.integration.controls'`.
- Successive focused REDs established unknown operational commands, absent guarded
  project outcomes, unsuppressed merge activation, and absent pre-forge managed merge
  refusal before each implementation slice.

Focused GREEN milestones:

- `pytest -q tests/test_integration_operational_controls.py -x` -> `4 passed,
  2 warnings in 1.19s`, then `5 passed in 1.38s` after drain coverage.
- Focused authority and sensitive-edit nodes -> `1 passed` each.
- `pytest -q tests/test_activation_source_scope.py` -> `7 passed`.
- Managed `pr_merge` parametrized nodes -> `2 passed`.
- Engine exact-rule suppression node -> `1 passed, 3 warnings in 1.07s`.
- Durable destination suppression node -> `1 passed, 3 warnings in 1.16s`.
- Human ambiguity/resume/abort node -> `1 passed, 3 warnings in 1.18s`.
- Exact-digest single-use waiver/rollback node -> `1 passed, 3 warnings in 1.16s`.
- Cleanup exact retry/prewrite node -> `1 passed, 3 warnings in 1.13s`.
- Managed event-to-review-to-promotion node -> `1 passed, 3 warnings in 2.55s`.
- Release catch-up + drain nodes -> `2 passed, 3 warnings in 1.54s`.
- `pytest -q tests/test_integration_schedule.py` -> `15 passed, 3 warnings in 3.04s`.
- `pytest -q tests/test_integration_controls.py` -> `8 passed, 2 warnings in 1.35s`.
- `pytest -q tests/test_integration_contracts.py` -> `10 passed, 3 warnings in 0.86s`.
- `pytest -q tests/test_api_scope.py` -> `23 passed, 3 warnings in 0.80s`.
- Daemon service/orchestrator wiring nodes -> `2 passed, 3 warnings in 1.69s`.

Affected-area gate:

`aq test tests/test_integration_operational_controls.py tests/test_integration_controls.py tests/test_integration_contracts.py tests/test_integration_schedule.py tests/test_integration_service.py tests/test_integration_cleanup.py tests/test_activation_source_scope.py tests/test_integration_outbox.py tests/test_v2_engine.py tests/test_integration_review_evidence.py tests/test_pr_merge_command.py tests/test_api_scope.py tests/test_orchestrator.py`

Result: `362 passed, 1 skipped, 1 failed in 49.45s`. The sole failure was a test
principal that granted `integration_enable` but omitted `integration_resume`, so the
existing capability gate rejected before the intended LOCAL-only handler assertion.
After correcting only that fixture, the parent-approved focused rerun
`pytest -q tests/test_integration_operational_controls.py::test_public_control_authority_keeps_enable_local_and_status_project_scoped`
returned `1 passed, 3 warnings in 1.09s`. The warnings are inherited
`pkg_resources`/namespace and Python `audioop` deprecations; the skip is inherited.

Final static checks:

- `python3 -m compileall -q` on the changed runtime modules -> exit 0.
- `ruff check` on all changed source/test paths -> `All checks passed!`.

## Concerns and deferred work

- Security/protection inspection, positive/negative scratch probes, transport/worker/
  control-plane isolation certification, and broad recovery/crash/PostgreSQL race
  matrices are explicitly deferred by the operational override. Status never reports
  them certified.
- SQLite-to-PostgreSQL database-copy compatibility is a separately assessed backend
  switch limitation; this implementation does not touch the copier and supports the
  same-backend upgrade path.
- Human recovery intentionally fails closed while a writer or irreversible/unresolved
  external fact remains. It does not add a force-clear or expiry-based bypass.
- No live provider probe, production enablement, operator DB/config mutation, push,
  PR, or main merge was performed.
