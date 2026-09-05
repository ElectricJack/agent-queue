# Project Onboarding Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement surface-independent local project onboarding for linked and newly initialized Git repositories with durable idempotency, recovery, and bounded rollback.

**Architecture:** `ProjectOnboardingService` owns the saga and coordinates validated root-relative paths, async Git operations, request persistence, filesystem ownership, and atomic project/workspace registration. Command-handler and Click CLI methods only validate/forward requests and format results. Existing project creation and onboarding registration share a vault-initialization helper so both paths retain identical filesystem conventions.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy Core async adapters, Pydantic command contracts, Click CLI, pytest/pytest-asyncio, GitManager async subprocess APIs.

**Spec:** `docs/superpowers/specs/2026-09-03-project-onboarding-design.md` (§3.1, §3.4, §4.3, §4.4 excluding GitHub, §5.3, §6, §7)

## Global Constraints

- Link mode is read-only with respect to the source repository.
- Init mode stages beneath the configured root and publishes with an atomic rename.
- Cleanup may remove only resources recorded as owned by the request.
- Project and primary `project-repo` workspace rows are inserted in one database transaction.
- All Git commands use existing async argument-array APIs; no shell interpolation.
- `create_github=true` and `github_clone` remain structured `not_implemented` failures for the sibling task.
- Tests use `aq test`; never run the full suite or raise the worker count.

---

### Task 1: Integrate Approved Prerequisites

**Files:**
- Merge: `aq/brisk-beacon.1`, `.2`, `.3`, `.5`, `.6`
- Verify: prerequisite-focused tests from each branch

**Interfaces:**
- Consumes: approved design, `ProjectRootConfig`, `src.projects.paths`, Pydantic onboarding requests/results/errors, onboarding query mixin.
- Produces: a compilable branch containing all declared prerequisites without recreating sibling work.

- [ ] **Step 1: Merge prerequisite branches in dependency order**

```bash
git merge --no-edit aq/brisk-beacon.1
git merge --no-edit aq/brisk-beacon.2
git merge --no-edit aq/brisk-beacon.3
git merge --no-edit aq/brisk-beacon.5
git merge --no-edit aq/brisk-beacon.6
```

- [ ] **Step 2: Run prerequisite area tests**

```bash
aq test tests/test_project_roots_config.py tests/test_project_paths.py \
  tests/test_project_onboarding_contract.py tests/test_onboarding_queries.py
```

Expected: all prerequisite tests pass before service work starts.

### Task 2: Specify the Saga Through Failing Service Tests

**Files:**
- Create: `tests/test_project_onboarding_service.py`
- Create: `src/projects/onboarding.py`

**Interfaces:**
- Consumes: `OnboardProjectRequest`, `GetProjectOnboardingResult`, `ProjectOnboardingError`, database request-store methods, `GitManager`, and path helpers.
- Produces: `ProjectOnboardingService.onboard_project(request)` and `ProjectOnboardingService.get_project_onboarding(request_id)`.

- [ ] **Step 1: Write link-mode tests**

Use temporary Git repositories to assert remote/no-remote success, detected default branch, exact project/workspace registration, vault directories, project-ID collision, destination collision, cross-project workspace collision, and root escape. Hash the linked repository tree before/after failures and assert equality.

- [ ] **Step 2: Run link tests and verify RED**

```bash
aq test tests/test_project_onboarding_service.py -k 'link or collision or root_escape' -x
```

Expected: collection/import fails because `ProjectOnboardingService` does not exist.

- [ ] **Step 3: Write init-mode tests**

Assert README content and initial commit when enabled; assert an unborn repository whose symbolic HEAD is `refs/heads/main` when disabled; assert an existing destination is untouched.

- [ ] **Step 4: Run init tests and verify RED**

```bash
aq test tests/test_project_onboarding_service.py -k 'init' -x
```

Expected: failures identify missing init saga behavior.

- [ ] **Step 5: Write idempotency, concurrency, recovery, and rollback tests**

Assert terminal replay equality, fingerprint conflict, same-destination contention returning `destination_locked`, reconstruction after a persisted prepare phase, and registration-failure cleanup of owned rows/directories only.

- [ ] **Step 6: Run recovery tests and verify RED**

```bash
aq test tests/test_project_onboarding_service.py -k 'replay or fingerprint or concurrent or recover or rollback' -x
```

Expected: failures identify missing locking, recovery, and compensation.

### Task 3: Implement the Service and Atomic Registration

**Files:**
- Create: `src/projects/onboarding.py`
- Modify: `src/database/base.py`
- Modify: `src/database/queries/onboarding_queries.py`
- Test: `tests/test_project_onboarding_service.py`

**Interfaces:**
- Produces: `ProjectOnboardingService(db, config, git_manager=None)`; `onboard_project(request)`; `get_project_onboarding(request_id)`; database `register_onboarded_project(project, workspace)` transaction helper.

- [ ] **Step 1: Add the minimal lock and fingerprint layer**

Implement a process-wide keyed lock registry. Acquire non-blocking locks for request ID, project ID, and canonical destination in stable sorted order. Normalize requests with `model_dump(mode="json", exclude_none=False)`, serialize with sorted compact JSON, and hash with SHA-256.

- [ ] **Step 2: Run idempotency/concurrency tests to GREEN**

```bash
aq test tests/test_project_onboarding_service.py -k 'replay or fingerprint or concurrent' -x
```

- [ ] **Step 3: Implement read-only link preflight and registration**

