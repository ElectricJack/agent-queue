# GitHub access #615 acceptance record

Recorded 2026-09-23 UTC for task `fair-bridge.4`. **Disposition: live acceptance
not run; issue #615 completion is not recommended.** This is an evidence record
against [the specification](../specs/github-access.md#11-verification-and-acceptance)
and [the disposable repository runbook](../plans/github-access/acceptance-runbook.md),
not a sign-off on the private repository workflow.

Live acceptance is tracked by task `prime-grove`, which is **BLOCKED** until the disposable fixtures are supplied.

The preceding disposition records the state when `fair-bridge.4` closed. The
subsequent live run and its current result are recorded below.

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

## Live disposable run: `gh615-20260923` (in progress)

Task `prime-grove` started a separate App daemon and a separate existing-login
daemon against disposable private repositories on 2026-09-23 UTC. This is live
GitHub evidence. **Current disposition: acceptance failed at App onboarding
and AQ task close; issue #615 completion is not recommended.** The App
installation permission update has propagated and direct token mint works,
but AQ's bootstrap response read is incomplete. Source repairs are tracked
as `agile-delta` (App permission key), `prime-forge` (response read) and
`fair-stone` (task-close tracking ref). All isolated daemons were stopped after these checks. The disposable
repositories and isolated fixture data are retained for the approved rerun;
final cleanup is not yet claimed.

| Item | Observed value |
| --- | --- |
| Source revision | `65e2cfb5cdecd4c07fc97437187b22e043296b25` on `aq/prime-grove` before the evidence edit |
| AQ version | `aq, version 0.1.0` |
| Real GitHub CLI | `gh version 2.45.0` (Ubuntu `2.45.0-1ubuntu0.3`); this run does not certify other versions |
| App fixture | `ElectricJack/aq-gh615-app-fixture-20260923`, repository ID `1384141153`, App ID `5052310`, installation ID `164168761` |
| Login fixture | `ElectricJack/aq-gh615-login-fixture-20260923`, repository ID `1384141270`, existing-login actor `ElectricJack` |
| Isolation | Separate disposable PostgreSQL databases `aq_e2e_gh615_app_20260923` and `aq_e2e_gh615_login_20260923`, data/workspace roots and API ports `18155` and `18156`. The App process had no `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`, worker database scope, or operator database URL; it used an empty `GH_CONFIG_DIR`, disabled global Git configuration, and disabled SSH identity discovery. |

### App-only result: failed before clone

The isolated App daemon reported `credential_mode=app`, `authenticated=true`,
App ID `5052310` and installation ID `164168761`. The public CLI command
`aq project onboard --source-mode github_clone --root-id e2e-onboarding
--relative-path gh615-20260923/app --project-id app-gh615-20260923 --github-url
https://github.com/ElectricJack/aq-gh615-app-fixture-20260923 --json` returned
`github_repository_inaccessible` in `preflight`. No clone, branch, PR or remote
write followed.

Sanitized direct tracing of the same configured access object observed
`GET /app` HTTP 200, then `POST /app/installations/164168761/access_tokens`
HTTP 422 with GitHub's message that requested permissions were not granted.
The live installation reported `checks:read`, `actions:read`, `contents:write`,
`metadata:read`, `pull_requests:write` and `actions_variables:read`. The source
at [`src/git/github_app.py`](../../src/git/github_app.py) requests
`checks:write`, `administration:read`, `issues:write` and `variables:read`
among its token permissions. `variables` also differs from the GitHub
permission key `actions_variables`. A source repair was filed as
`agile-delta`; the additional App permissions await owner approval. This is a
failed live gate, not an App credential-parity result. The installation selects
only this App fixture repository, so live concurrent repository token
separation also needs a second disposable repository selected for the App.

### App permission-update rerun before installation approval

After the fixture owner reported an App permission change, the isolated App
daemon was restarted from `agile-delta` fix
`9c12d1df7c97650a746fa842e6d74cb9207b0ff7`, which requests
`actions_variables` instead of `variables`. On 2026-09-23 UTC, authenticated
`GET /app` returned HTTP 200 and showed the App registration now has
`checks:write`, `administration:read`, `issues:write` and
`actions_variables:read`. Authenticated `GET /app/installations/164168761`
also returned HTTP 200, but the installation
still showed `checks:read` and had no `administration` or `issues` grant.

With the corrected source, installation-token mint scoped to repository
`1384141153` returned HTTP 422: “The permissions requested are not granted
to this installation.” The AQ `github_clone` retry with request ID
`gh615-app-onboard-2` again returned `github_repository_inaccessible` in
`preflight`. It made no clone or remote write. This confirms the source-key
repair alone was insufficient at that point; the fixture installation still
needed the permission update applied. The credential
daemon was stopped after this check, and the App fixture remains for the
approved rerun.

### App rerun after installation approval: token minted, AQ bootstrap failed

After the owner accepted the installation update, authenticated
`GET /app/installations/164168761` returned HTTP 200 with `checks:write`,
`administration:read`, `issues:write` and `actions_variables:read`. A direct
`POST /app/installations/164168761/access_tokens` using the fixed source's
permission set and repository ID `1384141153` returned HTTP 201. Its response
selected exactly `ElectricJack/aq-gh615-app-fixture-20260923` and reported the
requested grants, with expiry `2026-09-23T23:16:28Z`. The token was not
printed or retained in evidence.

The isolated daemon used fixed source
`9c12d1df7c97650a746fa842e6d74cb9207b0ff7`. AQ `github_clone` request
`gh615-app-onboard-3` still returned `github_repository_inaccessible` in
`preflight`. A separate call through production `GitHubAccess.bind_repository`
raised “GitHub response was not valid JSON” during token bootstrap. A
sanitized instrumented transport call measured HTTP 201 with
`Content-Length: 6774`, while `AiohttpTransport.request` returned only 239
bytes, which were incomplete JSON. The transport calls
`response.content.read(max_bytes + 1)` once; that read can return one partial
chunk. Task `prime-forge` tracks a bounded full-response repair. AQ made no
clone or remote write in this attempt. Held-branch push, App PR, CI and merge
remain `not_run` live. The credential daemon was stopped after the check.

### Existing-login result: remote PR and merge verified, AQ close blocked

The other isolated daemon reported `credential_mode=existing_login`,
`login=ElectricJack`. The same onboarding command shape with the login URL,
project ID `login-gh615-20260923` and request ID `gh615-login-onboard-2`
cloned the private repository, registered workspace
`login-gh615-20260923-primary`, and detected `main`. The first attempt with
relative path `gh615-20260923/login` returned `registration_failed` in
`prepare` because that relative path's parent did not yet exist. Creating the
parent directory inside the disposable project root and retrying with a new
request ID succeeded; no remote write occurred in the failed attempt.

Isolated fixture task `bold-nexus` was claimed by its fake-provider pool
session. `aq prime` identified held branch `aq/bold-nexus`. A harmless file
commit produced local OID `d9f4ce082efd956e43e6705a9f60105472daff1a`.
`aq git push --json` returned that branch and OID; an independent GitHub ref
read matched it. `aq git create-pr` opened [fixture PR 1](https://github.com/ElectricJack/aq-gh615-login-fixture-20260923/pull/1).
Repeating the command returned the same URL, and GitHub listed only that one
PR. A separate remote read identified creator `ElectricJack`, head
`d9f4ce082efd956e43e6705a9f60105472daff1a`, and base
`dea56cd9ab39a522d5cd6f064929030a3ee9f4bc`. Both fixture CI checks
passed.

The isolated operator called `aq git pr-merge --project-id
login-gh615-20260923 --pr-url
https://github.com/ElectricJack/aq-gh615-login-fixture-20260923/pull/1
--method squash --json`. The client returned an **unknown outcome** after
30 seconds. No retry was issued. A later independent GitHub read confirmed
the PR merged at `2026-09-23T21:32:32Z` by `ElectricJack`; remote `main`
equals merge OID `ce71d41853e2d51e769eb79e3317755a39d384c8`, with
the original base as its parent. The task branch ref returned HTTP 404 after
remote cleanup. Closing fixture task `bold-nexus` with a shipped outcome then
returned `pipeline_ok=false`, status `BLOCKED`; `aq task explain` reported
`session_close_pipeline_stop`.

A second held fixture task `bold-flare` tested task close **while its PR was
still open and green**. `aq git push` published branch `aq/bold-flare` at
`c49e332e4e6f965c35bdaff2aa8deac6581ec5b6`; its client response was
unknown after 30 seconds, so the remote ref was checked before proceeding.
`aq git create-pr` opened [fixture PR 2](https://github.com/ElectricJack/aq-gh615-login-fixture-20260923/pull/2),
and both fixture checks passed. Closing `bold-flare` also returned
`pipeline_ok=false`, status `BLOCKED`. The isolated daemon log identifies the
cause in both closes: the completion pipeline tried `git rev-list
refs/remotes/origin/<task-branch>..HEAD --count`, but AQ's successful push
had not created that local remote-tracking ref. It treated the missing ref as
an unfixable verification failure, despite the exact branch OID existing on
GitHub. Task `fair-stone` tracks this source repair. The first close failure
was therefore **not** caused by merging before close.

Before merging PR 2, the production Git manager validated head
`c49e332e4e6f965c35bdaff2aa8deac6581ec5b6` and base
`ce71d41853e2d51e769eb79e3317755a39d384c8`. A separate disposable
checkout advanced its head once to `a670e8cd1eb50358b1d0c06b212ae72f1432f865`.
Calling the production merge method with the pinned old head returned
`success=false` and an explicit head-moved refusal; GitHub still showed PR 2
open and `main` unchanged. After the new head passed CI, `aq git pr-merge`
returned success with a green CI verdict and base state `current`.
Independent GitHub reads confirmed merge by `ElectricJack` at
`2026-09-23T21:58:22Z`, new `main` OID
`725b217262412548cc1b7ce60ebcb6093accf30f`, and HTTP 404 for the
deleted task-branch ref.

For a live foreign-target check, the existing-login daemon also onboarded the
disposable App fixture repository as a **different** project,
`foreign-gh615-20260923`. Asking `aq git pr-merge` for that project with the
login repository's PR 2 URL refused it because the PR reference did not match
the project's authorized repository. Both repositories' `main` OIDs were
unchanged by the refused call.

Separate disposable daemons on ports `18157` and `18158` tested environment
credential discovery. Each had an empty `GH_CONFIG_DIR`, no stored login,
and exactly one credential variable in its daemon environment. The
`GH_TOKEN` instance and the `GITHUB_TOKEN` instance both reported
`credential_mode=existing_login`, `login=ElectricJack`, then successfully
onboarded the same private login fixture by URL and detected `main`.
These were read/clone checks; PR, CI and merge were exercised in the
stored-login instance above. Both environment-token daemons were stopped
after verification. No token value was logged or placed in this record.

### Remaining runbook steps and repeatable checks

| Step | Current result |
| --- | --- |
| App registration update and installation token mint | App and installation now have requested grants; direct fixed-source mint HTTP 201 for the selected fixture repo |
| AQ App token bootstrap and URL onboarding | `fail`: HTTP 201 token response truncated to 239 of 6774 bytes by one read; `github_clone` preflight still failed |
| App-only held-branch push, PR, CI, immutable merge, integration and WIP publication | `not_run` after App token HTTP 422 |
| App token expiry refresh and concurrent repository separation | `not_run` live; mock-only evidence above |
| Existing-login stored-auth onboarding, held-branch push, PR idempotency, CI, AQ merge | Live GitHub writes and exact OIDs verified in two PRs; AQ task close `BLOCKED` on both |
| Existing-login `GH_TOKEN` and `GITHUB_TOKEN` discovery in separate instances | Live auth-status and private URL onboarding passed; no remote writes in these two instances |
| App denial with valid ambient PAT and independent no-fallback audit | `not_run`; current App failure alone does not prove this case |
| Foreign PR/cross-project rejection | Live AQ refusal on a PR URL from the other disposable project; both `main` OIDs unchanged |
| Stale-revision refusal | Live production-method refusal on PR 2, with remote `main` unchanged before the later validated merge |
| Final fixture cleanup | Four isolated daemon ports confirmed closed; disposable repositories, App installation and fixture data retained pending owner approval and remaining validation |

At this source revision, the declared focused command
`POSTGRES_TEST_DSN=postgresql+asyncpg://agent_queue:agent_queue_dev@localhost:5533/postgres
aq test tests/test_github_access_workflow.py
tests/test_github_access_architecture.py tests/test_worker_git_scope.py`
finished **126 passed, 3 skipped, exit 0** in 64.08 seconds. It remains
mock-only evidence. No full-suite run was made.
