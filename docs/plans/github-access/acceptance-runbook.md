# Private GitHub access acceptance runbook

This runbook is for `fair-bridge.4`, the live acceptance gate in
[§11 of the specification](../../specs/github-access.md). The deterministic
process and local Git fixture is `tests/test_github_access_workflow.py`. Its
results are **mock-only evidence**. A live pass requires a separately
provisioned disposable private GitHub repository and App installation.

## Fixture inputs and isolation

Record these values in a private run sheet before starting. Do not put tokens,
private keys, credential-helper output, or complete process environments in
the evidence file.

| Input | Required value |
| --- | --- |
| `RUN_ID` | Unique suffix for this acceptance run. |
| `APP_REPO_URL`, `LOGIN_REPO_URL` | Two newly created, private, disposable GitHub repositories owned by the fixture operator. Each has a default-branch commit and passing required CI. |
| `APP_ID`, `INSTALLATION_ID`, `APP_KEY_FILE` | App installed on `APP_REPO_URL` with repository contents, metadata, pull request, checks and workflow permissions used by AQ. The key file is private to the isolated daemon. |
| `APP_ACTOR`, `LOGIN_ACTOR` | Expected GitHub actor names, determined by fixture provisioning, for independent audit verification. |
| `AQ_TEST_CONFIG`, `AQ_TEST_DATA`, `AQ_TEST_DB`, `AQ_TEST_ROOT_ID`, `AQ_TEST_ROOT` | Fresh isolated AQ configuration, data directory, disposable PostgreSQL database and writable project root. None points at the operator's AQ database, vault or working repositories. |
| `LOGIN_GH_CONFIG` | Separate temporary `gh` configuration containing the existing-login fixture account. |
| `EVIDENCE_DIR` | New directory accessible only to the fixture operator. |

Provision CI and the App before running. Confirm repository visibility is
private and both URLs are on `github.com`. Start a **separate** AQ daemon from
`AQ_TEST_CONFIG`, with its own database and workspace root. Do not run
`aq start`, `alembic upgrade` or `alembic stamp` from a worker slot. App mode
must run with no `GH_TOKEN`, `GITHUB_TOKEN`, stored `gh` login, personal Git
credential helper or usable SSH key in the daemon environment. A separate
read-only verifier account may inspect GitHub after the run; its credentials
must never enter the AQ daemon environment. For the deliberate poisoned-PAT
case, add a valid PAT only to a **new isolated daemon instance** with the App
still configured and deny the App installation's repository permission. The
operation must fail; the PAT must remain unused.

## Repeatable automated gate

Use only the disposable PostgreSQL test service for `POSTGRES_TEST_DSN`. The
test harness creates and deletes its own scratch databases. Run the declared
focused command from the checkout:

```bash
aq test tests/test_github_access_workflow.py tests/test_github_access_architecture.py tests/test_worker_git_scope.py
```

Record the exact command, commit SHA, test count and result as `mock-only`.
The workflow test runs `prime`, `git_push` and `git_create_pr` through the
command handler with a held worker session, a local bare Git remote and a
process-backed fake `gh`. The same PR, polling, CI and merge sequence runs
under App and existing-login credentials. It also covers App denial with an
available ambient PAT, expiry refresh and concurrent repository selection.
The focused architecture and worker scope suites retain their own invariants.
The architecture test covers the migrated repository, CI and onboarding
surfaces; it is not a whole-tree proof while the legacy
`GitPlugin.create_github_repo` command still calls `GitManager.acheck_gh_auth`
and `acreate_github_repo`, which launch `gh` independently. Resolve that path
before marking the specification's single-launcher gate complete.

## Live App-only pass

1. Record `RUN_ID`, source revision, AQ version, repository numeric ID,
   `APP_ID`, `INSTALLATION_ID`, and a UTC start time in
   `EVIDENCE_DIR/app.json`. Record identities only; never record token values.
   Start the isolated daemon with the App configured. Check
   `aq project get-github-auth-status` and record its mode and nonsecret App
   identity. Verify `GH_TOKEN` and `GITHUB_TOKEN` are absent from the daemon
   environment, its `gh` config is empty, and no SSH identity is usable.
2. From the isolated operator CLI, run `aq project onboard --source-mode
   github_clone --root-id "$AQ_TEST_ROOT_ID" --relative-path
   "$RUN_ID/app" --project-name "App acceptance $RUN_ID" --project-id
   "app-$RUN_ID" --github-url "$APP_REPO_URL" --json`. Save the sanitized
   response and the fresh workspace path. A `github_operation_unsupported`
   response is a failed gate, not a successful preflight.
