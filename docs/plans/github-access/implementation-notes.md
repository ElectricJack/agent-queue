# GitHub access implementation notes

Status: migration inventory and package-1 handoff for
[Unified GitHub access through `gh`](../../specs/github-access.md).

Inventory baseline: repository commit `f60cc85f35c697057a2e678384fecc5575fb9c20`,
2026-09-22. The repository spec is byte-identical to the filed execution
snapshot (SHA-256
`fdf89a699290e5233dd029c9ae1ffe47e13e4d40169e8ca7b9abbf011c334431`).

## Baseline and non-goals

The daemon host used for this inventory reports `gh version 2.45.0`; current
source also names 2.45 as the supported compatibility floor in
`GitManager.aget_pr_identity`. In particular, migration code must not depend
on `gh api --slurp` or the `baseRefOid` PR JSON field added in later releases.

This package does not change production credential selection, add a backend
switch, persist a credential, alter a database table, or inject credentials
into worker sessions. `integration.github_app` remains the selection rule:
present means App, absent means the daemon user's existing login. The final
transport consolidation and removal happen in later packages.

## Contracts introduced here

`src/git/github_contracts.py` is dependency-free and contains only immutable
values and a safe exception shape:

| Contract | Meaning |
|---|---|
| `GitHubRepositoryBinding` | Numeric repository ID, canonical `owner/repository`, and the fixed `github.com` host. |
| `GitHubCredentialMode` | Credential source, exactly `app` or `existing_login`; it says nothing about HTTP, `gh`, or Git transport. |
| `GitHubCredentialIdentity` | Non-secret mode plus App and installation IDs where applicable. It contains no token, key path, login secret, environment or subprocess detail. |
| `GitHubAccessError` | Safe category, operator-safe message and optional retry time; no raw body, stderr or credential field. |
| `credential_identity_from_client` | Temporary compatibility adapter for staged clients/test doubles. Explicit `credential_identity` wins; legacy `auth_mode`/`config.app_id` inspection is confined here and is removed with the old clients in migration step 5. |

`GitHubAppClient` and `GitHubCLIClient` now expose the shared credential
identity. `GitHubAppError` and `GitHubRepositoryBinding` remain re-exported
from `github_app.py` so intermediate commits and downstream checkouts keep
working. Integration preflight and attestation consume the credential identity
instead of making policy directly from `auth_mode == "gh"` or
`client.config.app_id`.

## Final module and dependency layout

Imports must point down this list, never back up it:

1. `src/git/github_contracts.py` — bindings, credential identity and safe
   errors; standard library only.
2. `src/git/github_app.py` — final `AppTokenProvider`, fixed bootstrap HTTP and
   App key/JWT/token mechanics; imports contracts and configuration only.
3. `src/git/github_auth.py` — `GitHubAuth`, startup credential selection,
   repository binding, token caching and refresh; imports contracts and the
   App bootstrap provider.
4. `src/git/github_cli.py` — `GhRunner`, process containment and response
   framing; imports contracts and the auth interface, not repository
   operations.
5. `src/git/github.py` — repository-bound `GitHubClient` operations using the
   runner; imports contracts and runner interfaces.
6. `src/git/manager.py` — local Git plus authenticated Git object/ref transfer;
   imports contracts and auth, and may delegate repository operations to the
   shared client during compatibility staging.
7. Domain adapters (`src/projects/github.py`, onboarding, profile gists,
   integration and commands) import the shared client/manager and receive
   them from orchestrator composition.

The orchestrator owns one `GitHubAuth`, one `GhRunner` and the clients they
create. Standalone services must receive equivalent explicit dependencies.
No domain adapter may construct a credential-unaware fallback after App mode
has been selected.

## GitHub API and `gh` launcher inventory

The migration owner column names the component responsible for removing or
absorbing the current path. A path remains listed even when it only adapts or
constructs a client, because losing those sites would silently restore ambient
credential fallback.

