# Task 10c implementation report

## Scope and revisions

- Logical reviewed base: `a01048e5`; resumed runtime work was recovered above
  `00476982`. The unrelated routing commit `efb1b5bf` and controller recovery
  documentation commit `d8fdcc27` were preserved.
- Shared-path Task 10c hunks landed with authored routing in `56f919ff`: the
  root fixture builder/inventory and E402 annotations in the rebuild script,
  production integration wiring in `src/orchestrator/core.py`, disabled-source
  inventory/documentation in `src/vault.py`, and the root source inventory test.
- Remaining runtime/tests/migration/fixture commit: `8e95bc87`, based on the
  post-main-merge head `613865d1` (24 files; 4,045 insertions, 36 deletions).
- Report commit: pending controller report-only commit after this handoff.
- No operator database/configuration, protected environment, daemon, activation,
  live forge/credentials, push, PR, or branch-protection mutation was used.
- `aq prime` had no task-id scope in this delegated shell, so no queue mutation
  was attempted.

## Delivered runtime

- `IntegrationReleaseService` performs the hierarchy-first, single-transaction
  terminal shipping checks, exact schedule/lease CAS, canonical replay, and
  first-catch-up request/outbox insertion. Cleanup state is independent.
- `IntegrationCleanupService` idempotently materializes normalized source/audit
  PRs, local/remote integration refs, and authenticated retained worktrees. It
  uses per-item nonce claims, frozen retry/backoff/exhaustion, post-I/O nonce CAS,
  terminal aggregate projection, authenticated PR reconciliation, expected-old
  Git ref deletion, default-branch rejection, and no-force exact worktree removal.
- The new cleanup table has immutable target identity, named checks/FKs/index,
  dual-dialect terminal guards, and guarded downgrade. Revision `18cd4540cd0d`
  follows the single reviewed head `f0a1b2c3d4e5`.
- Strict subject-only build, CI, release, and cleanup command contracts are
  registered. Omitted seal time is server-derived. Build/rebuild derives the
  current batch/revision/default head and frozen `on_main_moved` policy.
- Production orchestration installs release, root-intent reconciliation, cleanup,
  and CI handlers. A configured daemon resolves the persisted GitHub repository
  to an exact App binding and gives the default `CandidateService` the real
  repository-bound candidate/publication transport; it is not test-injection-only.
- `GitHubAppClient` now supplies least-privilege repository binding, exact PR
  read/comment/close, stable-marker audit PR lookup/create, and cached bound
  tokens. `GitManager` supplies isolated App-auth empty-refspec expected-old
  deletion, expected-old local deletion, and exact no-force worktree removal.
- The disabled prose source and reviewed V2 fixture route due, empty, sealed,
  green/red/conflict, rebuild/wait, debug/human, batch-promoted release, and
  cleanup-requested facts without caller Git/CI/cleanup authority.

## TDD and focused evidence

Observed RED evidence after crash recovery (no missing history reconstructed):

```text
default configured build transport: failed because CandidateService.app_client was None
repository binding/audit transport tests: failed before the production methods existed
owned-worktree test: foreign recorded base was accepted by the generic force-fallback remover
batch-scoped cleanup advance: expected one requested-batch result, got [] from the global page
```

GREEN/focused evidence:

```text
aq test tests/test_github_app.py tests/test_orchestrator.py -x
73 passed

aq test tests/test_integration_cleanup.py tests/test_root_integration_playbook.py \
  tests/test_github_app.py tests/test_git_app_auth.py -x
54 passed

aq test tests/test_integration_contracts.py -x
10 passed in 2.62s

aq test tests/test_integration_cleanup.py -x
13 passed in 5.38s

aq test tests/test_root_integration_playbook.py -x
4 passed in 2.93s
```

The real-engine route test executes due/empty release, sealed build/CI/main
promotion, cleanup requested, batch-promoted release, and repair-exhausted debug
dispatch through the registered executor and command handlers. Artifact assertions
cover all transitions, strict inputs, frozen rebuild/wait, and terminal paths.

## Migration and artifact evidence

The existing disposable PostgreSQL container `65b3e4ca1457` was alive at
`127.0.0.1:16833`. The PostgreSQL test created and dropped only its uniquely named
`task10c_cleanup*` scratch database; it did not migrate `integration_test`,
`postgres`, the worker DB, or the operator DB.

