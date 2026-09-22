---
project: agent-queue
status: draft
source_repository_path: docs/specs/github-access.md
issue: https://github.com/ElectricJack/agent-queue/issues/615
---

<!-- Execution snapshot for the explicitly filed GitHub access epics. Relative source links refer to the repository copy. Do not approve this snapshot through the spec-ingest pipeline: its tasks are filed directly with aq task create --graph. -->

# Unified GitHub access through `gh`

Date: 2026-09-22

Status: proposed design; the single execution path and credential precedence
are agreed direction. The refactor is not implemented.

Issue: [#615 — GitHub App credentials do not cover gh-based PR operations](https://github.com/ElectricJack/agent-queue/issues/615)

AQ will use one implementation of each GitHub operation, executed through the
GitHub CLI (`gh`). A configured GitHub App supplies short-lived installation
tokens to that implementation. Without an App, the same implementation uses
the daemon user's existing `gh` credentials, including a personal access token
(PAT). An existing repository's supported AQ workflow must work with either
credential source, without requiring both.

This is a design record for future behavior. See
[Integration](../concepts/integration.md) and
[Project onboarding](../guides/project-onboarding.md) for current behavior.

## 1. Problem and evidence

The current code has several GitHub execution paths:

| Area | Current implementation | Gap |
| --- | --- | --- |
| Integration services | `GitHubAppClient` performs HTTP requests; `GitHubCLIClient` subclasses it and overrides transport | Authentication choice also selects the transport implementation |
| Ordinary PR and CI operations | `GitManager` invokes `gh` with its shared subprocess environment | App tokens never reach these calls |
| Task delivery and merge validation | Ordinary Git pushes; validation fetches use `gh auth git-credential` | These still depend on ambient credentials |
| Project onboarding | `projects.github.GhClient` has another subprocess runner | Its login checks and operations assume a user identity |
| Profile sharing | Profile commands spawn `gh gist` directly | Another execution and error-handling path |

Source: [src/git/github_app.py](../../src/git/github_app.py),
[src/git/github_cli.py](../../src/git/github_cli.py),
[src/git/manager.py](../../src/git/manager.py),
[src/projects/github.py](../../src/projects/github.py), and
[src/commands/profile_commands.py](../../src/commands/profile_commands.py).
The App/CLI client selection is currently inside
[src/orchestrator/core.py](../../src/orchestrator/core.py).

Two details affect the scope. `_create_pr_for_task` in
[src/orchestrator/git_ops.py](../../src/orchestrator/git_ops.py) is deprecated
and has no production caller; repairing that helper alone would not repair
normal worker delivery. The shipped
[completion protocol](../../src/prime/templates/completion_protocol.md)
instructs workers to run `git push` themselves. Also, `_apr_delivery_diff`
fetches the exact commits needed to approve a merge; authenticating only the
final `gh pr merge` command would still leave private-repository merges broken.

## 2. Decisions and scope

1. **One GitHub execution path.** All AQ-owned GitHub API, PR, repository and
   gist commands use one shared `gh` runner. Each operation has one
   implementation shared by both credential modes. There is no selectable
   HTTP backend, transport registry, or permanent old/new implementation flag.
2. **Credentials are selected separately.** Keep the existing
   `integration.github_app` configuration. Its presence selects App credentials
   for AQ's GitHub operations, including operations outside integration. Its
   absence retains the daemon user's existing `gh` authentication.
3. **No silent fallback.** An App binding, minting, refresh or permission
   failure never causes a retry with an ambient PAT, stored login or SSH key.
4. **Complete the existing-repository workflow.** Cover private-repository
   onboarding by explicit URL, workspace acquisition, fetch, worker branch
   publication, PR creation/polling, CI inspection, merge validation, merge,
   integration publication, recovery and remote cleanup.
5. **Preserve delivery and trust rules.** Authentication unification does not
   change which task may push a branch, who may publish, what CI is required,
   or which immutable revision was reviewed.

The one direct-HTTP exception is App authentication bootstrap: identifying the
App and minting installation tokens. It is a small fixed-endpoint component,
not an alternate implementation of repository operations. Git object transfer
still uses Git, sharing the credential selection described here.

This refactor does not add GitHub Enterprise, multiple App installations,
automatic fork provisioning, broader App permissions, or new authentication
for arbitrary worker shell commands. Local repositories and non-GitHub remotes
retain their existing Git behavior and never receive a GitHub App token.

## 3. Operator behavior and compatibility

| Configuration and operation | Result |
| --- | --- |
| No App, stored `gh` PAT/login | Use that login under the daemon's OS user |
| No App, environment token | Preserve `gh` precedence: `GH_TOKEN`, then `GITHUB_TOKEN`, then stored credentials |
| App configured, repository accessible to installation | Supply its repository-scoped token as `GH_TOKEN` for that invocation |
| App configured, personal credentials also available | Use the App for AQ; leave the stored login and parent environment untouched |
| App configured, repository inaccessible or App invalid | Report the failure; do not try personal credentials |
| `gh` missing | Report `github_cli_missing` in either mode |

`GH_TOKEN` overriding stored credentials is documented by
[GitHub CLI](https://cli.github.com/manual/gh_help_environment).
AQ does not run `gh auth login`, `logout`, `setup-git` or `auth token` as part
of normal operation, and does not persist an App token in `hosts.yml`.

No configuration or database migration is required. The existing App setting
gains coverage; it is not renamed during this refactor. `gh` becomes required
for App-backed API operations as well as existing-login operations. Installation
and deployment documentation must state this dependency.

The provider is constructed from the daemon's effective configuration at
startup. Applying a credential-mode, installation or key-reference change
requires a daemon restart in this version. Configuration surfaces must state
that boundary; they must not imply that changing the YAML has switched live
requests. Tokens and clients from different effective configurations are never
reused. Ordinary token expiry is handled in memory without a restart.

## 4. Contributor architecture

Use small concrete components with injected dependencies for tests:

| Component | Responsibility |
| --- | --- |
| `GitHubAuth` | Select the configured credential mode; own App token caching and expose non-secret authentication identity |
| `AppTokenProvider` | Sign App JWTs, perform fixed bootstrap requests, and validate installation-token responses |
| `GhRunner` | The only AQ-owned process launcher for GitHub commands; scope, environment, deadlines, bounded output and safe errors |
| `GitHubClient` | Repository-bound operations, validation and response decoding, all using `GhRunner` |
| `GitManager` | Local Git and authenticated object/ref transfer, using the same credential authority |

The intended file split is `src/git/github_auth.py` for credential selection,
`src/git/github_app.py` for bootstrap only, `src/git/github_cli.py` for the
runner, and `src/git/github.py` for the shared client. Move shared binding and
error types to a small dependency-free module if necessary to avoid cycles.
These names describe ownership, not a requirement for a framework or class
hierarchy.

The orchestrator owns one auth service and one runner and injects them into
GitManager, integration, onboarding and command services. Standalone service
construction must receive equivalent explicit dependencies; it must not create
a default unauthenticated manager that bypasses configured App credentials.

Repository operations include the existing `request_json`, bounded pagination,
exact-ref/PR reads, audit-PR reconciliation, comments and close operations, plus
ordinary PR creation/listing/polling/merge and CI reads. Move existing behavior
into the common client; do not maintain App-specific versions of these methods.
Audit-PR reconciliation adds its marker and identity checks around shared PR
primitives, rather than maintaining a second set of PR execution methods.
GitManager may retain thin async forwarding methods while its callers migrate.

`projects.github.GhClient` becomes a domain adapter for onboarding responses,
with no subprocess launcher of its own. Explicit account operations such as
repository creation and gist sharing use the same runner under the capability
rules in section 9.

## 5. Repository identity and authorization

Every repository operation receives an explicit validated target. Resolve it
from the project's canonical repository or the task's authorized repository
and workspace; do not select credentials from the process's working directory.
PR URLs and API paths must agree with that target. A caller cannot name project
A and use its authority to act on a URL for repository B.

Reuse the identity parser in
[src/projects/github.py](../../src/projects/github.py) for accepted GitHub URL
forms. App-backed network Git always uses a derived canonical HTTPS URL, even
when the configured repository was written in SSH form. Do not rewrite the
operator's remote configuration as a side effect.

Bindings retain numeric repository ID, canonical full name and `github.com` as
the host. Validate GitHub's response against the requested identity; a rename,
transfer or mismatch requires an explicit rebind instead of accepting a
different repository silently. Preserve the existing integration checks on
canonical repository records and durable authority.

The common client only accepts endpoints under its repository's
`repos/{owner}/{repo}` or `repositories/{id}` namespace. Reject foreign hosts,
absolute URLs, traversal and encoded namespace escapes. Fixed GraphQL queries
use explicit validated repository variables. Account operations have named
internal entry points; the worker command surface never accepts arbitrary
`gh` arguments, endpoints or token-mint requests.

Tokens describe GitHub access, not AQ authorization. The existing
CommandHandler grants, task/workspace ownership and publication policy remain
mandatory before resolving or using a privileged credential.

## 6. Credential lifecycle

### 6.1 App credentials

Extract and reuse the current private-key ownership/mode checks, JWT signing,
fixed permission set, expiry parsing and per-token refresh lock from
[src/git/github_app.py](../../src/git/github_app.py).

Bootstrap HTTP is limited to `GET /app` and
`POST /app/installations/{installation_id}/access_tokens` on the pinned GitHub
API host. Reject redirects outside this fixed contract. Request exactly one
repository and the existing permission set; reject widened, missing or
mismatched repository/permission responses. Do not mint an installation-wide
token for discovery or convenience.

Verify repository identity through the shared `gh api` runner with the newly
minted candidate credential before exposing a ready binding. This internal
verification passes that credential explicitly, avoiding recursive calls to
the provider that is still establishing it. Subsequent repository reads also
use `gh`; the bootstrap component has no general repository HTTP method.

Cache by effective App identity, installation and repository binding. Coalesce
concurrent mint/refresh requests and preserve refresh within five minutes of
expiry. Cache entries and secrets live only in daemon memory. Select a fresh
credential immediately before each network operation, not once per task or
worker session.

Installation tokens expire after approximately one hour and support GitHub
REST, GraphQL and HTTP Git access subject to permissions. See
[GitHub installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

### 6.2 Existing credentials

Without App configuration, the runner preserves the daemon user's credential
discovery environment, including home/config location and supported credential
store access. It lets `gh` resolve the credential; AQ need not extract or cache
the PAT. A login belonging to a different OS user is not the daemon's login.

Authenticated HTTPS Git uses the existing isolated helper arrangement with
`gh auth git-credential`. Existing operator-managed SSH Git remains usable in
existing-login mode. These are credential/transfer choices within the common
Git operation implementation, not duplicate PR or API implementations.

### 6.3 Failure and replay

Preserve structured categories for missing CLI, credentials, permission,
not-found-or-hidden, conflict/invalid input, rate limiting and transient errors.
Expose the selected mode, repository and remedy without exposing secrets.

A read that is rejected for authentication may invalidate the rejected App
token and retry once with a fresh token. If another request already refreshed
it, reuse the newer generation. Do not refresh repeatedly on permission errors
or treat a hidden repository as a missing branch.

The transport never blindly retries a write. `gh pr create` and `gh pr merge`
can perform several requests, so a nonzero exit does not prove no mutation
happened. After a timeout, cancellation or uncertain failure, the operation's
existing marker/PR/ref reconciliation determines the outcome before retrying.
In particular, distinguish a completed merge followed by failed branch cleanup
from a merge that never happened. Preserve rate-limit retry information where
available; retry scheduling remains with the existing caller.

## 7. Shared `gh` execution contract

`GhRunner` owns the following behavior for both credential modes:

- Launch a resolved trusted executable with argument arrays and bounded stdin;
  do not use shell interpolation, extensions or caller-supplied aliases.
- Use a daemon-controlled directory outside worker and operator checkouts.
  Supply explicit `--repo` or `--hostname` and validated PR/API identity.
- Build a new environment for every invocation. Never mutate `os.environ`,
  `GitManager._SUBPROCESS_ENV`, or another request's environment.
- Disable prompting, browser/editor flows, paging, update notifications and
  inherited API-debug output. Remove ambient `GH_REPO`, conflicting host
  selection and Git directory/config overrides that could redirect execution.
- In App mode, replace ambient GitHub token variables with only the selected
  repository token as `GH_TOKEN`; pin `GH_HOST=github.com` and isolate `gh`
  configuration from the operator's stored login. Failure to obtain a token
  must prevent process launch, not produce a credential-less fallback command.
- In existing-login mode, retain the credential discovery settings described
  in section 6.2 while still applying explicit targeting and no-prompt rules.
- Apply operation deadlines, output limits while reading streams, and process
  cleanup on cancellation or timeout. Kill and reap descendants that might
  otherwise outlive a privileged invocation. Retain longer bounded budgets
  for operations such as CI logs and large validation fetches.
- Send JSON bodies through stdin with `gh api --input -`; use stdin for PR
  bodies where supported. Never put tokens or authorization headers in argv.
- Keep machine responses intact for validation. Scrub any diagnostic before
  logging or returning it, including exact supplied secret values and known
  token/header patterns. Do not log the full environment, request body or raw
  authenticated subprocess output.

For `gh api`, retain HTTP status and the headers needed for pagination and
retry timing using `--include`. Enforce the caller's expected status set,
including empty `204` responses; do not discard it as the current CLI client
does. Distinguish JSON from bounded text responses such as Actions logs.
Support the repository's existing gh 2.45 compatibility baseline; do not assume
newer `--slurp` or PR JSON fields are present.

Pagination is explicit and bounded: validate each next link against host and
repository scope before issuing the next request, and enforce both page and
aggregate byte limits. Avoid unrestricted `--paginate` followed by a size check
after all pages have already been downloaded. The runner must also preserve
credential safety through HTTP redirects, including Actions log redirects;
test the supported CLI's behavior rather than logging authenticated traffic.

GitHub documents the relevant API flags in
[the `gh api` manual](https://cli.github.com/manual/gh_api).

For ordinary PR creation, provide repository, title, body, base and an already
published head explicitly. `--head` suppresses implicit fork/push behavior, as
documented in [the `gh pr create` manual](https://cli.github.com/manual/gh_pr_create).
For merging, preserve the validated expected head through
`--match-head-commit`, as documented by
[the `gh pr merge` manual](https://cli.github.com/manual/gh_pr_merge).
AQ's existing CI, target-base and reserved-path checks still run before merge.

Passing `GH_TOKEN` places a credential in a trusted subprocess environment.
This design does not claim secrecy from arbitrary processes running as the
same OS user. It must nevertheless preserve containment from worker hooks,
worker-controlled Git configuration and accidental session inheritance.

## 8. Git transfer and worker delivery

Use one implementation for each GitHub network operation, with credentials
provided by `GitHubAuth`. Extend the isolated transfer primitives already in
[src/git/manager.py](../../src/git/manager.py); do not inject App tokens into
ordinary `_arun` calls inside worker checkouts.

Preserve the protections exercised by
[tests/test_git_app_auth.py](../../tests/test_git_app_auth.py): derived HTTPS
destinations, isolated Git configuration, disabled worker hooks and helpers,
credential broker checks, immutable object import and process cleanup.
Tokens remain in the existing broker mechanism for Git, rather than remote
URLs, Git configuration, command arguments or worker environments.

For a delivery push, resolve the local source once, validate the exact tree or
diff for reserved paths, observe the remote target through authenticated
access, and push that same OID. Preserve fast-forward requirements for ordinary
pushes. Where a lease is required, use the explicit expected remote OID; an
all-zero expected OID means create only if absent. Emit push events only after
confirming success. Authentication changes must not turn a normal push into an
unconditional force push or remove a publication deadline.

Cover all prerequisite network operations: initial clone, default-branch
discovery, workspace/base fetch, exact PR-diff fetch, recovery/WIP publication,
parent CI snapshots, promotion and branch deletion. Extend isolated acquisition
to transfer objects into a credential-free local checkout. Preserve repository
locks, exact-ref checks, object validation and existing transfer budgets.
Additional private submodules or LFS services require their own explicit scope;
do not forward the repository token to arbitrary secondary destinations.

Worker publication uses existing daemon-backed `git_push`/`push_branch` and
`git_create_pr` command surfaces. Ensure these receive the task's authorized
repository, use the same validated delivery policy, and are available to the
roles that need them. Extend the existing push contract if an explicit expected
remote OID is needed for the documented squash/lease workflow. Do not grant
workers integration-owner or main-publication authority as a side effect.

Update the shipped completion protocol, delivery prompts and relevant AQ Git
skills to use those commands for network delivery. Keep local commit/history
work local. Existing customized vault profiles must receive actionable grant
drift guidance rather than being overwritten. An App-only acceptance run must
use the instructions and grants actually delivered by `aq prime`.

AQ does not install a global credential helper for worker shells, and arbitrary
`git push`/`gh` commands in those shells do not acquire App credentials. Existing
PAT session propagation is unchanged by this refactor. Never inject a minted
App token or its private key into a worker session.

## 9. Capabilities and integration trust

Sharing a transport does not give installation tokens every user capability.
Keep the following explicit boundary:

| Operation | Existing login | Configured App |
| --- | --- | --- |
| Supported operations on an authorized existing repository | Use existing credential permissions | Use repository-scoped installation permissions |
| Onboard an existing repository by explicit URL | Supported | Supported, with binding and access validation |
| User-wide repository search, owner picker and user identity | Retain existing behavior through shared runner | Report unavailable; offer explicit repository URL entry |
| Create a repository or create/read a gist through AQ's account features | Retain existing behavior and permission checks | Report `github_operation_unsupported` before invoking a user credential |

Account-wide discovery and provisioning do not justify minting a broader App
token or consulting a personal login behind the user's back. App installations
may support some broader operations with different permissions, but this design
retains AQ's current narrow permission set. In particular, organization
repository creation requires `administration: write`; AQ requests `read`. See
[GitHub repository creation permissions](https://docs.github.com/en/rest/repos/repos#create-an-organization-repository).

Health reporting distinguishes CLI availability, selected auth mode and access
to a specified repository. A successful App setup does not depend on
`gh auth status`, `/user`, or GraphQL `viewer` checks intended for user logins.
App-mode onboarding can accept and validate a repository URL even when no
personal account is logged in. Capability errors must be distinct from expired
credentials and include the applicable next action.

Integration already makes policy decisions from `auth_mode == "gh"` and
`client.config.app_id` in
[src/integration/preflight.py](../../src/integration/preflight.py) and
[src/integration/attestation.py](../../src/integration/attestation.py).
Replace these transport-shaped assumptions with explicit non-secret credential
identity: `app` or `existing_login`, plus App/installation IDs where applicable.
An App transported through `gh` must still perform App trust-manifest,
producer-identity and hosted-variable checks. Existing-login mode retains its
current policy-derived trust checks. Do not change the trust model or persist
tokens in attestation evidence.

## 10. Migration sequence and removal

Implementation may be staged, but completion requires the final removal below.
Do not ship a permanent feature flag selecting old versus new GitHub access.

1. **Extract credentials and the shared runner.** Preserve App bootstrap tests;
   add process-level environment, targeting, bounded-output and cancellation
   coverage. Establish explicit auth identity and repository binding.
2. **Consolidate API behavior.** Move inherited repository operations out of
   `GitHubAppClient` into the common client. Route integration reads/writes
   through `gh api` and preserve response and trust validation. Rename
   integration-only factory/resolver attributes and update their consumers.
3. **Consolidate ordinary and account operations.** Route PR/CI operations,
   onboarding and profile gists through the runner. Adapt existing command
   response envelopes and make unsupported App capabilities explicit.
4. **Complete Git and worker delivery.** Wire isolated clone/fetch/push/delete,
   merge-diff validation, recovery and cleanup. Update command grants and
   instructions, including explicit lease support where required.
5. **Remove superseded paths and document the change.** Delete the transport
   subclass split, repository HTTP methods and independent `gh` launchers.
   Remove unused synchronous network methods and deprecated task PR helpers
   after checking callers; any necessary compatibility entry point delegates
   to the single implementation and cannot run a separate subprocess path.

The migration inventory must include all consumers found by searches for
`GitHubAppClient`, `GitHubCLIClient`, `GhClient`, `installation_token`,
`integration_app_client_factory`, `auth_mode`, `api.github.com` and `gh` process
launches. Audit network Git callers separately; the API search will miss them.

The only remaining direct GitHub HTTP calls belong to the fixed App bootstrap
component. The `gh auth git-credential` subprocess used inside isolated Git is
an explicit credential-helper exception to the shared command launcher, not a
second API client. Test doubles and operator documentation may mention `gh`;
production business logic may not launch it independently.

Update current integration/onboarding/configuration documentation, module
catalogs, release notes, shipped worker instructions and the deployment guide.
The issue's `deploy/README.md` is on `feature/cloud-deploy`; update it when that
branch/file is integrated rather than adding a conflicting copy here. Document
the new `gh` dependency, restart boundary, PAT compatibility, App-only workflow,
and account-operation limitations. No database migration is expected. If
command/API models change, regenerate the committed clients under repository
rules.

## 11. Verification and acceptance

Use one operation test suite parameterized by credential source. Keep focused
tests for bootstrap and credential mechanics. Retain the existing integration,
CI/merge and Git-containment suites; replacing transport must not weaken them.

| Area | Required evidence |
| --- | --- |
| Existing PAT/login | Stored login, `GH_TOKEN`, and `GITHUB_TOKEN` remain usable with documented precedence; AQ does not alter stored auth |
| App selection | A configured App wins over both environment and stored PATs; mint failure prevents launch and never falls back |
| Token lifecycle | Expiry refresh, concurrent refresh coalescing, one safe read retry, and rejected identity/permissions |
| Concurrent repositories | Distinct tokens and targets under simultaneous requests, with unchanged parent/shared environments |
| Target authority | Foreign PR URL, project/repository mismatch, unsupported host, malicious next link and path escape are rejected |
| Process behavior | Missing executable, timeout, cancellation, child cleanup, bounded output, safe diagnostics, and supported gh version |
| API parity | Expected status codes, empty responses, bounded JSON pagination, text logs and redirect credential safety |
| Writes and reconciliation | Uncertain PR/comment/check publication is reconciled; partial merge/cleanup does not trigger blind replay |
| Git invariants | Exact OID push/fetch, fast-forward and lease checks, reserved paths, worker-hook/config isolation, deadlines and cleanup |
| Trust semantics | App-over-gh still enforces App trust; existing-login trust behavior remains intact |
| Surface coverage | Onboarding by URL, polling, CI baseline, merge-diff fetch, recovery pushes, remote cleanup, and worker command grants/instructions |
| Architecture | No alternate repository HTTP client or production `gh` launcher remains outside documented exceptions |

Existing focused suites include
[tests/test_github_app.py](../../tests/test_github_app.py),
[tests/test_github_cli.py](../../tests/test_github_cli.py),
[tests/test_git_app_auth.py](../../tests/test_git_app_auth.py),
[tests/test_git_manager_async.py](../../tests/test_git_manager_async.py),
[tests/test_project_github.py](../../tests/test_project_github.py),
[tests/test_project_onboarding_service.py](../../tests/test_project_onboarding_service.py),
[tests/test_pr_merge_command.py](../../tests/test_pr_merge_command.py),
[tests/test_pr_merge_ci_gate.py](../../tests/test_pr_merge_ci_gate.py),
[tests/test_ci_baseline_status.py](../../tests/test_ci_baseline_status.py),
[tests/test_worker_git_scope.py](../../tests/test_worker_git_scope.py), and
[tests/test_integration_attestation.py](../../tests/test_integration_attestation.py).
Run focused selections through `aq test` under the repository's resource rules;
expand only to affected areas. These are implementation acceptance checks,
not claims that tests were run while writing this specification.

The final acceptance gate uses an explicitly provisioned disposable private
repository with the App installed. Run AQ with no personal token, stored `gh`
login or usable SSH credential, and follow the actual worker instructions:

1. Onboard by URL and acquire a fresh workspace.
2. Commit and publish a task branch through AQ, then open a PR.
3. Poll its state, inspect CI and fetch the exact merge-validation commits.
4. Merge the validated revision under normal policy and perform branch cleanup.
5. Exercise integration publication and recovery/WIP delivery in their
   respective configured workflows.
6. Exercise token refresh and confirm the resulting remote revisions and
   authenticated actor. Repeat the supported workflow with existing-login
   authentication to demonstrate parity.

Automated tests must additionally prove that an available ambient PAT is not
used when the configured App is denied. Record live smoke-test evidence only
when run against a disposable fixture; label mock-only evidence as such. No
live workflow was run while writing this specification.

The issue is complete only when the supported App-only workflow succeeds and
the duplicated execution paths have been removed. Merely authenticating
`gh pr create`, or retaining an untested alternate backend behind the common
interface, does not meet this specification.