| Current production path | Operations and current behavior | Migration owner / handoff |
|---|---|---|
| `src/git/github_app.py`: `AiohttpTransport`, `GitHubAppClient` | Direct HTTP App identity/token bootstrap **and** repository reads, pagination, refs, PRs, comments, audit PRs and checks. | `AppTokenProvider` keeps only `GET /app` and installation-token POST (step 1). `GitHubClient` takes every repository operation through `GhRunner` (step 2). |
| `src/git/github_cli.py`: `GitHubCLIClient._api_json` | Independent `gh api` subprocess launcher; repository binding, JSON bodies, pagination and error classification; subclasses the App HTTP client. | `GhRunner` owns launch/process/error behavior (step 1); `GitHubClient` owns operations (step 2); delete subclass split in step 5. |
| `src/git/manager.py`: `_run`, `_arun_subprocess` and PR/API helpers | Direct `gh pr create/view/list/merge`, `gh api` PR/commit/check/log/compare reads, open-PR reconciliation, `gh auth status`, and `gh repo create`, in synchronous and async variants. | Repository operations move to `GitHubClient` (steps 2–3); account/auth operations become named `GhRunner` calls with capability checks (step 3). Synchronous duplicates are removed in step 5 after callers are checked. |
| `src/git/manager.py`: isolated Git credential setup | `credential.helper=!gh auth git-credential` for existing-login private Git transfers. | `GitManager` owns this documented helper exception (step 4); it is not a second API launcher. App mode continues to use its one-shot broker. |
| `src/projects/github.py`: `GhClient._run` | Independent launcher for `gh auth status`, `/user`, owner discovery, repository search and repository creation. | Keep URL parsing and onboarding response adaptation here; move execution to `GhRunner` and repository validation to `GitHubClient` (step 3). App mode rejects account-wide operations before launch. |
| `src/commands/project_onboarding_commands.py`: `_github_client` | Constructs a fresh `GhClient` for auth, owner and search commands. | Inject the orchestrator-owned onboarding adapter (step 3); remove fallback construction. |
| `src/projects/onboarding.py`: constructor and `_create_github_remote` | Defaults to independent `GitManager()`/`GhClient()`; clone, account repository creation and initial push. | Inject `GitHubAuth`, shared onboarding client and configured `GitManager` (steps 3–4). Explicit existing-repository URL remains supported in App mode; account creation is unsupported there. |
| `src/commands/profile_commands.py`: profile export/import | Launches `gh gist create` and `gh gist view` directly and returns raw-ish command diagnostics. | Named account-operation methods on `GhRunner`, existing-login only, with `github_operation_unsupported` in App mode (step 3). |
| `src/orchestrator/core.py`: `integration_app_client` and `resolve_integration_repository` | Selects App HTTP versus CLI subclass, owns client cache, binding, and the legacy factory/resolver names. | Construct `GitHubAuth`/runner/client once and inject them (steps 1–2); rename factories after consumers migrate. Selection semantics do not change. |
| `src/commands/integration_commands.py`, `src/integration/preflight.py` | Read the legacy factory/resolver. Preflight previously inferred trust mode from `auth_mode`. | Shared provider/client wiring (step 2); preflight now consumes `GitHubCredentialIdentity`, preserving App versus existing-login trust policy. |
| `src/integration/attestation.py` | Repository API operations, App token fetch and trust selection; previously inspected `auth_mode` and `config.app_id`. | Shared client/auth identity (step 2) and authenticated Git manager (step 4). It now uses the shared identity contract without changing trust rules. |
| `src/integration/{candidates,main_promotion,cleanup,branch_discard,parent_ci}.py` | Call `installation_token()` and App-specific authenticated Git helpers; cleanup and promotion also call repository API methods. | `GitHubAuth` supplies credentials and `GitManager` consumes the same authority (step 4); `GitHubClient` supplies API operations (step 2). |
| `src/integration/ci.py` | Builds `api.github.com/.../check-runs/...` evidence URLs but does not launch HTTP or `gh` itself. | `GitHubClient` validation/evidence adapter (step 2); keep URL identity validation. |

Search coverage for this table included every production occurrence of
`GitHubAppClient`, `GitHubCLIClient`, `GhClient`, `installation_token`,
`integration_app_client_factory`, `auth_mode`, `api.github.com`, all literal
`gh` argv, and every `create_subprocess_exec` in the GitHub/Git/command
modules. `src/doctor/builtin.py` only probes whether `gh` exists, and
`src/commands/git_commands.py` delegates to `GitManager`; neither is an
additional GitHub operation launcher.

## Network Git inventory

All network Git execution ultimately uses `GitManager`, but many callers still
invoke generic `_arun`/`arun_git_result` with an `origin` chosen from a checkout.
That is insufficient for configured App credentials. The step-4 owner must
resolve an authorized repository explicitly before any GitHub network action.
Local-only Git calls in the same modules stay local.

