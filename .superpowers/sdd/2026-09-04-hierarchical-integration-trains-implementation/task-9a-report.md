# Task 9a implementation report

## Result

- Base: `5d8f84cccf3f07ec2fcf6c269a89457e40ee556c`
- Runtime/test commits: `205f825d`, `dc751f7f`
- Final implementation head before this report: `dc751f7f`
- Scope: Task 9a only. No CandidateService/build/rebuild/promotion, workflow/tick/cleanup
  wiring, protection enablement/probes, project enablement, live credentials, network calls, forge
  mutations, or operator database operations.
- `aq prime` was run exactly once. It returned `task_id is required (no task in scope...)`; this
  delegated shell therefore made no queue mutations.

## Implemented contracts

### Daemon GitHub App authentication

- `IntegrationConfig.github_app` is optional and stores only `client_id`, positive numeric
  `app_id`, positive numeric `installation_id`, and `private_key_path`.
- `OwnerFilePrivateKeyProvider` opens without symlink traversal and requires a regular file owned
  by the daemon user, owner-readable, and inaccessible to group/world. Key bytes are injected into
  the signer and never serialized in config.
- App JWT is RS256 with `iat=now-60`, `exp=now+540`, and `iss=client_id`.
- Before a token is usable, the adapter verifies JWT-authenticated `GET /app`, requests exactly one
  repository and `checks:write`, `actions:read`, `contents:write`, `administration:read`, permits
  only implicit `metadata`, then verifies exact numeric repository ID and `full_name` through
  `GET /repositories/{id}`.
- Installation tokens are cached behind one async lock and refreshed with five minutes remaining.
  Installation-authenticated 401 receives exactly one invalidation/refresh/retry.
- REST calls pin `Accept: application/vnd.github+json` and
  `X-GitHub-Api-Version: 2022-11-28`, bound responses and pagination, prohibit pagination away from
  `api.github.com`, and expose only typed safe error categories (`rate_limited`, `transient`,
  `credentials`, `permission`, `not_found_or_hidden`, `conflict_or_invalid`) without raw bodies.

### Credential-isolated Git push

- `GitManager.apush_oid_with_app_auth` validates immutable lowercase OIDs/ref, constructs a frozen
  `https://github.com/<trusted full_name>.git` target (never a mutable checkout origin), and uses an
  exact `--force-with-lease=refs/heads/<branch>:<expected-old>` refspec.
- The token is written to an unlinked seekable temporary file and exposed only as an inherited FD
  to one Git process. The only child values are the FD number and `x-access-token` username.
  Ambient `GH_TOKEN` and `GITHUB_TOKEN` are removed. Token bytes never enter argv, environment,
  Git config, output, or errors.
- The askpass helper is committed executable (`100755`). Timeout cleanup kills and reaps Git before
  the credential FD closes.

### Trust and attestation

- Trust schema: `aq.integration-trust.v1`; exact constants are
  `.github/agent-queue-integration.json` and
  `Agent Queue Integration Attestation`. An inactive replacement-value example is shipped at
  `.github/agent-queue-integration.example.json`; the actual authority path is reserved from
  ordinary delivery.
- Trust validation binds RepoConfig canonical ID, numeric GitHub repository ID/full name, distinct
  numeric CI and attestation App IDs, and a nonempty ordered unique versioned check set. Unknown
  fields and bool-as-int inputs fail closed.
- Attestation schema: `aq.integration-attestation.v1`, including canonical repository ID. Canonical
  bytes use `json.dumps(sort_keys=True,separators=(",",":"),ensure_ascii=True)`; external ID is
  `aq-attestation-v1:<sha256>`.
- Parser rejects unknown/missing/duplicate fields, noncanonical bytes, bool IDs, malformed/nonlower
  SHAs, duplicate checks/suites, incomplete suite coverage, and incoherent heads/attempts.
- Readers select the greatest numeric exact-name check-run ID from the attestation App. A malformed,
  red, or identity-skewed newest trusted record fails closed; older successes never win.
