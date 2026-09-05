# Task 9b1 fix round 1 report

## Scope and revisions

- FIX_BASE: `b1390aec`; review/base HEAD observed: `399c400e`.
- Generated Alembic revision: `69416e65ee21`, derived from the actual sole head `9b3e5a7c1d20` with `/usr/bin/python3.12 -m alembic revision -m "candidate publication authority"`.
- Runtime/tests/migration commit: `33bd7534`.
- No Task 9b2 promotion, live GitHub calls, operator database changes, reset, rebase, or external branch rewrite.

## RED evidence

Each accepted review finding received a focused failing test before production changes:

1. `pytest -q tests/test_integration_candidates.py::test_expired_project_lease_cannot_advance_candidate` — **1 failed**: returned `built` instead of `wait`.
2. `pytest -q tests/test_integration_candidates.py::test_direct_caller_repair_lineage_is_not_authoritative` — **1 failed**: missing `CandidateAuthorizationError`/caller lineage still authoritative.
3. `pytest -q tests/test_integration_candidates.py::test_nonempty_build_requires_authenticated_repository_dependencies` — **1 failed**: returned `built` instead of `configuration_blocked`.
4. `pytest -q tests/test_integration_candidates.py::test_persisted_pr_never_hides_diverged_candidate_ref` — **1 failed**: returned `already_built` instead of fencing the diverged ref.
5. `pytest -q tests/test_integration_candidates.py::test_construction_applies_only_each_sealed_source_delta` — **1 failed**: imported unrelated pre-source-base history.
6. `pytest -q -m migration tests/test_migration_candidate_authority.py::test_sqlite_candidate_authority_upgrade_downgrade_upgrade` — **1 failed** against the generated empty migration skeleton.
7. First PostgreSQL migration arm exposed invalid PL/pgSQL CASE syntax — **1 failed**; parenthesized CASE comparisons fixed it before runtime depended on the schema.
8. `pytest -q tests/test_git_app_auth.py::test_app_exact_fetch_imports_only_requested_oid_to_daemon_namespace` — **1 failed** until the exact source OID was made reachable in the local containment remote.

## Implemented invariants

- Canonical state resolution reads only project identity first, then holds `db.immediate()`/hierarchy project lock while re-reading project, batch, lease, operation/current stage, current revision, and repository-qualified branch owner.
- The reserved repair operation is the durable `collector` branch owner. Lease owner/token/batch/expiry and branch owner/token/state are revalidated. Progress, completion, publication, and resolution consumption use affected-row CAS.
- Repair-owned/attached branches wait. Conflict publication pins and publishes the exact partial before ordinary Task 7 repair dispatch. Acceptance transfers to the collector only after the injected server handoff confirmation.
- Nonempty builds require App and forge dependencies. Repository binding checks numeric/canonical App identity against the frozen repository (trusted local transports are explicit test-only adapters).
- `GitHubAppClient.exact_head_ref` performs an installation-authenticated numeric-repository exact ref read. `GitManager.afetch_exact_oid_with_app_auth` uses the existing one-shot credential broker in an isolated temporary bare repository, fetches one literal OID, verifies it as a commit, then imports it credential-free to a daemon-only `refs/aq/*` ref.
- Candidate construction has no production `repository.url` clone/fetch, wildcard fetch, ambient credential/config, or caller-selected remote. A search for `repository.url|clone|+refs/heads|_fetch(` in `src/integration/candidates.py` returned empty.
- Publication is per revision and monotone: `reserved -> ref_published -> pr_reserved -> pr_published`. Ref intent precedes push; replay reads the exact authenticated remote ref. PR intent/idempotency precedes provider I/O; lookup precedes create; persist follows a second hierarchy/fence/revision validation. Full numeric repository, full name, base/head refs, SHA, number, URL and idempotency identity are validated.
- Sealed member application uses `source_base_sha..reviewed_head_sha` as the explicit merge base and validates ancestry plus reviewed tree. Tests cover divergent inferred merge base and binary/delete/rename deltas.
- Rebuild treats the caller SHA only as an expected comparison, reads the authenticated default head twice, exact-fetches/verifies it before superseding, pins old revision before N+1, and leaves N current for absent/mismatched/moved authority.
- Candidate resolutions are normalized and bound to batch/revision/member, current operation stage (primary or debug), task/session/instance/workspace, repository/branch fence, partial/source/resolved OIDs/tree/ordered commits and push evidence. Raw caller lineage is rejected. Push and acceptance revalidate current server scope. Acceptance authenticated-fetches the exact remote object, checks remote ref, tree, ancestry, ordered no-merge commits, intended path scope/non-noop, and reserved paths on both sealed and repaired ranges, then advances once.

