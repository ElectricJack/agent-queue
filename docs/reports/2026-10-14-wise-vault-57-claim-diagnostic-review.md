# Claim diagnostic storage review receipt — wise-vault-57

Date: 2026-10-14. Reviewed prepared source: `87d395891ae35d60b143bd1c8442a4db55326fa9`.

All six inherited claim-diagnostic cases are already covered by source-specific manifest classifications in this prepared checkout. This task publishes only this attribution receipt; no additional classification correction is needed.

## Source and publication attribution

The historical failure is reproducible as six missing or mismatched sink/owner identities in `bold-zenith-11.27` publication `a5bed2e4609a669ab94383df16f374a56caadaa6`. It is not a current claim-command inventory failure.

The compared `wise-stone-87` publication `2d6fecad0fd322fd19a0b37a261112351979996e` (PR 1520) also has exact manifest matches for its six corresponding current calls. Three attention call fingerprints survive unchanged; its other calls and enclosing owners have changed. That publication is **not an ancestor** of this prepared HEAD. This receipt does not claim delivery of its whole tree or equivalent production safeguards.

The prepared checkout independently contains the needed classifications. `df59b20729013fccd4f2b279d50840d5a58650e5` introduced the branch-fence diagnostic replacement. `d77002e4d73b9c7f9a76f3549f53b8b4b1ac44fc` reviewed workspace-safeguard preparation owners and the canonical diagnostic replacement; `3c6cee1c04053d6126c4d50381734031f925c8b1` reconciled the three attention markers against the combined batch owner. Existing historical entries are not evidence of current acceptance: the current call **and** enclosing owner must match.

## Exact six-case comparison

All owners below are `ClaimCommandsMixin` methods in `src/commands/claim_commands.py`; line numbers refer to the reviewed prepared source. Each current entry has disposition `ordinary` and an exact owner-fingerprint match in `tests/integration_ownership.json`.

### `aa66` → `dafd`

- Historical call: `aa66b0c4bf394b454a55b04b3048c4ae854edc5b7c2e7dcc2bdeb6a61fa2e5b1`.
- Current call: `dafd2c39585871bc43eee31fe328d25d78b2fe580fe7377a7af483d0e3dd123a`.
- Current owner: `ClaimCommandsMixin._branch_fenced_prepare_failed` at line 1092; fingerprint `f0016aa1c4398254d4b2a25927bbc29e3a4d67dcebba1e5e3362bcf1e765207d`.

Branch-fence refusal diagnostics (reason, branch, repository, workspace, session, failed_at) written in the same command as the release_claim to READY that follows; cleared by the next successful activation. Metadata key: branch_fenced_prepare_failed; a claim preparation marker with no remote OIDs, publication intent or ref replay authority.

### `5f457` → `9a6c`

- Historical call: `5f457951c70bf9a85748058a4541961a976c9d242448650882070b61af58ba61`.
- Current call: `9a6c34a59da72b0a5f8d49ff08edadf3828cf7b641054abd4cf2ff7f3a44b73c`.
- Current owner: `ClaimCommandsMixin._record_prepare_failure` at line 1139; fingerprint `1dd1e2cdd2ecc4efe42a17188b8f509919a34ca44a557c2d7a50ecdadab0a54f`.

Ordinary PREPARE_FAILURE diagnostic snapshot: one bounded secret-scrubbed stage/type/cause description plus task, observing session instance, claim epoch, workspace id and safe_text-scrubbed bounded workspace path, timestamp, attempt and the caller's already-selected disposition. Replaces the prior diagnostic call fingerprint after task/path identity was added. Cleared by the next activation; grants no retry, budget reset, ownership change, publication intent or ref replay authority and creates no duplicate integration lifecycle.

### `64d907` → `a013`

- Historical call: `64d9070466d052e67a1a96dd3a40b4a768057d1acb08c0c4e390c117e3eb58e7`.
- Current call: `a013c7a46d34c2c833b5b1129a77bc31978b9ed19c44d76097646187d4fe028d`.
- Current owner: `ClaimCommandsMixin._prepare_and_activate_locked` at line 1545; fingerprint `7538eb4115db7ac17fcc674f6041d8e4255f4f56486a529aee9bc55c435b793a`.

Ordinary slot_reset_failure diagnostic snapshot after non-transient preparation errors: bounded scrubbed reason, error type and stage, observing workspace/session, timestamp, attempt and existing automatic/manual retry disposition. PostgreSQL contention bypasses this write. Re-reviewed after workspace binding, foreign-claim and checkout reconciliation guards changed the owner: the marker does not grant retries, reset budgets, replay refs or duplicate integration lifecycle; guarded release_claim still owns the transition.

### `3d42` → `3d42`

- Historical call: `3d42ccd676d8e3ea97e225f6a3ff602b9f5b68a48a3950e76d6d7fabbd3e03b9`.
- Current call: `3d42ccd676d8e3ea97e225f6a3ff602b9f5b68a48a3950e76d6d7fabbd3e03b9`.
- Current owner: `ClaimCommandsMixin._prepare_and_activate_locked` at line 1421; fingerprint `7538eb4115db7ac17fcc674f6041d8e4255f4f56486a529aee9bc55c435b793a`.

Ordinary attention marker when a stack preparation conflict cannot safely release its attached writer. Metadata key: needs_attention; stores the exception code, while retained branch evidence stays with the existing guarded ownership mechanism. Combined batch adds staged preparation diagnostics and redacted failure evidence around the same attention write; retained ownership still controls recovery.

