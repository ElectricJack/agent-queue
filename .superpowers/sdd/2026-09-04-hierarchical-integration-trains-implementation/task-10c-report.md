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
