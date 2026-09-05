# Task 9a Fix Round 1 Report

## Identity and scope

- Review HEAD at start: `ded1788ef18af56cbf4d0567b95fc1df4484ae7d`
- Required runtime/report fix base: `5afb085b`
- Runtime fix commit: `32c27e7df54a7a95e75f03178f834c044d8134a2`
- Review source read completely: `task-9a-review.md`
- Scope was limited to the four accepted Task 9a findings. No Task 9b candidate
  construction/promotion, Task 10 workflow, Task 11 protection/config enablement, live
  network, live credential, forge mutation, project enablement, daemon start, or operator
  database action was performed.
- No schema or migration changed, so no SQLite/PostgreSQL migration cycle was applicable.

## Finding 1: privileged Git containment

### Runtime shape

- `GitManager.apush_oid_with_app_auth` constructs only the literal validated
  `https://github.com/<owner>/<repository>.git` destination. `GitHubRepositoryBinding`
  now rejects URL-significant and nonliteral full names.
- Before any credential exists, the exact validated tip is fetched into a fresh
  daemon-owned mode-0700 temporary bare repository. The import runs with a minimal
  environment, no system/global config, `GIT_NO_REPLACE_OBJECTS=1`, and explicit local
  protocol permission. The imported ref is checked against the exact requested commit.
- The credentialed Git command runs from the isolated home/bare repository, never the
  worker checkout. Its environment is an explicit allowlist only; system/global config,
  hooks, helpers, proxies, URL rewrites, external transports, terminal prompting, and
  unrelated daemon environment are unavailable. The push uses `--no-verify`, a null
  hooks path, an exact lease, exact OID refspec, and the literal destination.
- The token is written once into a non-seekable pipe by a broker task. The ordinary parent
  write descriptor is closed before Git spawn; only the read capability reaches Git.
  Askpass returns the username without consuming the pipe and releases the token only for
  the exact expected GitHub password prompt. There is no rewind/reuse path and no token in
  argv, Git config, ambient environment, stdout/stderr, logs, or errors.
- Privileged Git starts a new session. Timeout, cancellation, spawn failure, and all
  post-spawn exceptions terminate and reap the process group; cancellation re-raises
  `CancelledError`. A final audit found the pre-spawn executable failure could wait on a
  full broker pipe while the parent reader remained open; the reader is now closed before
  awaiting the broker on that path.

### Security diagnostics and TDD evidence

1. RED:

   `pytest -q tests/test_git_app_auth.py::test_privileged_push_ignores_worker_hooks_config_rewrites_helpers_and_daemon_env`

   Result: `1 failed, 2 warnings`; the isolated push seam was absent (`AttributeError`).

2. GREEN, initial real containment:

   Same command: `1 passed, 2 warnings in 0.48s`.

3. The exact-object test was strengthened to use an active replacement ref and a
   `--shared` checkout backed by alternates. Its first diagnostic assertion incorrectly
   expected `rev-parse <oid>^{commit}` to print the replacement OID and failed `1` test;
   comparing `cat-file` content correctly proved the replacement was active. The corrected
   focused run passed: `1 passed, 2 warnings in 0.55s`, while the destination received the
   original validated OID.

4. Full focused containment/lifecycle file before the final broker audit:

   `pytest -q tests/test_git_app_auth.py`

   Result: `8 passed, 2 warnings in 1.04s`. A later run after moving the broker ownership
   before spawn also returned `8 passed, 2 warnings in 0.96s`.

5. RED, final spawn-failure lifecycle audit:

   `timeout 5s pytest -q tests/test_git_app_auth.py::test_app_push_spawn_failure_closes_broker_reader_before_waiting; test $? -eq 124`

   Result: shell exit `0`, confirming pytest itself reached the external five-second timeout.
   Root cause: on executable spawn failure an oversized token filled the broker pipe, while
   exception cleanup awaited the broker before closing the only reader.

6. GREEN after the single root-cause fix:

   `pytest -q tests/test_git_app_auth.py::test_app_push_spawn_failure_closes_broker_reader_before_waiting`

   Result: `1 passed, 2 warnings in 0.48s`.

The real malicious regression installed a worker `pre-push` hook, local/global/system URL
rewrites, a credential helper, proxy settings, an unrelated daemon secret, shared object
alternates, and a replacement ref. The intended local bare destination alone advanced;
the trap destination, hook capture, and helper capture remained absent. Separate real
scripts proved timeout and cancellation remove both the Git leader and its descendant
process group.

