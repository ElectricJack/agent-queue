# Task 9b1 fix round 3 report

## Result

Implemented the two Critical and two Important corrections from
`task-9b1-rereview-2.md` and `task-9b1-fix-3-brief.md`, starting from
`FIX_BASE=96bf6a1c`.

Runtime/test/migration commit: `d695789e` (`fix(integration): close candidate
handoff authority windows`). No external branch advance occurred before that
commit.

## Delivered corrections

- Rebuild now rechecks and locks unresolved current-revision mutation claims in
  the same hierarchy-first transaction that supersedes revision N and installs
  N+1. Existing branch-transfer and repair-stage expiry claim blockers remain
  covered by the affected-area tests.
- Candidate ref claims cover the Git transport's 120-second bound plus a
  15-second reservation margin. A second transaction immediately before push
  requires at least the transport bound plus a 5-second margin on both the
  exact claim and project lease. A short/expired writer never pushes.
- Observation-only recovery authenticates and reads remote state outside SQL,
  then under the hierarchy lock marks an exact desired tip applied or removes
  an expired claim proven unapplied at its exact expected tip. An unexpected
  remote remains reserved and blocking. This path does not require a live
  `_locked_state()` lease.
- Repair handoff confirmation is obtained outside SQL. One subsequent local
  hierarchy transaction revalidates batch/revision, project/repository, exact
  operation episode/active stage, lease and repair fence; transfers the branch;
  and persists the collector owner/fence on the resolution. No network await is
  held by that transaction. Fresh replay uses the persisted collector fence and
  never retries the obsolete repair fence.
- Same-revision final publication freezes `expected_old_sha` from the current
  revision's applied repair handoff first, partial mutation second, with stable
  tie-breaking. Actual Git conflict -> repair -> accept -> resume revision N ->
  PR is exercised before the retained N+1 repaired-lineage assertions.
- Repair reservations persist an absolute, strict-resolved workspace path.
  Push compares the current server-owned scope's normalized path and uses only
  the persisted path.
- Migration `46f910d0dce6` (generated from actual head `e1eab6dbc186`) adds
  immutable workspace-path provenance, an explicit qualified-vs-legacy target
  kind, and one-time durable handoff owner/fence fields with dual-dialect
  constraints/guards. The predecessor migration now preflights episode/stage
  deadline/project joins and preserves a legacy pushed row's actual integration
  branch rather than fabricating a repair ref. The new migration preflights the
  workspace/target join before DDL.

## RED evidence

1. Atomic rebuild interleaving:

   `pytest -q tests/test_integration_candidates.py::test_rebuild_rechecks_claim_inside_supersession_transaction tests/test_integration_candidates.py::test_mutation_claim_covers_transport_bound_plus_margin tests/test_integration_candidates.py::test_instance_bound_repair_reservation_push_and_accept_once -x`

   First node failed because rebuild superseded revision 0 after the test
   inserted a live claim between the preliminary check and the supersession
   transaction.

2. Lease duration:

   `pytest -q tests/test_integration_candidates.py::test_mutation_claim_covers_transport_bound_plus_margin -x`

   `1 failed`; assertion showed committed claim duration was 60 seconds, below
   the required 125-second minimum.

3. Workspace path rebind:

   `pytest -q 'tests/test_integration_candidates.py::test_instance_bound_repair_reservation_push_and_accept_once[exact-accepted-0]' -x`

   `1 failed`; same workspace ID with a rebound path reached Git and failed as
   “not a git repository” instead of being rejected by writer authorization.

4. Final combined gate initially exposed a deterministic expected-old defect:

   `aq test tests/test_integration_candidates.py tests/test_integration_ownership.py tests/test_integration_repair.py tests/test_git_app_auth.py`

   `5 failed, 100 passed`; all five repair crash variants selected the partial
   mutation when equal test-clock timestamps tied. Adding repair-handoff-first,
   partial-second, ID-stable ordering corrected the root cause.

## Focused GREEN evidence

- Atomic rebuild blocker + 135-second claim + normalized workspace rejection:
  first two nodes passed; the repair node then progressed beyond the new checks
  to expose the same-revision publication assertion addressed below.
