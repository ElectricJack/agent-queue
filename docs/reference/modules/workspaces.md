# Projects and workspaces module catalog

This catalog maps the project-registration, Git, workspace, and worktree
implementation to [projects and workspaces](../../concepts/projects-and-workspaces.md).
It is a reference: command syntax belongs in the CLI reference and delivery
policy belongs in [integration](../../concepts/integration.md).

## Project registration and paths

| Module | Purpose | Component | Notes |
| --- | --- | --- | --- |
| [`src/projects/__init__.py`](../../../src/projects/__init__.py) | Marks the project-onboarding package and describes its dependency boundaries. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Package documentation points callers to paths, GitHub validation, and saga modules. |
| [`src/projects/events.py`](../../../src/projects/events.py) | Builds and emits the `project.created` event both project-creation paths announce, including the `workspace_in_vault` path fact. | [Project onboarding](../../guides/project-onboarding.md) | Emit failures only log: the project is already registered. `tests/test_project_onboarding_service.py`. |
| [`src/projects/github.py`](../../../src/projects/github.py) | Validates GitHub repository identities, derives safe clone URLs, and adapts shared runner results to onboarding responses. | [Project onboarding](../../guides/project-onboarding.md) | App mode refuses account operations; `tests/test_project_github.py`, `tests/test_project_onboarding_service.py`. |
| [`src/projects/onboarding.py`](../../../src/projects/onboarding.py) | Coordinates idempotent link/init/clone onboarding, registration, and bounded compensation. | [Project onboarding](../../guides/project-onboarding.md) | `tests/test_project_onboarding_service.py`, `tests/test_project_onboarding_contract.py`. |
| [`src/projects/paths.py`](../../../src/projects/paths.py) | Resolves and validates root-relative paths and provides bounded safe directory listings. | [Project onboarding](../../guides/project-onboarding.md) | Rejects managed worktree trees and symlink escapes. `tests/test_project_paths.py`. |
| [`src/projects/storage.py`](../../../src/projects/storage.py) | Creates the task and vault directory scaffolding for a newly registered project. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Called by onboarding; covered with onboarding service tests. |
| [`src/projects/roots.py`](../../../src/projects/roots.py) | Classifies configured-root readiness for doctor and installer surfaces. | [Project onboarding](../../guides/project-onboarding.md) | Read-only roots still permit linking; `tests/test_project_roots_config.py`. |
| [`src/projects/relocation.py`](../../../src/projects/relocation.py) | Inventories and verifies full storage copies, fences writers and atomically publishes path bindings. | [Storage relocation](../../guides/matter-engine-storage-relocation.md) | Local operator preview/apply; `tests/test_project_relocation.py`. |
| [`src/projects/storage_binding.py`](../../../src/projects/storage_binding.py) | Resolves repository, retained-store and cache paths through a project's committed storage generation. | [Storage relocation](../../guides/matter-engine-storage-relocation.md) | Default paths remain in effect for unrelated projects. |

## Git boundaries