```text
aq test -m migration -p no:xdist \
  tests/test_migration_integration_cleanup.py::test_sqlite_cleanup_migration_guarded_round_trip -x
1 passed

POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/integration_test \
  aq test -m migration -p no:xdist \
  tests/test_migration_integration_cleanup.py::test_postgres_cleanup_migration_guarded_round_trip -x
1 passed

python3 -m py_compile migrations/versions/18cd4540cd0d_normalized_integration_cleanup.py \
  src/integration/release.py src/integration/cleanup.py
[exit 0]

python3 -m alembic heads
18cd4540cd0d (head)

python3 scripts/rebuild-reviewed-playbook-artifacts.py --check root-integration-train
no DRIFT; exit 0
```

## Final verification

Required Task 10c area gate after authored-routing fixture restoration and the
latest-main merge:

```text
aq test tests/test_integration_service.py tests/test_root_integration_playbook.py \
  tests/test_integration_cleanup.py tests/test_integration_candidates.py \
  tests/test_integration_ci.py tests/test_integration_main_promotion.py \
  tests/test_integration_outbox.py tests/test_default_playbook_v2_artifacts.py -x
307 passed, 1 skipped, 12 warnings in 56.02s
```

This fresh gate includes the requested-batch cleanup selection/empty-page
projection, expanded real-engine route coverage, restored authored-routing
fixtures, and latest merged main. The earlier transient `task_route` fingerprint
obstruction is resolved.

```text
ruff check <all changed Task 10c Python paths>
All checks passed!

git diff --cached --check -- <explicit Task 10c paths>
[exit 0]
```

Warnings were the pre-existing `pkg_resources`, namespace-package, and Discord
`audioop` deprecations.

## Self-review and deliverables reconciliation

- Confirmed provider/Git work is outside DB transactions; every finalization is
  nonce/identity/state fenced and aggregate cleanup never mutates promoted/main/CI
  facts or lease ownership.
- Confirmed repository numeric/full-name binding, exact SHA/ref/PR/worktree
  identities, stable comment/audit markers, default-branch refusal, lost-response
  reconciliation, and no-force worktree behavior.
- Confirmed release consumes promoted lifecycle, exact current candidate and
  publication, completed root operation/stage, committed root intent, applied
  main mutation, ordered member/results/reservations/receipts, resolved publication,
  exact lease, and matching active schedule before mutation.
- Confirmed command models reject caller repository/ref/SHA/lease/fence/claim/CI
  authority and trusted project authorization remains enforced after lease release.
- Confirmed source installation is disabled, mechanical, idempotent, byte-exact,
  and never overwrites an operator-owned file; fixture rebuild check is non-writing.
- Reconciled Task 10 deliverables: Task 10a bounded scheduler/service/outbox,
  Task 10b exact attestation/resolver, and Task 10c routes/release/cleanup/runtime
  composition are present. Task 11 controls/cutover and Task 12 live E2E remain
  excluded.

## Concerns carried forward

- The earlier shared-index/overlap concern is resolved: authored routing landed
  in `56f919ff`, latest main was merged at `613865d1`, and the remaining Task 10c
  runtime landed explicitly at `8e95bc87` with a clean cached diff check.
- No live provider or credential exercise was authorized; Task 11 must probe the
  installed transport/protection/configuration before enabling the disabled policy.
- The fresh required gate passed after routing fixture restoration; there is no
  remaining cross-work obstruction for independent review.

---

## Fix round 1 — accepted review findings

Fix base: `64169b8a`. Runtime/report commits are controller-owned because this
inherited worker cannot write the Git index.

### Slice A — frozen route admission

RED:

```text
aq test tests/test_integration_outbox.py::test_disabled_frozen_owner_still_accepts_new_operation_event -x
1 failed: disabled frozen owner was not admitted
```

GREEN:

```text
aq test tests/test_integration_outbox.py::test_disabled_frozen_owner_still_accepts_new_operation_event -x
1 passed in 2.90s
```

Runtime refresh now retains exact activation addresses separately from enabled
destinations. A disabled operation owner is admitted only when activation ID,
playbook, scope, scope identifier, pinned artifact, project scope, and event rule
all match; enabled sibling consumers remain independently selected.

### Slice A — durable candidate CI continuation

Approved interface: operation-bound `integration.candidate_green` and
`integration.candidate_red` events carry exact `project_id`, `operation_id`,
`batch_id`, `revision`, and `head_sha`. Pending/missing/malformed/mixed-attempt
evidence emits no event. Repair dispatch may omit `stage`, in which case the
command derives the current stage under server authority.

RED:

```text
aq test tests/test_integration_attestation.py::test_candidate_observation_emits_only_durable_terminal_ci_continuations -x
1 failed, 8 warnings in 2.12s: pending expected not_green, actual stale_subject
```

The real configured-service path rejected a newly built candidate because
attestation lookup required an already-awaiting repair stage before CI could
make it green.

GREEN (domain continuation):

```text
aq test tests/test_integration_attestation.py::test_candidate_observation_emits_only_durable_terminal_ci_continuations -x
3 passed, 8 warnings in 2.56s
```

The actual authenticated observer and attestation service now distinguish
pending from authenticated terminal red, publish green attestation evidence,
and idempotently enqueue exactly one evidence-bound continuation on replay.

RED (frozen route graph):

```text
aq test tests/test_root_integration_playbook.py::test_candidate_result_routes_are_durable_and_server_derive_repair_stage -x
1 failed, 11 warnings in 2.69s: promote-green-candidate rule absent
```

The first mechanical artifact rebuild also refused the draft graph because a
green-rule CI step reused construct-rule terminal IDs and the authoring source
did not name `integration_promote_main`. The invalid artifact was not accepted.
After correcting those closed-subgraph/source-index defects, the builder wrote
approvable artifact
`sha256:b5868186205aa0079b01623a005926b57087ad2e72ef524f3409c207b19ea985`.

GREEN (route/event contracts):

```text
aq test tests/test_root_integration_playbook.py::test_candidate_result_routes_are_durable_and_server_derive_repair_stage tests/test_integration_contracts.py::test_all_design_events_require_project_and_operation_identity tests/test_integration_contracts.py::test_hierarchy_event_payloads_expose_exact_typed_command_inputs -x
3 passed, 11 warnings in 2.88s
```

After adding exact-current red-event revalidation, the final rebuilt artifact is
`sha256:4682f2214459bef4b8a76afb89cefda5bc602a3245f81102eea3233159d36e9d`.

GREEN (real executor and real repair service):

```text
aq test tests/test_root_integration_playbook.py::test_candidate_result_routes_are_durable_and_server_derive_repair_stage tests/test_root_integration_playbook.py::test_due_and_cleanup_events_run_real_executor_and_subject_handlers tests/test_integration_repair.py::test_repair_dispatch_command_derives_current_stage_with_real_service -x
3 passed, 11 warnings in 3.17s
```

The configured command maps internal `not_green` to public `pending`; green and
red event consumers run through the real executor. The repair command rejects a
stale candidate tuple, derives `active_stage` server-side, and dispatches through
an actual `RepairService`; legacy explicit-stage callers remain supported.

### Slice B — immutable release and complete cleanup projection

RED (release replay):

```text
aq test tests/test_integration_cleanup.py::test_release_replay_is_immutable_after_a_later_train_acquires_lease -x
1 failed, 11 warnings in 2.82s
```

After release promoted a catch-up request, replay under a later train's lease
did not reproduce the original release result.

GREEN (release replay):

```text
aq test tests/test_integration_cleanup.py::test_release_replay_is_immutable_after_a_later_train_acquires_lease tests/test_integration_cleanup.py::test_release_is_atomic_replayable_and_cleanup_independent tests/test_integration_cleanup.py::test_release_atomically_promotes_first_catchup_once -x
3 passed, 11 warnings in 7.15s
```

Release now atomically writes an immutable canonical result before committing
lease/schedule release, and every replay reads that record before inspecting a
later lease or mutable schedule.

RED (cleanup command page):

```text
aq test tests/test_integration_cleanup.py::test_cleanup_command_never_reports_complete_for_a_partial_page -x
1 failed, 11 warnings in 2.90s: actual complete, expected advanced
```

GREEN: the same command passed (1 passed, 11 warnings in 2.69s) after command
projection was changed to count every durable item state, not only the returned
execution page.

PostgreSQL RED infrastructure note: an `aq test` attempt with the disposable
DSN could not write the sandboxed `~/.agent-queue/locks/test-slots` path. A
permitted direct single-file pytest attempt reached collection but socket
creation for `127.0.0.1:16833` failed with `PermissionError: [Errno 1]`.
No baseline database was opened or changed. The test itself creates and drops a
uniquely named scratch database; controller-side execution remains required.

Controller PG GREEN: the exact supplied node ran through `aq test` on the
disposable server and unique scratch database: 1 passed, 11 inherited warnings
in 4.98s (test call 2.46s), exit 0.

### Slice C — irreversible cleanup fencing and local ownership

RED:

```text
aq test tests/test_integration_cleanup.py::test_cleanup_never_reposts_after_ambiguous_pr_comment_prewrite tests/test_integration_cleanup.py::test_cleanup_local_ref_requires_vacancy_and_recorded_owner tests/test_integration_cleanup.py::test_cleanup_reconciles_worktree_absent_after_remove_crash -x
1 failed, 11 warnings in 2.82s: ambiguous PR comment was posted twice
```

GREEN:

```text
aq test tests/test_integration_cleanup.py::test_cleanup_never_reposts_after_ambiguous_pr_comment_prewrite tests/test_integration_cleanup.py::test_cleanup_local_ref_requires_vacancy_and_recorded_owner tests/test_integration_cleanup.py::test_cleanup_reconciles_worktree_absent_after_remove_crash -x
4 passed, 11 warnings in 7.04s

aq test tests/test_integration_cleanup.py::test_expired_cleanup_claim_cannot_post_after_successor_prewrite -x
1 passed, 11 warnings in 2.80s
```

PR comments and worktree removals now freeze an immutable claim-owned prewrite.
Marked ambiguous publication never posts again after expiry; retries only
reconcile the exact marker. Local ref deletion requires immutable promotion
ownership, rejects a foreign current owner, and rejects any matching checked-out
worktree. A missing retained worktree is accepted only with its exact durable
removal prewrite, making remove-before-finalize crash replay safe.

### Slice D — frozen source retention and strict target tuples

RED:

```text
aq test tests/test_integration_service.py::test_policy_uses_compatible_rebuild_and_cleanup_defaults tests/test_integration_sealing.py::test_one_root_seal_freezes_review_and_real_unstarted_operation tests/test_integration_sealing.py::test_seal_freezes_source_ref_and_retention_from_authoritative_checkpoint tests/test_integration_cleanup.py::test_cleanup_materializes_normalized_terminal_set_idempotently tests/test_integration_cleanup.py::test_cleanup_retains_source_ref_from_frozen_policy tests/test_integration_cleanup.py::test_cleanup_legacy_source_identity_is_visible_conflict -x
2 failed, 1 error, 11 warnings in 2.89s
```

The policy lacked both approved retention inputs, sealed members had no frozen
source-ref identity, and cleanup could not materialize or safely conflict on
that identity. This is semantic RED; a preceding invocation named one
nonexistent test node and collected nothing, so it is not counted as evidence.

GREEN (frozen identity/materialization):

```text
aq test tests/test_integration_service.py::test_policy_uses_compatible_rebuild_and_cleanup_defaults tests/test_integration_sealing.py::test_one_root_seal_freezes_review_and_real_unstarted_operation tests/test_integration_sealing.py::test_seal_freezes_source_ref_and_retention_from_authoritative_checkpoint tests/test_integration_cleanup.py::test_cleanup_materializes_normalized_terminal_set_idempotently tests/test_integration_cleanup.py::test_cleanup_retains_source_ref_from_frozen_policy tests/test_integration_cleanup.py::test_cleanup_legacy_source_identity_is_visible_conflict -x
6 passed, 11 warnings in 6.50s

aq test tests/test_integration_cleanup.py::test_cleanup_executes_exact_refs_and_prs_once tests/test_integration_cleanup.py::test_cleanup_never_deletes_default_branch_even_with_matching_sha tests/test_integration_cleanup.py::test_cleanup_source_ref_requires_frozen_head_and_no_foreign_owner tests/test_integration_cleanup.py::test_cleanup_delays_failed_retained_work_by_frozen_window -x
5 passed, 11 warnings in 3.70s
```

The authoritative checkpoint branch is normalized and frozen with its reviewed
head and delete/retain decision in each member and manifest digest. Default
delete materializes an exact-SHA authenticated remote-ref item; retain omits the
delete. Moved, default, and actively foreign-owned source refs conflict. Legacy
members without the new tuple durably set batch cleanup to conflict and never
consult a mutable task. Failed retained work receives the frozen 604800-second
default (or configured) delay.

SQLite migration GREEN:

```text
aq test -m migration tests/test_migration_integration_cleanup_hardening.py::test_sqlite_cleanup_hardening_migration_round_trip -x
1 passed, 8 warnings in 1.99s
```

Revision `a10c5e1e4f03` upgrades/downgrades/upgrades the source tuple and corrected
target constraint while recreating SQLite immutable/prewrite guards. The
historical cleanup creation constraint also explicitly requires non-null PR
numbers. A first draft migration rebuild missed a cross-table empty-batch
trigger and failed during SQLite schema setup; the migration now deliberately
drops and recreates that guard around the member-table rebuild.