Resolve/revalidate with `src.projects.paths`, verify Git worktree root, use `GitManager.aget_remote_url` and `aget_default_branch`, check project/workspace uniqueness, and register exactly one `Project` plus one enabled primary `Workspace(kind_id="project-repo", source_type=RepoSourceType.LINK)`.

- [ ] **Step 4: Run link tests to GREEN**

```bash
aq test tests/test_project_onboarding_service.py -k 'link or collision or root_escape' -x
```

- [ ] **Step 5: Implement staged init**

Create `.<destination>.aq-onboard-<request-id>` exclusively beneath the root, append ownership before each later phase, run `git init --initial-branch <branch>` asynchronously, create `README.md` with mode `x`, optionally stage/commit, revalidate destination, and publish with `Path.replace`/`os.rename` only when absent.

- [ ] **Step 6: Run init tests to GREEN**

```bash
aq test tests/test_project_onboarding_service.py -k 'init' -x
```

- [ ] **Step 7: Implement recovery and compensation**

Use the persisted phase/ledger to recognize request-owned staging/final paths. Resume safe prepared state, reject mismatched ownership, delete partially inserted rows in one transaction, and recursively remove only ledger-owned init paths. Persist scrubbed terminal errors.

- [ ] **Step 8: Run the full service file to GREEN**

```bash
aq test tests/test_project_onboarding_service.py
```

### Task 4: Share Vault Setup and Wire Commands

**Files:**
- Create: `src/projects/storage.py`
- Modify: `src/commands/project_commands.py`
- Modify: `src/commands/project_onboarding_commands.py`
- Modify: `src/commands/handler.py`
- Test: `tests/test_create_project_auto_channels.py`
- Test: `tests/test_project_onboarding_service.py`

**Interfaces:**
- Produces: shared `ensure_project_storage(data_dir, project_id)` helper and command bodies delegating to one lazily constructed service instance.

- [ ] **Step 1: Write failing command-delegation/status tests**

Assert `_execute_onboard_project` returns the service result, `_execute_get_project_onboarding` maps persisted statuses/phases, and unsupported GitHub modes return `not_implemented` without mutation.

- [ ] **Step 2: Verify RED**

```bash
aq test tests/test_project_onboarding_service.py -k 'command or not_implemented' -x
```

- [ ] **Step 3: Extract and reuse project storage setup**

Move task-directory and vault-directory creation behind one helper used by both `_cmd_create_project` and onboarding registration.

- [ ] **Step 4: Wire the handler-owned service**

Construct the service from `self.orchestrator.db`, `self.config`, and the existing Git manager; keep all HTTP-independent behavior in the service module.

- [ ] **Step 5: Run command and regression tests to GREEN**

```bash
aq test tests/test_project_onboarding_service.py tests/test_create_project_auto_channels.py
```

### Task 5: Add `aq project onboard`

**Files:**
- Modify: `src/cli/projects.py`
- Test: `tests/test_cli_projects.py`

**Interfaces:**
- Produces: `aq project onboard` with common flags plus init-mode README/GitHub flags, forwarding one `onboard_project` request unchanged.

- [ ] **Step 1: Write failing CLI forwarding/output tests**

Use `CliRunner` and a fake API client to assert flags map to `request_id`, `source_mode`, `root_id`, `relative_path`, `project_name`, `project_id`, `default_branch`, `create_readme`, and `create_github`, and success prints project/workspace/path.

- [ ] **Step 2: Verify RED**

```bash
aq test tests/test_cli_projects.py -k onboard -x
```

- [ ] **Step 3: Implement the Click command**

Generate a UUID request ID when omitted, build only mode-valid fields, call `client.execute("onboard_project", args)`, and render stable success details.

- [ ] **Step 4: Verify GREEN**

```bash
aq test tests/test_cli_projects.py -k onboard -x
```

### Task 6: Verify, Document Evidence, Commit, and Push

**Files:**
- Check all changed Python and test paths.

- [ ] **Step 1: Run focused service and dependency suites**

```bash
aq test tests/test_project_onboarding_service.py tests/test_project_onboarding_contract.py \
  tests/test_onboarding_queries.py tests/test_project_paths.py tests/test_project_roots_config.py \
  tests/test_cli_projects.py tests/test_create_project_auto_channels.py
```

- [ ] **Step 2: Run changed-file Ruff**

```bash
ruff check src/projects/onboarding.py src/commands/project_commands.py \
  src/commands/project_onboarding_commands.py src/commands/handler.py \
  src/database/base.py src/database/queries/onboarding_queries.py src/cli/projects.py \
  tests/test_project_onboarding_service.py tests/test_cli_projects.py
```

- [ ] **Step 3: Review ownership and diff**

```bash
git diff --check
git status --short
git diff origin/main...HEAD --stat
```

- [ ] **Step 4: Commit and push**

```bash
git add docs/superpowers/plans/2026-09-05-project-onboarding-service.md \
  src/projects/onboarding.py src/commands/project_commands.py \
  src/commands/project_onboarding_commands.py src/commands/handler.py \
  src/database/base.py src/database/queries/onboarding_queries.py src/cli/projects.py \
  tests/test_project_onboarding_service.py tests/test_cli_projects.py
git commit -m "feat(projects): add local onboarding saga"
git push -u origin HEAD
```

- [ ] **Step 5: Record exact verification and close with `--claim-next`**

Use `aq task comment` for findings/tests, then close with the pushed SHA, prerequisite-stack note, exact `--test`/`--command` evidence, and follow the returned pool status.