### `6a228` → `6a228`

- Historical call: `6a228f11c57cb47938f52d2fd7326591678f2c7af3ca331ebf2cb0fd60c52b3a`.
- Current call: `6a228f11c57cb47938f52d2fd7326591678f2c7af3ca331ebf2cb0fd60c52b3a`.
- Current owner: `ClaimCommandsMixin._prepare_and_activate_locked` at line 1516; fingerprint `7538eb4115db7ac17fcc674f6041d8e4255f4f56486a529aee9bc55c435b793a`.

Ordinary attention marker when permanent preparation failure cannot safely detach an attached writer. Metadata key: needs_attention; stores integration_prepare_failed_attached without publication intent, authoritative ref OIDs or replay authority. Combined batch adds staged preparation diagnostics and redacted failure evidence around the same attention write; retained ownership still controls recovery.

### `7a916` → `7a916`

- Historical call: `7a916a65e9151b75a342cc96b899777719a789274ba927f08edaabe5af70d879`.
- Current call: `7a916a65e9151b75a342cc96b899777719a789274ba927f08edaabe5af70d879`.
- Current owner: `ClaimCommandsMixin._prepare_and_activate_locked` at line 1474; fingerprint `7538eb4115db7ac17fcc674f6041d8e4255f4f56486a529aee9bc55c435b793a`.

Ordinary attention marker when the repair target is confirmed absent and the managed fence cannot be released. Metadata key: needs_attention; stores repair_target_unpublished without publication intent, authoritative ref OIDs or replay authority. Combined batch adds staged preparation diagnostics and redacted failure evidence around the same attention write; retained ownership still controls recovery.

The former `claim_preparation_failure` write is replaced by the canonical `PREPARE_FAILURE` write in `_record_prepare_failure`, rather than accepted under its obsolete fingerprint. The branch-fence and slot-reset payloads now carry the described error type/stage. These payloads describe observations and the caller's disposition. Actual claim release, retained attachment decisions and integration recovery still belong to `release_claim`, `BranchLock.release` and `arelease_integration_writer_for_retry` (claim-command lines 1103–1120 and 1402–1579); the metadata calls do not publish refs or grant replay authority.

## Verification and retained drift

- `POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue_test:agent_queue_test_dev@localhost:5534/postgres aq test tests/test_integration_size.py`: **32 passed, 16 warnings**, 37.03 seconds. Default resource caps and marker selection. Initial invocation without the required test DSN exited 4 and ran nothing; only the subsequent run counts as verification.
- `python -m tests.integration_ownership`: completed; engine **75,949**, owned total **140,360**, **45** tables, zero unknown claim-command sinks. Existing transition ceilings remain 97,627 engine lines, 155,499 total lines and 45 tables.
- `./scripts/regenerate-generated.sh --check`: passed; generated files current.
- `git diff --check`: passed.

The current test assertions report ownership/storage drift as warnings. This task changes neither those assertions nor any manifest, fingerprint, baseline, table allowance, legacy-sink classification or generated report. Passing tests do not establish a drift-free inventory. The two inventory warnings name these inherited items:

```text
src/doctor/stall_checks.py::_train_batch_findings (lines 640-648)
src/commands/integration_commands.py::IntegrationCommandsMixin._reopen_source_ci_once::99067e5d5da4bc5efd63f84a8dbf3d8a88481ddf555f7b70a646a51fe37b5581
src/commands/task_commands.py::TaskCommandsMixin._cmd_archive_task::513842f306aa56e3e63cfa84b52c59c27b3f94d7c784a662f64170e6c03e1d30
src/commands/task_commands.py::TaskCommandsMixin._cmd_archive_task::b611360d398cc725e9ada50f9112d9c7be31f747e1251e86de6d9f03c44ab5f7
src/database/queries/archive_queries.py::ArchiveQueryMixin._archive_task_on::6ee0195b41e365a4f138cbf111c69b21f7782f9b3bc5c059df2281abfd263cfe
src/integration/root_pr_recovery.py::recover_root_pr_checks::9838abf578dc8c2a386d1d4a9012e2aabe1e175732dd1d51dbe41a2ef7cfc4e9
src/orchestrator/execution.py::ExecutionMixin._complete_session_task_locked::91f3e9ef1047553b1b6a181d34a45e38af1c9c81aa3244f1605030fa2da742ba
src/orchestrator/execution.py::ExecutionMixin._complete_session_task_locked::ae8c919429f58dac2642af5184c5d10500c81e12bf5ef7db9da07251fc6b167d
src/orchestrator/execution.py::ExecutionMixin._complete_session_task_locked::cfcd13e9187c1de3a01142301b8203fae1e11a8a3a4ce9bf92d2a08ca1895e28
```

The remaining warnings are dependency deprecations. Existing true integration intent/retirement sinks remain classified and charged. This receipt does not waive the historical failures, release the original `wise-stone-87` Matter acceptance HOLD, qualify cache integrity, authorize live repair/deployment or cross-project inspection, change any batch/owner/ref/lease, or modify the published wait-classification scope of `bold-zenith-11.27`. Native routing remains unchanged.

Raw session evidence: `/tmp/wise-vault-57-claim-comparison.json`, `/tmp/wise-vault-57-inventory.json`, `/tmp/wise-vault-57-size.log`, and `/tmp/wise-vault-57-generated.log`. Exact identities, disposition reasoning, source revision, checks and retained drift are preserved here for durable publication.