Controller PostgreSQL migration checkpoint: the amended historical revision-18
round trip passed. The first new-revision run failed before migration execution
because the test constructed an unavailable synchronous `psycopg` engine; this
was test infrastructure, not semantic RED, and left one scratch database for
the controller to identify/drop. The test now uses the repository's existing
asyncpg engine plus `connection.run_sync`, with initialization and disposal
inside the scratch-drop `finally`.

Focused area files:

```text
aq test tests/test_integration_cleanup.py -x
25 passed, 1 skipped (PostgreSQL), 11 warnings in 8.15s

aq test tests/test_integration_sealing.py tests/test_integration_service.py -x
29 passed, 6 skipped (PostgreSQL), 11 warnings in 5.63s
```

Controller PostgreSQL migration GREEN: after the asyncpg/run-sync driver fix,
the exact `a10c5e1e4f03` round-trip node passed with 1 test and 8 warnings in
4.34s (test call 2.58s). The controller dropped only the exact leaked scratch
database `integration_test_gw2_task10c_cleanup_hardening` after the successful
rerun; no baseline or operator database was changed. The already-run amended
revision-18 PostgreSQL round trip remained green (1 passed in 3.22s).

### Final amended-area gate

The first eight-file gate stopped after 159 passed and 1 skipped because the
reviewed root-integration manifest still named the pre-change artifact hash.
After refreshing the manifest, its exact seven-case review-record group passed
(7 passed, 8 warnings in 1.61s). The next gate stopped after 162 passed and 1
skipped because `hierarchical-delivery` still carried the pre-change
`integration_repair_dispatch` fingerprint. Rebuilding that reviewed fixture
and updating its artifact/contract manifest repaired the genuine command
fingerprint ripple. Timestamp-only rebuild churn in five unrelated fixtures
was removed; only root-integration-train and hierarchical-delivery retain
deterministic fixture changes.

GREEN (single completed amended-area gate):

```text
aq test tests/test_integration_service.py tests/test_root_integration_playbook.py tests/test_integration_cleanup.py tests/test_integration_candidates.py tests/test_integration_ci.py tests/test_integration_main_promotion.py tests/test_integration_outbox.py tests/test_default_playbook_v2_artifacts.py -x
320 passed, 2 skipped, 11 warnings in 54.52s
```

Generated fixture reconciliation also passed read-only drift checking:

```text
python3 scripts/rebuild-reviewed-playbook-artifacts.py --check
exit 0; no DRIFT entries
```

Round-1 changed-path hygiene:

```text
ruff check <26 changed Python runtime/migration/test paths>
All checks passed!

git diff --check
[exit 0]
```

### Round-1 self-review against all accepted Important findings

1. Pending CI is non-terminal and durable exact-current green/red evidence now
   emits a frozen-operation continuation; missing or ambiguous proof emits none.
2. Red/conflict repair dispatch consumes the existing operation and derives its
   active stage server-side; the candidate tuple is revalidated before dispatch.
3. Disabled activations remain addressable only for an exact frozen route and
   pinned artifact, while ordinary enabled destination selection is unchanged.
4. Release replay reads the immutable per-batch result before mutable later
   lease/schedule state.
5. PR comment POST is preceded by an immutable claim-owned prewrite; ambiguous
   publication can only reconcile its marker and never obtains a duplicate POST.
6. Cleanup finalization locks the batch row before item CAS and aggregate
   projection; the two-transaction PostgreSQL test passed on the scratch server.
7. Sealing freezes normalized source refs plus delete/retain policy into member
   identity and digest; cleanup never reconstructs missing legacy identity from
   mutable task state, and failed work uses the frozen retention delay.
8. Local deletion requires matching immutable ownership and no worktree
   occupancy in addition to default-branch and expected-SHA protection.
9. Command outcomes are projected from every durable item, preserving pending,
   retryable, conflict, and failed states beyond a returned execution page.
10. Worktree removal has an immutable prewrite, so exact owned absence after a
    remove-before-finalize crash is reconciled without accepting foreign absence.
11. Source/audit PR tuples require an explicit non-null positive PR number in
    metadata and both migration dialects; SQLite and PostgreSQL U-D-U tests
    exercise rejection of the incomplete tuple.

No new concern was found in the round-1 self-review. The controller-owned
`progress.md` edit remains intentionally excluded from Task 10c runtime/report
paths. Fix-round runtime and report commits remain pending controller exact-path
commits; no index mutation was attempted in this inherited worker.