## Finding 2: current CI subject, repository, and lock authority

### Runtime shape

- Parent validation resolves task/project first, requires an active enabled hierarchy/train
  project whose designated repo is the trust manifest's canonical repo, takes the established
  hierarchy project lock, then locks/rechecks project/task, current checkpoint, and the exact
  operation for the checkpoint episode. It requires `verifying`, exact repo/generation/head,
  exact operation ID/episode/target, and live `active|escalated` operation state.
- Root validation resolves batch/project first, requires the same project/repo authority,
  takes the hierarchy project lock, then locks/rechecks project, batch, exact current
  candidate revision, and exact batch episode operation. It requires current revision,
  `testing|repairing` batch lifecycle, exact candidate SHA and `built|testing` state, and a
  live exact operation.
- Authenticated provider observation no longer runs inside a database transaction or while
  holding project/row locks. Phase one snapshots authority under the canonical lock order;
  observation runs after release; phase two reacquires the same locks, revalidates every
  fact and frozen check policy, and appends evidence in that same transaction. A mutation
  during observation returns `stale_subject` and writes no evidence.

### TDD evidence

- True parent repository RED after correcting fixture creation order:

  `pytest -q tests/test_integration_ci.py::test_ci_service_rejects_parent_outside_designated_repository`

  Result: `1 failed, 2 warnings`; old code returned `green` and appended two rows.

- Parent repository/wrong/completed operation GREEN at that boundary:

  `pytest -q tests/test_integration_ci.py::test_ci_service_rejects_parent_outside_designated_repository tests/test_integration_ci.py::test_ci_service_rejects_wrong_or_completed_parent_operation`

  Result: `3 passed, 2 warnings in 0.71s`. The test was then extended/renamed for an extant
  old episode; `test_ci_service_rejects_wrong_completed_or_old_parent_operation` returned
  `3 passed, 2 warnings in 0.84s`.

- Root fixture iteration initially exposed two test-construction mistakes (an immutable
  sealed-batch update and a conditional accidentally patched into the valid fixture), not a
  runtime failure. After seeding the wrong repository at insert time and restoring the valid
  fixture, the root current-authority command returned `4 passed, 2 warnings in 0.93s`:

  `pytest -q tests/test_integration_ci.py::test_ci_service_rejects_noncurrent_root_authority tests/test_integration_ci.py::test_ci_service_binds_root_evidence_to_exact_batch_revision_and_candidate`

- Full CI contract after two-phase revalidation:

  `pytest -q tests/test_integration_ci.py`

  Result: `32 passed, 2 warnings in 1.77s`.

- Explicit SQLite SQL/lock trace:

  `pytest -q tests/test_integration_ci.py::test_ci_service_parent_lock_order_is_hierarchy_subject_operation`

  Result: `1 passed, 2 warnings in 0.46s`; both validation phases observed hierarchy lock →
  checkpoint lock → exact operation lock, and evidence insert only followed the second phase.

PostgreSQL-specific lock tracing was not added because this focused test file has no existing
PostgreSQL lock-order harness; the final affected gate exercises the shared SQLAlchemy path.

## Finding 3: malformed ordering identities

Each attestation-read, required-CI-check, and publication-reuse path now first collects records
by exact name and strict trusted App identity. Any matching candidate with missing, boolean,
string, zero, or negative/nonpositive `id` fails closed before numeric newest selection. No
older record can be read or reused.

The independent review had already demonstrated the old filter-before-order failure at the
accepted fix base. The focused covering run after the small shared ordering correction was:

`pytest -q tests/test_integration_ci.py -k 'malformed_trusted_ordering_id'`

Result: `12 passed, 19 deselected, 2 warnings in 0.27s`, covering all four malformed classes
in all three paths. This slice's tests were added immediately after the direct three-site
correction, so there was no separate local pre-fix RED command; the accepted review scenario
is the RED evidence. This sequence deviation is disclosed rather than reconstructed.

## Finding 4: closed non-secret configuration

- `integration.github_app` is now either absent or an exact mapping containing only
  `client_id`, `app_id`, `installation_id`, and `private_key_path`. Non-mappings and every
  unknown key fail before environment substitution or dataclass construction; error text
  includes field names but never values.