- Expired writer and observation-only convergence:

  `pytest -q tests/test_integration_candidates.py::test_expired_writer_observes_without_starting_push tests/test_integration_candidates.py::test_remote_success_after_lease_expiry_is_observation_reconciled -x`

  `2 passed`.
- Old 60-second boundary:

  `pytest -q tests/test_integration_candidates.py::test_mutation_remains_authorized_beyond_old_sixty_second_window -x`

  `1 passed`.
- Handoff crash matrix/fresh-service replay:

  `pytest -q tests/test_integration_candidates.py -k 'instance_bound_repair_reservation_push_and_accept_once and exact and accepted and 0' -x`

  `5 passed, 34 deselected`, covering baseline plus
  `after_handoff_reservation`, `after_handoff_transfer`,
  `after_handoff_push`, and `before_repair_acceptance`.
- Stage/transfer equivalents:

  `pytest -q tests/test_integration_candidates.py::test_live_external_claim_blocks_rebuild_and_branch_transfer tests/test_integration_candidates.py::test_rebuild_rechecks_claim_inside_supersession_transaction -x`

  `2 passed`.
- Focused repair/final expected-old correction:

  `pytest -q 'tests/test_integration_candidates.py::test_instance_bound_repair_reservation_push_and_accept_once[exact-accepted-0-None]' -x`

  `1 passed`.
- Candidate file before the final tie-break assertion was added:

  `pytest -q tests/test_integration_candidates.py -x`

  `39 passed`.

## Migration evidence

Fresh final dual-backend command:

`POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres' pytest -q tests/test_migration_candidate_mutation_claims.py -m migration -x`

Result: `2 passed`. Both SQLite and the uniquely named disposable PostgreSQL
scratch database exercised upgrade/downgrade/upgrade with a seeded pushed legacy
resolution. The row retained `target_branch == branch`,
`target_kind == legacy_integration`, and its canonical workspace path. Setting
the joined stage deadline to NULL produced the deliberate
`irreconstructible legacy authority` refusal on both dialects. The PostgreSQL
test dropped its scratch database in `finally`; neither `postgres`,
`integration_test`, nor an operator database was migrated.

`alembic heads` returned exactly `46f910d0dce6 (head)`.

## Final verification

`aq test tests/test_integration_candidates.py tests/test_integration_ownership.py tests/test_integration_repair.py tests/test_git_app_auth.py`

Result: `105 passed, 11 warnings in 37.56s` using aq slot 1 and three workers.

`ruff check src/integration/candidates.py src/integration/ownership.py src/database/tables.py tests/test_integration_candidates.py tests/test_migration_candidate_mutation_claims.py migrations/versions/e1eab6dbc186_candidate_durable_mutation_claims.py migrations/versions/46f910d0dce6_candidate_handoff_workspace_.py`

Result: `All checks passed!`.

`python -m py_compile` on the three changed runtime/schema Python files and both
migrations completed with exit 0. `git diff --check` completed with exit 0.

## Changed files

- `src/integration/candidates.py`
- `src/integration/ownership.py`
- `src/database/tables.py`
- `migrations/versions/e1eab6dbc186_candidate_durable_mutation_claims.py`
- `migrations/versions/46f910d0dce6_candidate_handoff_workspace_.py`
- `tests/test_integration_candidates.py`
- `tests/test_migration_candidate_mutation_claims.py`

## Self-review and scope

- External token/ref/Git/confirmation work occurs only after the claim or
  handoff intent transaction commits. The only operations inside hierarchy
  transactions are reads, row locks, inserts/updates/CAS, and local ownership
  transfer.
- Observation-only recovery cannot push. A live claim owned by another process
  is observed but retained; an expired expected-old claim requires a later
  invocation after its safe release before any new mutation can begin.
- Handoff transfer and collector-fence persistence are atomic. Resolution guard
  logic permits exactly the initial NULL-to-bound handoff write and then makes
  it immutable.
- Final expected-old selection is explicit rather than timestamp-dependent.
- No Task9b2 main promotion, workflow/playbook, enablement, live forge call,
  caller-controlled repository transport, operator DB action, or queue mutation
  was added.

Residual concern: SQLite-to-PostgreSQL copy/cutover still does not enumerate the
hierarchical integration table family; this remains the pre-existing Task12
scope recorded by the independent review.