| Consumer group | Production callers and network operations | Migration owner / handoff |
|---|---|---|
| Core primitives | `src/git/manager.py`: sync/async clone; pull/fetch; `ls-remote`; ordinary, exact-OID and lease pushes; PR-diff fetch; App/existing-login exact fetch and push; remote branch deletion. | `GitManager` + `GitHubAuth` (step 4). One operation per action, canonical HTTPS in App mode, existing SSH/PAT behavior retained otherwise. |
| Initial acquisition and onboarding | `src/commands/agent_commands.py`; `src/projects/onboarding.py`; `src/orchestrator/workspace.py`; `src/orchestrator/workspace_attachments.py`; `src/orchestrator/worktree_manager.py`; `src/doctor/pool_checks.py`. These clone/fetch/default-discover, refresh bases and prune/delete remote branches. | Workspace/acquisition integration with configured `GitManager` (step 4). App mode imports into credential-free workspaces and never rewrites operator remotes. |
| Task and worker delivery | `src/plugins/internal/git.py`; `src/orchestrator/git_ops.py`; `src/orchestrator/stranded_work.py`; `src/commands/project_commands.py`; `src/orchestrator/pr_polling.py`. These observe remotes, publish task/recovery branches, refresh PR bases and delete task branches. | Daemon-backed Git command owner plus `GitManager` (step 4). Preserve task/workspace authorization, immutable OIDs, fast-forward/lease checks and event timing. |
| Checkpoint and workspace recovery | `src/orchestrator/task_checkpoint.py`; `src/integration/completion_recovery.py`; `src/integration/owner_recovery.py`; `src/orchestrator/provider_failover.py` (through the Git command service). These fetch saved refs, inspect remote state and publish WIP/recovery branches. | Recovery service + configured `GitManager` (step 4); distinguish local-path fetches from GitHub remotes and never give tokens to worker shells. |
| Hierarchy and candidate publication | `src/integration/hierarchy.py`; `src/integration/promotion.py`; `src/integration/candidates.py`; `src/integration/main_promotion.py`; `src/integration/attestation.py`; `src/integration/parent_ci.py`; `src/integration/review_evidence.py`. These clone/fetch retained stores, observe refs, publish exact/leased candidates and main, and import exact reviewed objects. | Integration services + `GitManager`/`GitHubAuth` (step 4). Preserve frozen authority, exact OIDs, attestation/CI trust and ambiguity reconciliation. |
| Cleanup and branch removal | `src/integration/cleanup.py`; `src/integration/branch_discard.py`; `src/integration/delivery_branches.py`; `src/orchestrator/worktree_manager.py`; `GitManager.adelete_branch`. These back up, lease-delete and confirm remote refs. | Cleanup services + `GitManager` (step 4). Existing backup, live-owner, default-branch and CAS protections remain mandatory. |
| Retained development/integration stores | `src/integration/development.py` and `src/integration/promotion.py` pass network clone/fetch/push commands through generic result runners while building and publishing retained repositories. | Development/promotion service + configured `GitManager` (step 4); repository authority must be explicit rather than inferred from `cwd`. |

The inventory also checked remote reads through `als_remote_ref`, direct
`fetch`/`clone`/`push`/`ls-remote` argument arrays, and the authenticated App
helpers. Secondary destinations such as submodules and LFS are not silently in
scope and must not inherit a repository token.

## Constructor fallback register

These constructors are migration hazards because a standalone service can
currently bypass the orchestrator's selected App credentials:

| Site | Current fallback | Required handoff |
|---|---|---|
| `Orchestrator.__init__` | `self.git = GitManager()` | Configure the manager with the orchestrator-owned auth service (step 4). |
| `ProjectOnboardingCommandsMixin._github_client` | `return GhClient()` | Inject the shared onboarding adapter (step 3). |
| `ProjectOnboardingService.__init__` | `git_manager or GitManager()` and `gh_client or GhClient()` | Require explicit effective dependencies in production construction (steps 3–4). |
| `CandidateService`, `DevelopmentIntegration`, `RootPromotionService`, `PromotionService` | `git_manager/git or GitManager()` | Require the configured manager wherever network operations are reachable (step 4). Local-only test construction may inject a deliberately local fake. |
| `owner_recovery.py` and `task_checkpoint.py` | Create isolated `GitManager()` instances for local index/tree work. | Keep only if statically local-only; pass the configured manager for any fetch/push path (step 4). |
| `doctor/pool_checks.py` | Several ad-hoc `GitManager()` instances, including a remote fetch check. | Inject configured auth for network checks or make the check explicitly local-only (step 4). |

## Package handoffs and removal gates

1. **Auth/runner package:** implement `AppTokenProvider`, `GitHubAuth` and
   `GhRunner` against the contracts above. Preserve the current App bootstrap
   validation, cache/refresh lock and no-fallback rule. Add process containment,
   bounded stream and safe-diagnostic tests on 2.45.
2. **Repository-client package:** move all repository operations from
   `GitHubAppClient`/`GitHubCLIClient` into one `GitHubClient`; update the
   integration factory/resolver consumers without changing trust decisions.
3. **Ordinary/account package:** adapt `GitManager` PR/CI calls, onboarding and
   profile gists to the runner/client. Enforce account-operation capability
   errors before process launch in App mode.
4. **Git/delivery package:** cover every network-Git group above, update worker
   command grants/instructions, and prove App-only clone through cleanup while
   retaining immutable delivery and CI/integration authority.
5. **Removal package:** delete direct repository HTTP, transport subclassing,
   all independent production `gh` launchers, legacy `auth_mode` compatibility,
   credential-unaware constructor fallbacks and unused synchronous network
   methods. `gh auth git-credential` remains only as the documented isolated
   existing-login Git helper.

Before the final removal lands, repeat the literal/API and network-Git searches
listed above. A compatibility entry point may remain only when it delegates to
the single implementation and cannot launch a second process path.