- Authenticated observation selects the greatest numeric exact-name check from the CI App for each
  required name, binds exact head/check suite/workflow run/run attempt, and permits multiple
  coherent workflows. Missing/pending is inconclusive; completed red/cancelled/skipped/neutral
  remains normalized non-green evidence.
- Publication writes canonical payload text and digest through the App-only Checks API. It reuses
  only a byte-identical newest valid trusted record; differing or newer invalid evidence is not
  mistaken for the old success.

### Durable CI evidence and consumers

- `ParentCISubject` and `CandidateCISubject` are disjoint frozen strict types. CIService rejects
  prose/dicts and accepts only `AuthenticatedGitHubObserver`, or the explicitly named
  `TrustedFixtureObserver` in tests.
- Production observer construction verifies configured App/repository/full-name/GitHub.com identity
  against the typed trust manifest.
- Parent operation/generation/head and root operation/batch/revision/candidate are revalidated under
  row locks before append. Root must match the manifest check set; a parent may use its frozen
  declared check set from `policy_snapshot`. If that declared transport is unavailable and differs
  from the root full suite, the service returns `full_suite_required` for Task 10 routing and never
  fabricates evidence.
- `IntegrationCIEvidenceAdapter` uses the landed append-only `integration_check_evidence` Task 6
  table. It writes normalized producer/workflow/run/attempt/check-set facts per workflow attempt for
  both subject kinds. Exact replay returns the original IDs even when observation time changes;
  the same attempt cannot be rebound to another subject. Conclusive failures are stored but never
  returned as green.
- Existing Task 6 parent verification and Task 7 repair/accounting suites were included in the
  affected-area gates. No database schema changed, so no migration or SQLite/PostgreSQL dialect
  cycle was applicable.

## TDD evidence

### Slice 1: configuration and provider

- RED: `pytest -q tests/test_config_validation.py -k GitHubAppConfig`
  - collection failed: `cannot import name 'GitHubAppConfig'`.
- GREEN: same command: `3 passed, 77 deselected`.
- RED: `pytest -q tests/test_github_app.py`
  - collection failed: `No module named 'src.git.github_app'`.
- GREEN after the provider implementation and focused correction: `3 passed`; owner/symlink RED
  later reproduced the uncaught `OSError: Too many levels of symbolic links`, then GREEN was
  `1 passed, 3 deselected`.
- One process deviation occurred and is intentionally preserved here: I ran
  `pytest -q tests/test_github_app.py tests/test_config_validation.py -k 'GitHubApp or github_app'`
  directly instead of through `aq test`; it returned `6 passed, 77 deselected`. All subsequent
  multi-file invocations used `aq test`; this run was not repeated to disguise the mistake.

### Slice 2: FD askpass

- RED: `pytest -q tests/test_git_app_auth.py`
  - collection failed: `No module named 'src.git.askpass_fd'`.
- The first whole-file GREEN attempt printed two dots and then hung. Controller observed PID
  `3880685` for about 13 minutes with no child processes, interrupted the turn, and TERM'd exactly
  that PID.
- Root-cause investigation collected four tests, then ran every node individually under
  `timeout --signal=TERM 8s pytest -vv -s ...`; all four completed including teardown in
  `0.32-0.35s`, exit 0.
- Proven hypothesis: the original helper test consumed the pipe once, then called `os.read` again
  while its write descriptor remained open, so EOF was impossible and the block was in the test
  body—not Git, cleanup, or pytest shutdown. Small diagnostic:
  - writer open: printed first read and `before-second writer-open`, external timeout exit `124`;
  - writer closed: second read returned `b''`, exit `0`.
- The production-shaped regression now uses the same unlinked seekable `TemporaryFile` FD, which
  has deterministic EOF and no writer-end lifecycle. Fix-attempt count: **1**.