## Schema and migration evidence

New tables:

- `integration_candidate_publications`: composite FK to candidate revision, positive repository numeric ID, state/check constraints, all-or-none PR identity and unique idempotency key.
- `integration_candidate_resolutions`: composite FK to member result and repair stage, FKs to task/session/workspace, exact writer/fence/Git identity, push/state checks and one reservation per member revision.

SQLite triggers and PostgreSQL functions/triggers enforce immutable identity/push evidence and monotone state. Downgrade fails if either authority table contains rows.

- `pytest -q -m migration ...::test_sqlite_candidate_authority_upgrade_downgrade_upgrade` — **1 passed**.
- Disposable PG command with `POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres` and unique worker `task9b1_final_69416` — **1 passed** upgrade/downgrade/re-upgrade.
- Scratch DBs `postgres_task9b1_final_69416` and `postgres_task9b1_final_69416_task9b1_candidate_authority` were terminated/dropped. Neither `postgres`, `integration_test`, nor an operator DB was migrated.
- `/usr/bin/python3.12 -m alembic heads` — `69416e65ee21 (head)`.

## GREEN and final verification

- Candidate focused file: `pytest -q tests/test_integration_candidates.py` — **22 passed**; the final affected gate includes the expanded debug/reserved/extra cases.
- Exact C nodes (divergent base, binary/delete/rename, rejected foreign rebuild base) — **3 passed**.
- Exact D nodes (primary exact, debug exact, reserved addition, extra content; each stale instance check) — **4 passed**.
- Authenticated exact ref/OID plus resolution handoff selection — **3 passed**.
- `aq test tests/test_integration_candidates.py tests/test_integration_repair.py tests/test_integration_ownership.py tests/test_git_app_auth.py tests/test_github_app.py` — **97 passed**, 11 warnings, 19.17s, slot 1/2, three workers (fresh final rerun after all adversarial cases).
- Changed-file `ruff check` over runtime/tests/migration — **All checks passed**.
- Migration `py_compile`, `git diff --check` — passed.

Warnings were existing `pkg_resources`, namespace-package, and Python `audioop` deprecations.

## Self-review

- External work inside a DB transaction is limited to the bounded exact branch ref read/push protected by the hierarchy lock, lease/current-revision validation, and `BranchOwnership.mutation_exclusion_on`. Construction, object fetch/import and PR provider calls remain outside; PR persistence revalidates afterward.
- Publication and resolution identities are deterministic and durable before mutation; replay reconciles remote state instead of trusting `batch.pr_url` or provider memory.
- No source ref is changed. All retained refs use daemon recovery namespaces. No generic clone/wildcard fetch remains.
- Repair resolution is server-owned and instance-bound; legacy NULL tokens and raw lineage cannot author acceptance. Primary/debug stages use `operation.active_stage`.
- No normalized root intent, all-member promotion receipt, main push, CI execution, workflow, or Task 9b2 implementation was added.

## Concerns

- The authenticated fetch implementation intentionally duplicates a small part of the existing push credential-broker lifecycle to keep this change narrow; a later internal refactor may share that private runner without changing security semantics.
- Existing Task 6 module-size warnings remain outside this fix scope.