- The config editor's dataclass-object schemas use `additionalProperties: false` recursively.
  Genuine typed dict fields continue to emit their prior typed `additionalProperties` schema.
- Both dry-run and real editor updates validate through `load_config`; rejected edits never
  touch the persisted file. `get_config` validates the raw file and returns one generic safe
  error instead of raw config if an external modification introduced forbidden material.

### TDD evidence

- Load RED:

  `pytest -q tests/test_config_validation.py::TestGitHubAppConfigValidation::test_load_rejects_nonmapping_or_unknown_inline_material`

  Result: `3 failed, 2 warnings`; all invalid values were silently accepted.

- Load GREEN:

  `pytest -q tests/test_config_validation.py::TestGitHubAppConfigValidation`

  Result: `6 passed, 2 warnings in 0.22s`.

- Editor/schema/get RED:

  `pytest -q tests/test_config_editor.py::TestGetConfigCommand::test_externally_modified_inline_github_secret_is_never_returned tests/test_config_editor.py::TestUpdateConfigCommand::test_inline_github_secret_edit_is_rejected_without_round_trip tests/test_config_editor.py::TestGetConfigSchemaCommand::test_returns_schema`

  Result: `2 failed, 2 passed, 3 warnings`; raw get exposed the sentinel and dataclass schemas
  were open. The two editor-update cases were already protected by the new loader validation.

- GREEN, same command: `4 passed, 3 warnings in 0.77s`.

## Final affected-area verification

Exactly one combined affected-area gate was run after all four main fixes:

`aq test tests/test_config_validation.py tests/test_config_editor.py tests/test_github_app.py tests/test_git_app_auth.py tests/test_integration_ci.py tests/test_git_manager_async.py tests/test_integration_parent_completion.py tests/test_integration_repair.py -x`

Result: **384 passed, 11 warnings, 0 failed in 22.40s** (`-n 3`, slot 1 of 2).
The only subsequent test was the single-file/node broker spawn-failure regression described
above, required by the final security audit; the combined gate was not rerun or concealed.

Final changed-file lint and whitespace check:

`ruff check src/commands/system_commands.py src/config.py src/config_editor.py src/git/askpass_fd.py src/git/github_app.py src/git/manager.py src/integration/ci.py tests/test_config_editor.py tests/test_config_validation.py tests/test_git_app_auth.py tests/test_github_app.py tests/test_integration_ci.py && git diff --check`

Result: `All checks passed!`; `git diff --check` emitted no output.

## Files and finding reconciliation

- Privileged Git: `src/git/manager.py`, `src/git/askpass_fd.py`,
  `src/git/github_app.py`, `tests/test_git_app_auth.py`, `tests/test_github_app.py`.
- CI authority and malformed IDs: `src/integration/ci.py`,
  `tests/test_integration_ci.py`.
- Config closure: `src/config.py`, `src/config_editor.py`,
  `src/commands/system_commands.py`, `tests/test_config_validation.py`,
  `tests/test_config_editor.py`.

All four Critical/Important review findings have direct runtime changes and focused regression
coverage. No duplicate evidence table, generated client change, or migration was introduced.

## Self-review, process disclosures, and concerns

- Reviewed the complete diff after the focused slices and checked the credential boundary for
  argv/env/config/hook/output leakage, exact-object import, replacement/alternates behavior,
  failed-spawn cleanup, direct/descendant process cleanup, and cancellation semantics.
- Reviewed both CI paths for hierarchy → current subject → exact operation ordering, current
  repository/episode/revision/lifecycle constraints, absence of network work under DB locks,
  and the second locked revalidation through append.
- Reviewed config load, editor candidate validation, raw-get behavior, generated schema closure,
  and a genuine nested dict schema to ensure dicts were not accidentally closed.
- The original Task 9a report's accidental bare two-file pytest command remains disclosed; it
  was not rerun or rewritten. The 11 final warnings are pre-existing `pkg_resources` namespace
  and `discord.audioop` deprecations and were not broadened into this security fix.
- No known runtime blocker or open security concern remains in this fix scope. The isolated
  transport pins `/usr/bin/git` deliberately so the privileged boundary cannot be redirected
  through ambient `PATH`; the internal executable override exists solely for real local failure
  and process-group tests.

## Commits

- Runtime/tests: `32c27e7df54a7a95e75f03178f834c044d8134a2`
- Report: recorded by the subsequent documentation commit.