- A bounded timeout regression then correctly RED-failed because the App push did not kill/reap a
  timed-out Git child (`process.killed is False`). The minimal fix copied the existing
  `_arun_unlocked` kill/wait pattern. GREEN: `1 passed`; the whole focused file then completed twice
  under the external 8-second bound: `5 passed` in `0.33s` each, both exit 0.
- Packaging inspection found the helper indexed as `100644` despite a locally executable file.
  A real inherited-FD direct-execution test passed locally and `git update-index --chmod=+x`
  recorded `100755`; final focused result: `6 passed`.

### Slice 3: manifest and attestation

- RED: `pytest -q tests/test_integration_ci.py`
  - collection failed: `No module named 'src.integration.ci'`.
- Subsequent narrow REDs covered missing authenticated observer, missing publisher, reserved trust
  path, and byte-identical publication reuse.
- GREEN progression: `3 passed`, `5 passed`, `6 passed`; final single-file CI contract run after
  durable service work: `13 passed`.
- The Pydantic field-shadow warnings were removed by using alias-backed wire field `schema`; only
  repository-wide dependency deprecation warnings remain.

### Slice 4: durable parent/root evidence

- RED: focused CIService collection failed on missing typed subjects/service.
- GREEN: parent persistence and untyped rejection `2 passed`.
- Root exact batch/revision/candidate test: `1 passed`.
- Replay RED proved an identical attempt with a later `observed_at` was incorrectly treated as
  rebinding; GREEN excludes only ID/observation time from immutable replay comparison.
- Final single-file service result: `13 passed`, including parent/root subject skew, explicit
  full-suite fallback outcome, conclusive failed persistence, exact replay, and caller-dict denial.

## Final verification

- Full affected-area gate after the last CI runtime changes:

  `aq test tests/test_config_validation.py tests/test_github_app.py tests/test_git_app_auth.py tests/test_integration_ci.py tests/test_git_manager_async.py tests/test_integration_parent_completion.py tests/test_integration_repair.py -x`

  Result: **321 passed, 11 dependency warnings, 0 failed in 19.07s** (`-n 3`, slot 1 of 2).
- Post-packaging focused gate:

  `aq test tests/test_git_app_auth.py tests/test_git_manager_async.py -x`

  Result: **168 passed, 11 dependency warnings, 0 failed in 9.66s** (`-n 3`, slot 1 of 2).
- Changed-file lint:

  `ruff check src/config.py src/git/github_app.py src/git/askpass_fd.py src/git/manager.py src/integration/ci.py tests/test_config_validation.py tests/test_github_app.py tests/test_git_app_auth.py tests/test_integration_ci.py`

  Result: `All checks passed!`
- `git diff --check`: exit 0, no output.

## Files

- `.github/agent-queue-integration.example.json`
- `src/config.py`
- `src/git/askpass_fd.py`
- `src/git/github_app.py`
- `src/git/manager.py`
- `src/integration/ci.py`
- `tests/test_config_validation.py`
- `tests/test_git_app_auth.py`
- `tests/test_github_app.py`
- `tests/test_integration_ci.py`

## Self-review and concerns

- Secret sentinels are absent from argv/env/error representations, and no code consults ambient
  worker token variables. All target URLs derive from the already-validated full-name binding.
- Numeric trust inputs use strict integer checks, so booleans cannot impersonate IDs. Every newest
  record selection uses numeric IDs and has no older-record fallback.
- CIService never accepts caller evidence dictionaries. The fixture seam is conspicuously test-only,
  and production provider identity is cross-checked at construction.
- The existing evidence schema already supports both Task 6 parent and Task 9 root candidate
  identities and already has append-only database guards; adding a duplicate table or migration
  would have split the accounting authority.
- Task 10 must route `full_suite_required`, authenticated publication, and attestation lookup into
  workflows. Task 11 must perform live protection/probe/cutover. Neither future behavior was faked
  here.
- No live API/GitHub credential test was performed by design. All HTTP and Git remote behavior is
  fake or temporary/local.