3. Create and claim one task in that project using the normal scheduler. In
   its worker session run `aq prime`, commit a harmless fixture file, then
   follow the emitted worker instructions to run `aq git push --json` and
   `aq git create-pr --title "Acceptance $RUN_ID" --body "Disposable
   fixture" --json`. Save the task ID, branch, exact local and remote OIDs,
   PR URL and nonsecret command results. The branch must be the held task's
   branch. Reject any attempt to publish a foreign branch or repository.
4. Poll via AQ task/integration status and inspect the PR's CI verdict using
   the configured AQ merge path. Record the PR head OID and base OID before
   merge. Exercise the exact merge-diff fetch and immutable validation path:
   after validation, move the PR head once in a separate fixture branch and
   confirm AQ refuses the stale revision; restore or create a new validated
   revision before proceeding. Under PR-based publication use
   `aq git pr-merge --project-id "app-$RUN_ID" --pr-url "$PR_URL"
   --method squash --json`. Under AQ development integration, use
   `aq integration status "app-$RUN_ID" --json` and the configured
   integration publication flow; do not independently merge its PR.
5. Confirm the remote default-branch OID equals the validated merge result,
   and that the task branch was deleted or recorded as cleanup outstanding.
   Run the configured integration batch and a recovery/WIP publication for
   separate fixture tasks; record candidate/WIP branch names, exact remote
   OIDs, status transitions and cleanup results. Expire the installation
   token between two operations and confirm both succeed with the same App
   identity. Verify the actor for each GitHub write independently using the
   fixture's GitHub audit view or a separate read-only verifier session.

Any failed, ambiguous or partial write is investigated from the remote state.
Do not replay PR creation, merge or cleanup blindly. Save sanitized AQ
responses, remote OIDs, PR numbers, CI conclusions, actor names and UTC times
only. Review the evidence file for `ghp_`, `github_pat_`, `ghs_`, `GH_TOKEN=`,
`GITHUB_TOKEN=` and key material before sharing it.

## Existing-login parity and negative cases

Repeat the same supported workflow on `LOGIN_REPO_URL` in a **new** isolated
daemon instance with App configuration removed and `LOGIN_GH_CONFIG` selected.
Run three credential-discovery cases in separate instances: stored `gh` login,
`GH_TOKEN`, and `GITHUB_TOKEN`. Record which identity AQ reports and verify
the expected actor independently. Preserve the stored login before and after
the environment-token cases. Compare command results, CI verdict, immutable
merge behavior, remote OIDs and cleanup with the App run.

Run the App permission-denial case with a valid ambient PAT deliberately
available in the isolated daemon. Deny the App's access to the disposable
repository, then attempt a repository read and PR write through AQ. Both must
fail with safe credential/permission diagnostics; neither may appear in the
PAT actor's audit trail. Restore App permissions and restart the isolated
daemon before any further positive test. Also exercise concurrent requests
to both disposable repositories, a token-expiry refresh, a foreign PR URL and
an attempted cross-project repository target. Record the rejection categories
and confirm no unintended remote ref changed.

## Evidence and cleanup

Write `EVIDENCE_DIR/result.json` with `run_id`, UTC timestamps, source commit,
AQ version, fixture repository names and numeric IDs, nonsecret credential
identities, each operation's command/result category, PR number, branch and
OID, CI verdict, actor, cleanup state, and `pass|fail|not_run` per step. Label
the automated suite `mock-only` and this run `live-disposable` only after the
remote actor and OIDs have been checked. Mark missing fixture inputs or steps
`not_run`; do not turn them into a pass.

After collecting evidence, stop the **isolated** daemon, remove its tasks and
project records through that instance's operator surface, delete both
disposable GitHub repositories and the App installation used for the run,
revoke fixture login/PAT credentials, remove `LOGIN_GH_CONFIG`, `AQ_TEST_ROOT`,
`AQ_TEST_DATA`, `AQ_TEST_CONFIG` and the disposable test database, then record
UTC cleanup time and deletion confirmation. Retain only sanitized evidence.
Never delete an operator repository or the database in `~/.agent-queue`.

App-backed `github_clone` requires the onboarding client and Git manager to
share the daemon's one App access object. The deterministic test exercises
that binding, but the live App-only gate has not been run. No live acceptance
result is claimed here.
