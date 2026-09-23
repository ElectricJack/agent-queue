# GitHub access #615 acceptance record

Recorded 2026-09-23 UTC for task `fair-bridge.4`. **Disposition: live acceptance
not run; issue #615 completion is not recommended.** This is an evidence record
against [the specification](../specs/github-access.md#11-verification-and-acceptance)
and [the disposable repository runbook](../plans/github-access/acceptance-runbook.md),
not a sign-off on the private repository workflow.

## Source and test environment

| Item | Observed value |
| --- | --- |
| Source revision tested | `20dd3db95275f1ff4719a083e8669c5126bd8cfe` (`origin/main` at the start of this task) |
| Worker branch | `aq/fair-bridge.4` |
| Installed `gh` | `gh --version` reported `2.45.0` (Ubuntu package `2.45.0-1ubuntu0.3`) |
| Live supported `gh` version | **Unverified.** The automated suite uses a fake `gh` process; no version is certified by this record. |
| GitHub fixture | No disposable repository URLs, App installation/key path, login fixture or isolated AQ instance was provided to this worker. The slot's `origin` is `ElectricJack/agent-queue`, so it was not used for acceptance. |

The missing fixture details were requested from `user:dashboard` in AQ message
`msg-4572f9a7770541398b73d6039592277b`. No App private key, token or
personal login was copied into this worker slot. An isolated daemon was not
started from the worker slot.

## Automated evidence: mock only

The repository's disposable PostgreSQL service was healthy. The first `aq
test` invocation without `POSTGRES_TEST_DSN` exited 4 before collection; it
ran no tests. The rerun used the base DSN published in `docker-compose.yml`;
the test harness leased separate scratch databases:

```text
POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue:agent_queue_dev@localhost:5533/postgres aq test tests/test_github_access_workflow.py tests/test_github_access_architecture.py tests/test_worker_git_scope.py
```

Result: **126 passed, 3 skipped, exit 0**. The skips are parameter cases that
do not apply to the selected credential mode. This is process-backed fake
`gh` and local Git evidence, not a live GitHub or daemon acceptance result.

| Verified in the automated command | Evidence |
| --- | --- |
| App and existing-login PR, CI and merge behavior | [`test_private_repository_pr_ci_merge_workflow_has_credential_parity`](../../tests/test_github_access_workflow.py) |
| App denial does not launch a personal-token fallback, even with an ambient PAT | [`test_app_denial_never_uses_available_personal_token`](../../tests/test_github_access_workflow.py) |
| Expiry refresh and concurrent repository token separation | [`test_app_expiry_and_simultaneous_repositories_keep_tokens_separate`](../../tests/test_github_access_workflow.py) |
| Audit PR idempotency, held-branch push and PR command, and URL onboarding | [`test_integration_audit_publication_reconciles_by_idempotency_key`](../../tests/test_github_access_workflow.py), [`test_prime_worker_push_and_pr_use_held_branch_and_shared_credential`](../../tests/test_github_access_workflow.py), [`test_operator_onboards_private_url_through_shared_access`](../../tests/test_github_access_workflow.py) |
| Migrated surface architecture and worker Git command scope | [`tests/test_github_access_architecture.py`](../../tests/test_github_access_architecture.py), [`tests/test_worker_git_scope.py`](../../tests/test_worker_git_scope.py) |

The architecture ratchet explicitly covers only its listed migrated surfaces.
The legacy `GitPlugin.cmd_create_github_repo` still calls
`GitManager.acheck_gh_auth` and `GitManager.acreate_github_repo` in
[`src/plugins/internal/git.py`](../../src/plugins/internal/git.py) and
[`src/git/manager.py`](../../src/git/manager.py). Those methods independently
launch `gh auth status` and `gh repo create`. Thus the specification's
whole-tree single-launcher condition is not yet met, regardless of the
automated test result. Final removal belongs to migration step 5.

## Live disposable repository evidence

All entries below are **not_run**. No result is inferred from the fake process
or from the project repository. There are no live PR numbers, remote OIDs,
CI conclusions, authenticated actor observations or cleanup confirmations.

| Runbook step | Status | Missing evidence |
| --- | --- | --- |
| App-only isolation and repository binding | `not_run` | Private fixture repository, installed App identity/key path, isolated daemon and credential-free environment |
| Explicit-URL onboarding and fresh workspace acquisition | `not_run` | AQ project/workspace response and repository numeric ID |
| Worker commit, exact-OID push and normal PR creation | `not_run` | Held task branch, remote OID, PR URL and App actor |
| PR polling, CI inspection, stale-revision refusal, validated merge and branch cleanup | `not_run` | CI verdict, immutable head/base/merge OIDs and cleanup state |
| Integration publication and recovery/WIP delivery in configured workflows | `not_run` | Separate tasks, candidate/WIP branches, exact remote OIDs, transitions and actors |
| Installation-token refresh | `not_run` | Two real operations across expiry with the same nonsecret App identity |
| Existing-login parity: stored login, `GH_TOKEN`, `GITHUB_TOKEN` | `not_run` | Separate isolated instances, matching workflow results, actor and remote OIDs |
| App denial with an available PAT | `not_run` | Safe AQ failures and independent audit proof that the PAT actor performed no write |
| Final fixture cleanup | `not_run` | Deletion/revocation confirmation and UTC time |

Before recommending #615 completion, finish the legacy-launcher removal,
provision the disposable inputs in the runbook, and run its full App-only and
existing-login sequences. Record sanitized commands/results, the supported
`gh` version, exact remote revisions, actor checks and cleanup in this gate.
