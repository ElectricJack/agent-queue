# Projects and workspaces module catalog

This catalog maps the project-registration, Git, workspace, and worktree
implementation to [projects and workspaces](../../concepts/projects-and-workspaces.md).
It is a reference: command syntax belongs in the CLI reference and delivery
policy belongs in [integration](../../concepts/integration.md).

## Project registration and paths

| Module | Purpose | Component | Notes |
| --- | --- | --- | --- |
| [`src/projects/__init__.py`](../../../src/projects/__init__.py) | Marks the project-onboarding package and describes its dependency boundaries. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Package documentation points callers to paths, GitHub validation, and saga modules. |
| [`src/projects/github.py`](../../../src/projects/github.py) | Validates GitHub repository identities, derives safe clone URLs, runs host `gh` commands, and scrubs secrets from failures. | [Project onboarding](../../guides/project-onboarding.md) | `tests/test_project_github.py`, `tests/test_project_onboarding_service.py`. |
| [`src/projects/onboarding.py`](../../../src/projects/onboarding.py) | Coordinates idempotent link/init/clone onboarding, registration, and bounded compensation. | [Project onboarding](../../guides/project-onboarding.md) | `tests/test_project_onboarding_service.py`, `tests/test_project_onboarding_contract.py`. |
| [`src/projects/paths.py`](../../../src/projects/paths.py) | Resolves and validates root-relative paths and provides bounded safe directory listings. | [Project onboarding](../../guides/project-onboarding.md) | Rejects managed worktree trees and symlink escapes. `tests/test_project_paths.py`. |
| [`src/projects/storage.py`](../../../src/projects/storage.py) | Creates the task and vault directory scaffolding for a newly registered project. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Called by onboarding; covered with onboarding service tests. |

## Git boundaries

| Module | Purpose | Component | Notes |
| --- | --- | --- | --- |
| [`src/git/__init__.py`](../../../src/git/__init__.py) | Marks AQ's Git integration package. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Public Git helpers live in the sibling modules. |
| [`src/git/askpass_broker.py`](../../../src/git/askpass_broker.py) | Pins credential-helper topology and brokers a credential to one authenticated Git prompt. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Security-sensitive helper; `tests/test_git_app_auth.py`, `tests/test_git_manager_async.py`. |
| [`src/git/askpass_fd.py`](../../../src/git/askpass_fd.py) | Implements the file-descriptor side of AQ's narrow Git askpass protocol. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Invoked as an askpass helper, not a user-facing CLI. |
| [`src/git/ci_gate.py`](../../../src/git/ci_gate.py) | Classifies pull-request checks and base freshness into a CI gate verdict. | [Integration](../../concepts/integration.md) | Strict/PR-oriented compatibility behavior; `tests/test_pr_merge_ci_gate.py`. |
| [`src/git/github_app.py`](../../../src/git/github_app.py) | Talks to the GitHub App API, including installation-token and audit-PR support. | [Integration](../../concepts/integration.md) | Optional GitHub App integration; `tests/test_github_app.py`. |
| [`src/git/github_cli.py`](../../../src/git/github_cli.py) | Adapts authenticated `gh api` calls to the GitHub client interface. | [Project onboarding](../../guides/project-onboarding.md) | Uses daemon-host authentication; `tests/test_github_cli.py`. |
| [`src/git/manager.py`](../../../src/git/manager.py) | Runs validated Git operations for checkout, branches, worktrees, commits, push, merge, and recovery. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_git_manager.py`, `tests/test_git_manager_async.py`, `tests/test_worktree_prepare.py`. |

## Workspace kinds, acquisition, and slot lifecycle

| Module | Purpose | Component | Notes |
| --- | --- | --- | --- |
| [`src/profiles/workspace_kind_parser.py`](../../../src/profiles/workspace_kind_parser.py) | Parses workspace-kind markdown into validated capability and locking definitions. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_kind_parser.py`. |
| [`src/profiles/workspace_kind_registry.py`](../../../src/profiles/workspace_kind_registry.py) | Maintains the watched registry of system and project workspace kinds. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_kind_registry.py`. |
| [`src/workspace_names.py`](../../../src/workspace_names.py) | Generates collision-checked human-readable workspace IDs. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_names.py`. |
| [`src/workspace_spec_watcher.py`](../../../src/workspace_spec_watcher.py) | Detects workspace spec/doc changes, writes vault reference stubs, and emits change events. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_spec_watcher.py`; separate from slot scheduling. |
| [`src/orchestrator/base_workspace.py`](../../../src/orchestrator/base_workspace.py) | Identifies base checkouts and refuses ordinary agents from running in one. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_base_workspace_guard.py`. |
| [`src/orchestrator/workspace.py`](../../../src/orchestrator/workspace.py) | Prepares task workspaces, grows slots, selects resume affinity, and releases/cleans attachments. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_worktree_acquisition.py`, `tests/test_workspace_branch_wait.py`, `tests/test_workspace_slot_affinity.py`. |
| [`src/orchestrator/workspace_attachments.py`](../../../src/orchestrator/workspace_attachments.py) | Resolves multi-kind requirements and acquires or rolls back attachments atomically. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_workspace_acquisition_read_only.py`, `tests/test_workspace_attachments.py`. |
| [`src/orchestrator/workspace_claim_recovery.py`](../../../src/orchestrator/workspace_claim_recovery.py) | Removes a stopped worker's exact stale claim file without touching a successor. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | Claim/session fence checks prevent stale cleanup. |
| [`src/orchestrator/worktree_manager.py`](../../../src/orchestrator/worktree_manager.py) | Creates, resets, salvages, restores, adopts, reaps, and prunes managed Git worktree slots. | [Projects and workspaces](../../concepts/projects-and-workspaces.md) | `tests/test_worktree_manager.py`, `tests/test_worktree_reaper.py`, `tests/test_worktree_doctor.py`. |
| [`src/orchestrator/git_ops.py`](../../../src/orchestrator/git_ops.py) | Executes orchestrator-side completion, verification, delivery, and Git recovery phases. | [Integration](../../concepts/integration.md) | Delivery behavior is documented in the integration shard; `tests/test_development_integration.py`, `tests/test_merge_and_push.py`. |

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
| `src/commands/project_commands.py`, `src/commands/workspace_commands.py` | `cli` | CLI command reference, including operator permissions and exact flags. |
| `src/database/queries/workspace_queries.py`, `workspace_kinds_queries.py`, `task_requirements_queries.py` | `database` | Database reference and query catalog. |
| `src/integration/` | `integration` | [Integration](../../concepts/integration.md) and its operations guide. |
| `src/sessions/` | `sessions` | [Sessions](../../concepts/sessions.md), which explains the prepared work directory's agent lifecycle. |