| Module | Purpose | Component | Notes |
| --- | --- | --- | --- |
| [`src/git/__init__.py`](../../../src/git/__init__.py) | Marks AQ's Git integration package. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Public Git helpers live in the sibling modules. |
| [`src/git/askpass_broker.py`](../../../src/git/askpass_broker.py) | Pins credential-helper topology and brokers a credential to one authenticated Git prompt. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Security-sensitive helper; `tests/test_git_app_auth.py`, `tests/test_git_manager_async.py`. |
| [`src/git/askpass_fd.py`](../../../src/git/askpass_fd.py) | Implements the file-descriptor side of AQ's narrow Git askpass protocol. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Invoked as an askpass helper, not a user-facing CLI. |
| [`src/git/ci_gate.py`](../../../src/git/ci_gate.py) | Classifies pull-request checks and base freshness into a CI gate verdict. | [Integration](../../concepts/integration.md) | Strict/PR-oriented compatibility behavior; `tests/test_pr_merge_ci_gate.py`. |
| [`src/git/github_contracts.py`](../../../src/git/github_contracts.py) | Defines immutable repository binding, non-secret credential identity, mode and safe error contracts. | [Integration](../../concepts/integration.md#github-access-during-delivery) | Identity is `app` or `existing_login`, independent of transport; `tests/test_github_contracts.py`. |
| [`src/git/github_app.py`](../../../src/git/github_app.py) | Bootstraps App identity and repository-scoped installation tokens through fixed HTTP endpoints. | [Integration](../../concepts/integration.md#github-access-during-delivery) | `AppTokenProvider` owns bootstrap HTTP; old `GitHubAppClient` is removed. `tests/test_github_app.py`. |
| [`src/git/github_auth.py`](../../../src/git/github_auth.py) | Selects one startup credential mode and refreshes App tokens in memory for each bound repository. | [Configuration](../../reference/configuration.md#github-credentials) | Configured App never falls back to ambient credentials; `tests/test_github_auth.py`. |
| [`src/git/github_cli.py`](../../../src/git/github_cli.py) | Owns the bounded, target-scoped `gh` runner and its per-call credential environment. | [Project onboarding](../../guides/project-onboarding.md) | `GhRunner` is the shared launcher; old `GitHubCLIClient` is removed. `tests/test_github_runner.py`. |
| [`src/git/github.py`](../../../src/git/github.py) | Implements repository-bound GitHub operations using the shared runner and exposes the startup-owned access service. | [Integration](../../concepts/integration.md#github-access-during-delivery) | Account gists require existing-login mode; `tests/test_github_client.py`. |
| [`src/git/manager.py`](../../../src/git/manager.py) | Runs validated Git operations for checkout, branches, worktrees, commits, push, merge, and recovery. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_git_manager.py`, `tests/test_git_manager_async.py`, `tests/test_worktree_prepare.py`. |
| [`src/git/identity.py`](../../../src/git/identity.py) | Resolves project/default/fallback commit identity and worker publication policy. | [Git identity](../../specs/git-identity.md) | Environment injection and launch digests; `tests/test_git_identity.py`. |
| [`src/git/process.py`](../../../src/git/process.py) | Owns subprocess deadlines and process-group settlement on exit, timeout and cancellation. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_git_manager_async.py`. |
| [`src/git/locks.py`](../../../src/git/locks.py) | Conservatively recovers abandoned Git lock files with process evidence. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Distinct from repository transaction locks in `GitManager`. |

## Workspace kinds, acquisition, and slot lifecycle

| Module | Purpose | Component | Notes |
| --- | --- | --- | --- |
| [`src/profiles/workspace_kind_parser.py`](../../../src/profiles/workspace_kind_parser.py) | Parses workspace-kind markdown into validated capability and locking definitions. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_kind_parser.py`. |
| [`src/profiles/workspace_kind_registry.py`](../../../src/profiles/workspace_kind_registry.py) | Reconciles system and project kind markdown to database rows at daemon startup. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | No live watcher; restart after edits. `tests/test_workspace_kind_registry.py`. |
| [`src/workspace_names.py`](../../../src/workspace_names.py) | Generates collision-checked human-readable workspace IDs. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_names.py`. |
| [`src/workspace_listing.py`](../../../src/workspace_listing.py) | Collects bounded, read-only owner and checkout diagnostics with explicit unknown/partial states. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_listing.py`. |
| [`src/workspace_registration.py`](../../../src/workspace_registration.py) | Proves an absent checkout before registration cleanup or repair. | [Project onboarding](../../guides/project-onboarding.md#repair-a-moved-repository-checkout) | Directory removal and registration repair are separate actions. |
| [`src/workspace_disable.py`](../../../src/workspace_disable.py) | Defines disable reasons, cooldown and per-kind slot capacity. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Operator disables are never automatically undone. |
| [`src/workspace_spec_watcher.py`](../../../src/workspace_spec_watcher.py) | Detects workspace spec/doc changes, writes vault reference stubs, and emits change events. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_spec_watcher.py`; separate from slot scheduling. |
| [`src/orchestrator/base_workspace.py`](../../../src/orchestrator/base_workspace.py) | Identifies base checkouts and refuses ordinary agents from running in one. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_base_workspace_guard.py`. |
| [`src/orchestrator/workspace.py`](../../../src/orchestrator/workspace.py) | Prepares task workspaces, grows slots, selects resume affinity, and releases/cleans attachments. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_worktree_acquisition.py`, `tests/test_workspace_branch_wait.py`, `tests/test_workspace_slot_affinity.py`. |
| [`src/orchestrator/workspace_attachments.py`](../../../src/orchestrator/workspace_attachments.py) | Resolves multi-kind requirements and acquires or rolls back attachments atomically. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_acquisition_read_only.py`, `tests/test_workspace_attachments.py`. |
| [`src/orchestrator/workspace_claim_recovery.py`](../../../src/orchestrator/workspace_claim_recovery.py) | Removes a stopped worker's exact stale claim file without touching a successor. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Claim/session fence checks prevent stale cleanup. |
| [`src/orchestrator/retained_workspace.py`](../../../src/orchestrator/retained_workspace.py) | Completes explicitly requested retained stops with claim/sentinel proofs and checkpoint capture. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Preserves ownership until the stopped-worker proof succeeds. |
| [`src/orchestrator/slot_restore.py`](../../../src/orchestrator/slot_restore.py) | Reconciles eligible disabled slots, recreating missing directories and preserving dirty evidence. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_worktree_slot_starvation.py`. |
| [`src/orchestrator/workspace_binding_audit.py`](../../../src/orchestrator/workspace_binding_audit.py) | Audits stale task/workspace bindings before retired-slot cleanup. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Periodic reaper work; not permission to reset a live slot. |
| [`src/orchestrator/worktree_manager.py`](../../../src/orchestrator/worktree_manager.py) | Creates, resets, salvages, restores, adopts, reaps, and prunes managed Git worktree slots. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_worktree_manager.py`, `tests/test_worktree_reaper.py`, `tests/test_worktree_doctor.py`. |
| [`src/orchestrator/git_ops.py`](../../../src/orchestrator/git_ops.py) | Executes orchestrator-side completion, verification, delivery, and Git recovery phases. | [Integration](../../concepts/integration.md) | Delivery behavior is documented in the integration shard; `tests/test_development_integration.py`, `tests/test_merge_and_push.py`. |

## Reusable integration checkouts

| Module | Purpose | Component | Notes |
| --- | --- | --- | --- |
| [`src/warm_workspaces.py`](../../../src/warm_workspaces.py) | Allocates reusable linked checkouts with external owner manifests and caller-supplied release proofs. | [Reusable checkouts](../../concepts/projects-and-workspaces.md#reusing-integration-checkouts-and-compiler-caches) | Job, close-merge and fixer callers own their durable writer/pin fences; `tests/test_integration_workspaces.py`. |
| [`src/build_cache.py`](../../../src/build_cache.py) | Resolves optional compiler-cache settings, environment and bounded shared-cache telemetry. | [Configuration](../../reference/configuration.md#warm-integration-checkouts-and-compiler-caches) | Supports `ccache` and `sccache`; relocation bindings override cache paths. |

## Running focused tests

```bash
aq test tests/test_project_onboarding_service.py tests/test_project_paths.py \
  tests/test_project_github.py tests/test_workspace_names.py \
  tests/test_workspace_kind_parser.py tests/test_workspace_kind_registry.py
aq test tests/test_worktree_manager.py tests/test_worktree_acquisition.py \
  tests/test_worktree_doctor.py tests/test_workspace_attachments.py \
  tests/test_workspace_spec_watcher.py tests/test_base_workspace_guard.py
```

## Modules this shard deliberately does not own

| Path | Owning shard | Where it is documented |
| --- | --- | --- |
| `src/commands/project_commands.py`, `agent_commands.py`, `worktree_commands.py`, `workspace_checkout_repair.py`, `workspace_cutover.py`, `project_relocation_commands.py` | `cli` | CLI command reference and [onboarding/repair guide](../../guides/project-onboarding.md), including operator permissions and exact flags. |
| `src/database/queries/workspace_queries.py`, `workspace_kinds_queries.py`, `task_requirements_queries.py` | `database` | Database reference and query catalog. |
| `src/integration/` | `integration` | [Integration](../../concepts/integration.md) and its operations guide. |
| `src/sessions/` | `sessions` | [Sessions](../../concepts/sessions.md), which explains the prepared work directory's agent lifecycle. |
