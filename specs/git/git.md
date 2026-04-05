# Git Manager Specification

**Source:** `src/git/manager.py`

## 1. Overview

`GitManager` is a wrapper around the `git` CLI and the `gh` (GitHub CLI) tool. It provides both synchronous and asynchronous APIs. There are no direct calls to any git library (e.g. GitPython or libgit2).

- Standard repository operations (clone, branch, commit, push, merge, worktree) use `git` subcommands.
- GitHub-specific operations (creating pull requests, checking PR status) use the `gh` CLI and are therefore only available in environments where `gh` is installed and authenticated.
- The class is instantiated with no arguments and holds no state. All methods accept an explicit `checkout_path` (or `source_path`) to identify which repository to operate on.

### Dual Sync/Async API

`GitManager` provides two parallel APIs:

- **Synchronous API:** Original methods using `subprocess.run` (e.g. `_run`, `create_checkout`, `push_branch`). Retained for backward compatibility and non-async contexts.
- **Asynchronous API:** Every public method has an async counterpart prefixed with `a` (e.g. `acreate_checkout`, `apush_branch`). The core async methods are `_arun` and `_arun_subprocess`, which use `asyncio.create_subprocess_exec()` instead of `subprocess.run`.

**All production callers have been migrated to the async API.** The orchestrator, command handler, and Discord commands exclusively use the `a`-prefixed methods. The synchronous API is retained for backward compatibility and tests only — no production code path calls the sync methods.

## Source Files
- `src/git/manager.py`

---

## 2. Error Handling

### `GitError`

`GitError` is the single exception type raised by this module. It inherits directly from `Exception`.

Any `git` subprocess that exits with a non-zero return code causes `_run` (or `_arun`) to raise:

```
GitError("git <args> failed: <stderr>")
```

`gh` subcommands (`create_pr`, `check_pr_merged`) are not routed through `_run`/`_arun`. They use `subprocess.run` (sync) or `_arun_subprocess` (async) directly and raise `GitError` manually when `returncode != 0`.

### `_run` (internal, synchronous)

```python
def _run(self, args: list[str], cwd: str | None = None, timeout: int | None = None) -> str
```

Executes `["git"] + args` in the given working directory via `subprocess.run`. Captures both stdout and stderr. Uses `timeout` (defaults to `_GIT_TIMEOUT` = 120 seconds) and raises `GitError` on timeout. On success returns `stdout.strip()`. On failure raises `GitError` with the stderr content.

Not used by any production code — all event-loop callers use `_arun` instead.

### `_arun` (internal, asynchronous)

```python
async def _arun(self, args: list[str], cwd: str | None = None, timeout: int | None = None) -> str
```

Async equivalent of `_run`. Executes `["git"] + args` using `asyncio.create_subprocess_exec()` with `asyncio.wait_for()` for timeout handling (defaults to `_GIT_TIMEOUT` = 120 seconds). On timeout, the subprocess is killed via `proc.kill()` to avoid orphaned processes, then raises `GitError`. On success returns decoded `stdout.strip()`. On failure raises `GitError` with the stderr content.

This is the method used by all production code for git operations. All event-loop callers (command handler, orchestrator, Discord commands) use `_arun` exclusively.

### `_arun_subprocess` (internal, asynchronous)

```python
async def _arun_subprocess(self, cmd: list[str], cwd: str | None = None, timeout: int | None = None) -> CompletedProcess
```

Async helper for non-git commands (e.g. `gh` CLI). Uses `asyncio.create_subprocess_exec()` with `asyncio.wait_for()` for timeout handling. Returns a `subprocess.CompletedProcess` with decoded `stdout` and `stderr` strings. On timeout, kills the subprocess and raises `subprocess.TimeoutExpired`. On missing executable, raises `FileNotFoundError`.

Used by async counterparts of `create_pr`, `check_pr_merged`, `check_gh_auth`, `create_github_repo`, and `commit_all` (for `git diff --cached --quiet`).

Several public methods intentionally catch `GitError` and suppress it (e.g. a failed `pull` that has no upstream tracking is silently ignored). Each such suppression is documented in the relevant method section below.

---

## 3. Repository Setup

### `create_checkout(repo_url, checkout_path)`

Clones a remote repository to a local path.

- Creates all intermediate parent directories with `os.makedirs(..., exist_ok=True)`.
- Runs `git clone <repo_url> <checkout_path>`.
- Raises `GitError` on clone failure.

### `validate_checkout(checkout_path)`

Returns `True` if `checkout_path` is a valid git repository, `False` otherwise.

- Returns `False` immediately if the path is not an existing directory.
- Runs `git rev-parse --git-dir` inside the directory. Returns `True` if it succeeds, `False` if it raises `GitError`.
- Does not raise exceptions; always returns a boolean.

### `init_repo(path)`

Initializes a brand-new git repository with an empty initial commit.

- Creates the directory with `os.makedirs(path, exist_ok=True)`.
- Runs `git init`.
- Runs `git commit --allow-empty -m "Initial commit"` to create a HEAD reference immediately (avoids detached-HEAD edge cases on first branch creation).
- Raises `GitError` on failure.

---

## 4. Branching

### `create_branch(checkout_path, branch_name)`

Creates a new branch and checks it out.

- Attempts `git checkout -b <branch_name>`.
- If that fails (branch already exists), falls back to `git checkout <branch_name>`.
- Does not update the branch from the remote; use `prepare_for_task` for a full pre-task setup.

### `checkout_branch(checkout_path, branch_name)`

Switches to an existing local branch.

- Runs `git checkout <branch_name>`.
- Raises `GitError` if the branch does not exist locally.

### `list_branches(checkout_path)`

Returns all local branch names as a list of strings.

- Runs `git branch --list`.
- Each entry in the returned list has leading/trailing whitespace stripped. The current branch retains the `*` prefix that git adds (e.g. `"* main"`).
- Returns an empty list on `GitError` rather than raising.

### `pull_latest_main(checkout_path, default_branch="main")`

Fetches from origin and hard-resets the default branch to match remote. This is the canonical way to sync a workspace's default branch with `origin`.

1. Runs `git fetch origin` to get the latest remote state.
2. Runs `git checkout <default_branch>` to switch to the default branch.
3. Runs `git reset --hard origin/<default_branch>` to discard any local divergence.

This is safer than `git pull` because pull can fail when the local branch has diverged (e.g. from un-pushed merge commits left by `_merge_and_push`). A hard reset unconditionally moves the branch pointer to match the remote. The caller must have the default branch checked out or be prepared for the checkout side-effect.

### `prepare_for_task(checkout_path, branch_name, default_branch="main")`

Full pre-task branch setup. Handles both normal clones and git worktrees differently.

1. Detects whether `checkout_path` is a worktree via `_is_worktree`.
2. Runs `git fetch origin` unconditionally.
3. **Worktree path:**
   - Creates the task branch directly from `origin/<default_branch>` with `git checkout -b <branch_name> origin/<default_branch>`.
   - If that fails (branch already exists — retry scenario), falls back to `git checkout <branch_name>`, then calls `_rebase_onto_default` to rebase the branch onto the latest `origin/<default_branch>` so the agent works on the latest code rather than a stale snapshot. If the rebase encounters conflicts, it is aborted silently — the agent works with whatever is on the branch as-is.
   - Avoids checking out the default branch locally because it may already be checked out in the main working tree, which git forbids.
4. **Normal clone path:**
   - Checks out `default_branch` locally.
   - Runs `git reset --hard origin/<default_branch>` to force the local default branch to match the remote exactly. This is used instead of `git pull` because pull can fail when local has diverged (e.g. from un-pushed merge commits left by `_merge_and_push`). **Resolves G4 for fresh branches.**
   - Creates the task branch with `git checkout -b <branch_name>`.
   - If that fails (task is being retried after a restart), falls back to `git checkout <branch_name>`, then calls `_rebase_onto_default` to rebase the branch onto the latest `origin/<default_branch>` so the agent doesn't work on stale code. **Resolves G4 for retried branches.**
   - If the rebase encounters conflicts, it is aborted silently — the agent works with whatever is on the branch as-is.

### `switch_to_branch(checkout_path, branch_name, default_branch="main", rebase=False)`

Switches to an existing branch, pulling latest, and optionally rebasing onto the
default branch.

Used for subtask branch reuse: when a plan generates multiple subtasks that share
a branch, this lets subsequent subtasks pick up where the previous one left off.

1. Attempts `git fetch origin`. Silently ignores `GitError` (no remote configured).
2. Attempts `git checkout <branch_name>`.
   - If that fails (branch exists only on the remote), tries `git checkout -b <branch_name> origin/<branch_name>` to create a local tracking branch.
   - If that also fails (no remote branch either, e.g. LINK repos), creates a fresh local branch with `git checkout -b <branch_name>`.
3. Attempts `git pull origin <branch_name>`. Silently ignores `GitError` (no upstream tracking).
4. **Rebase (when `rebase=True`):** Calls `_rebase_onto_default(checkout_path, default_branch)` to rebase the branch onto `origin/<default_branch>`, keeping subtask chains closer to main and reducing merge conflicts at the end. If the rebase encounters conflicts, it is aborted silently — the agent works with the branch as-is and conflicts will be handled at merge time.

The `rebase` parameter is controlled by `config.auto_task.rebase_between_subtasks` and is passed by `_prepare_workspace()` when switching to a shared subtask branch.

### `delete_branch(checkout_path, branch_name, *, delete_remote=True)`

Deletes a branch locally and optionally on the remote.

1. Attempts `git branch -d <branch_name>` (safe delete — only if fully merged).
2. If that fails, attempts `git branch -D <branch_name>` (force delete — handles squash-merged PRs).
3. If both local deletes fail, silently suppresses the error (branch may not exist locally).
4. If `delete_remote=True`, runs `git push origin --delete <branch_name>`. Silently ignores `GitError` (branch may not exist on the remote).

### `_rebase_onto_default(checkout_path, default_branch="main")` (internal)

Attempts to rebase the currently checked-out branch onto `origin/<default_branch>`.

1. Runs `git rebase origin/<default_branch>`.
2. If the rebase encounters conflicts, runs `git rebase --abort` and leaves the branch
   in its original pre-rebase state. The agent can still work with the branch — it
   just won't have the latest main changes incorporated.
3. If `rebase --abort` itself fails (rebase may not be in progress if it failed early),
   silently ignores the error.

Used internally by `prepare_for_task` (retry path), `switch_to_branch` (when
`rebase=True`), and `mid_chain_sync`.

---

## 5. Worktrees

Git worktrees allow multiple working trees to share a single `.git` directory. This is used for agent isolation when multiple agents operate on the same repository simultaneously.

### `_is_worktree(checkout_path)` (internal)

Returns `True` if `checkout_path` is a linked worktree (not the main working tree).

- Runs `git rev-parse --git-dir`.
- In a linked worktree the git-dir resolves to `.git/worktrees/<name>`, so the string `"worktrees"` appears in the output.
- In the main working tree the git-dir is simply `.git`.
- Returns `False` on `GitError`.

### `create_worktree(source_path, worktree_path, branch)`

Creates a new linked worktree with a new branch.

- Creates all intermediate parent directories for `worktree_path` with `os.makedirs`.
- Runs `git worktree add -b <branch> <worktree_path>` from `source_path`.
- The new branch is created at the current HEAD of `source_path`.
- Raises `GitError` on failure.

### `remove_worktree(source_path, worktree_path)`

Removes a linked worktree.

- Attempts `git worktree remove <worktree_path>` from `source_path`.
- If that fails (e.g. the worktree has untracked or modified files), retries with `git worktree remove --force <worktree_path>`.
- Raises `GitError` only if the force removal also fails.

---

## 6. Committing and Pushing

### `commit_all(checkout_path, message)`

Stages all changes and creates a commit. Returns `True` if a commit was made, `False` if the working tree was clean. Async counterpart: `acommit_all`.

1. Runs `git add -A` to stage all tracked and untracked changes.
2. **Unstages plan files** — iterates over `_PLAN_FILE_EXCLUDES` (`.claude/plan.md`, `.claude/plans/`, `plan.md`) and runs `git reset HEAD -- <pattern>` for each. Errors are silently ignored (pattern may not be staged or may not exist). This prevents plan files from being committed to target repos.
3. Runs `git diff --cached --quiet` directly via `subprocess.run` (sync) or `_arun_subprocess` (async) — bypassing `_run`/`_arun` — to check whether anything is staged. Exit code `0` means nothing staged.
4. If nothing is staged, returns `False` without creating a commit.
5. Otherwise runs `git commit -m <message>` and returns `True`.
6. Raises `GitError` if the commit fails.

### `push_branch(checkout_path, branch_name, *, force_with_lease=False)`

Pushes a local branch to the `origin` remote.

- Constructs the command as `git push origin <branch_name>`.
- When `force_with_lease=True`, inserts `--force-with-lease` into the command: `git push origin --force-with-lease <branch_name>`. This makes push idempotent for retries: if the branch was already pushed in a previous attempt, a second push with amended/additional commits succeeds as long as no other user pushed to the same branch in the meantime. It is safe for task branches because they are owned by a single agent and are never concurrently updated by others.
- The `force_with_lease` parameter is keyword-only to prevent accidental positional use.
- Used with `force_with_lease=True` by the orchestrator when pushing task branches for PR creation (task branches are agent-owned and safe to force-push).
- Raises `GitError` on failure (e.g. non-fast-forward without the flag, authentication error, or remote ref updated by another clone when using `--force-with-lease`).

### `merge_branch(checkout_path, branch_name, default_branch="main")`

Merges a feature branch into the default branch. Returns `True` on success, `False` on conflict.

1. Runs `git fetch origin` to get the latest remote state. Silently ignores `GitError` (no remote configured, e.g. LINK repos).
2. Checks out `default_branch`.
3. Runs `git reset --hard origin/<default_branch>` to force the local default branch to match the remote. This ensures the merge incorporates the latest remote changes even if other agents have pushed since the last fetch. Silently ignores `GitError` (no remote tracking branch — uses local state as-is). **Resolves G1.**
4. Attempts `git merge <branch_name>`.
5. If the merge raises `GitError` (conflict), runs `git merge --abort` to restore the working tree and returns `False`.
6. Returns `True` on a clean merge.

The fetch-and-hard-reset step ensures that concurrent agents always merge against the latest remote state, preventing stale-main problems. For repos without a remote (LINK repos), the method falls through to merge against whatever local state is available.

> **Note:** For rebase-before-merge conflict resolution, use `sync_and_merge` which
> attempts a rebase of the task branch onto `origin/<default_branch>` when the direct
> merge fails.

### `rebase_onto(checkout_path, branch_name, onto="main")`

Public API for rebasing an arbitrary branch onto a target. Returns `True` on success,
`False` on conflict.

1. Records the currently checked-out branch.
2. Checks out `branch_name`.
3. Runs `git rebase origin/<onto>`.
4. On conflict (`GitError`): runs `git rebase --abort` to restore the branch, then attempts to return to the original branch. Returns `False`.
5. On success: attempts to return to the original branch. Returns `True`.

The caller is responsible for ensuring `onto` is up-to-date (e.g. via a prior fetch + hard-reset). Used by `sync_and_merge` for its rebase-before-merge conflict resolution (Gap G3), and available for callers that need to rebase an arbitrary branch onto any target.

### `sync_and_merge(checkout_path, branch_name, default_branch="main", max_retries=1)`

High-level merge-and-push flow. Returns `(success: bool, error_msg: str)`.

Encapsulates the full sync-merge-push flow as a single operation. Callers (e.g. the
orchestrator's `_merge_and_push`) no longer need to coordinate fetch / checkout /
reset / merge / push individually. **Resolves G1, G2, G3.**

**Steps:**

1. **Fetch:** `git fetch origin` to get the latest remote state.
2. **Reset:** Checkout `default_branch` and `git reset --hard origin/<default_branch>` — discards any stale local state (e.g. un-pushed merge commits from a prior attempt).
3. **Merge:** Attempt `git merge <branch_name>`.
   - On conflict: abort the merge, then attempt rebase-before-merge recovery:
     - Call `rebase_onto(checkout_path, branch_name, default_branch)` to rebase the task branch onto `origin/<default_branch>`.
     - If rebase conflicts: return `(False, "merge_conflict")`. Switch back to `default_branch` for a clean state.
     - If rebase succeeds: checkout `default_branch`, hard-reset again, retry the merge.
     - If retry merge still fails: return `(False, "merge_conflict")`.
4. **Push with retry:** Attempt `git push origin <default_branch>` up to `max_retries + 1` times total.
   - On push rejection (another agent pushed between our fetch and push): run `git pull --rebase origin <default_branch>` to incorporate the new commits, then retry.
   - Repeat up to `max_retries` times.
   - If all attempts fail: return `(False, "push_failed: <details>")`.
5. On success: return `(True, "")`.

**Return values:**

| Result | Meaning |
|--------|---------|
| `(True, "")` | Merge and push succeeded |
| `(False, "merge_conflict")` | Both direct merge and rebase-before-merge failed |
| `(False, "push_failed: ...")` | Push failed after all retries |

### `mid_chain_sync(checkout_path, branch_name, default_branch="main")`

Push intermediate subtask work and rebase onto latest main. Returns `True` on success,
`False` on rebase conflict. **Resolves G6.**

Called between subtask completions in a chained plan to:

1. **Push** current commits to remote — saves intermediate work so it survives agent
   crashes and is visible to other clones. First attempts a plain push; if that fails
   (e.g. branch was previously pushed and rebased), falls back to `--force-with-lease`.
2. **Fetch** latest remote state via `git fetch origin`.
3. **Rebase** the branch onto `origin/<default_branch>` — keeps the subtask chain
   close to main and reduces the chance of large merge conflicts when the final subtask
   merges the accumulated work. If the rebase conflicts, aborts and returns `False`.
4. **Force-push** the rebased branch with `--force-with-lease` — updates the remote ref
   to match the rewritten (rebased) history.

The `push` parameter (when available as keyword-only on `mid_chain_rebase`) enables intermediate progress to be backed up on the remote. Push failures are silently ignored (rebase still succeeded).

All failures are non-fatal: callers should catch exceptions and continue — the next
subtask can still work on the branch as-is.

### `recover_workspace(checkout_path, default_branch="main")`

Reset workspace to a clean state after a failed merge-and-push. **Resolves G2.**

1. Runs `git checkout <default_branch>`.
2. Runs `git reset --hard origin/<default_branch>`.

This undoes any local merge commit left behind by a failed push, ensuring the workspace
is ready for the next task. The orchestrator's `_merge_and_push` calls `recover_workspace` automatically after `sync_and_merge` returns a failure. Best-effort: callers should wrap in try/except if they cannot tolerate failures.

---

## 7. GitHub PR and Repo Operations

These methods use the `gh` CLI rather than `git`. They require `gh` to be installed and authenticated with appropriate repository access. The synchronous versions call `subprocess.run` directly; the async versions use `_arun_subprocess`. Both raise `GitError` on non-zero exit codes (except `check_gh_auth` which returns a boolean).

### `create_pr(checkout_path, branch, title, body, base="main")`

Creates a GitHub pull request. Returns the PR URL as a string.

- Runs: `gh pr create --title <title> --body <body> --base <base> --head <branch>`
- The command is executed with `cwd=checkout_path` so that `gh` can determine the correct GitHub repository from the local git config.
- Returns `stdout.strip()` which is the PR URL (e.g. `https://github.com/owner/repo/pull/123`).
- Raises `GitError` if `gh` exits with a non-zero code.

### `check_pr_merged(checkout_path, pr_url)`

Polls the state of a pull request. Returns one of three values:

| Return value | Meaning |
|---|---|
| `True` | PR has been merged |
| `False` | PR is still open |
| `None` | PR was closed without merging |

- Runs: `gh pr view <pr_url> --json state,mergedAt`
- Parses the JSON response. Checks `state` (uppercased) and `mergedAt`.
- `True` if `state == "MERGED"` or `mergedAt` is non-null.
- `False` if `state == "OPEN"`.
- `None` for all other states (e.g. `"CLOSED"`).
- Raises `GitError` if `gh` exits with a non-zero code.

### `check_gh_auth()`

Checks whether the `gh` CLI is authenticated. Returns `True` if `gh auth status` exits successfully, `False` otherwise (including timeout and missing executable). Async counterpart: `acheck_gh_auth`.

- Used to pre-validate before attempting repository creation, so callers can surface a helpful error message rather than a cryptic `gh` failure.
- Uses a 30-second timeout (shorter than the default `_GIT_TIMEOUT`).
- Does not raise exceptions; always returns a boolean.

### `create_github_repo(name, *, private=True, org=None, description="")`

Creates a GitHub repository via the `gh` CLI. Returns the HTTPS URL of the newly created repository. Async counterpart: `acreate_github_repo`.

- Constructs `gh repo create <org/name or name>` with `--private` or `--public`.
- Optionally includes `--description <description>`.
- Uses a 60-second timeout.
- Parses the URL from stdout (or stderr for some `gh` versions) by scanning for lines starting with `https://` or `http://`.
- Raises `GitError` if `gh` exits with a non-zero code, times out, or if no URL is found in the output.

---

## 8. Inspection

All inspection methods return an empty string or empty list (never raise) when the underlying `git` command fails.

### `get_diff(checkout_path, base_branch="main")`

Returns the full unified diff of the working tree against `base_branch`.

- Runs `git diff <base_branch>`.
- Returns the raw diff string, or `""` on `GitError`.

### `get_changed_files(checkout_path, base_branch="main")`

Returns a list of file paths changed relative to `base_branch`.

- Runs `git diff --name-only <base_branch>`.
- Splits on newlines. Returns `[]` if output is empty or on `GitError`.

### `get_status(checkout_path)`

Returns the output of `git status` as a string.

- Returns `""` on `GitError`.

### `get_current_branch(checkout_path)`

Returns the name of the currently checked-out branch.

- Runs `git rev-parse --abbrev-ref HEAD`.
- Returns `""` on `GitError` (e.g. repository has no commits yet).

### `has_non_plan_changes(checkout_path, default_branch="main", min_files=3, min_lines=50)`

Checks if the branch has substantial code changes beyond plan files. Returns `True` if the diff exceeds the given thresholds, `False` otherwise.

1. Finds the merge-base between `HEAD` and `origin/<default_branch>`. Returns `False` on error (e.g. shallow clone, no remote).
2. Runs `git diff --stat <merge_base>..HEAD` excluding plan file paths (`.claude/plan.md`, `plan.md`, `.claude/plans/`).
3. Parses the summary line for files changed, insertions, and deletions.
4. Returns `True` if `files_changed >= min_files` or `total_lines >= min_lines`.

Returns `False` (conservative) on any git error so callers fall through to normal task-generation behavior. Used by the orchestrator to detect when a plan was already implemented by a prior subtask.

### `get_default_branch(checkout_path)`

Detects the default branch for the repository. Returns the branch name, or `"main"` as a last resort.

Uses three strategies in order:

1. **Remote HEAD symbolic ref:** Runs `git symbolic-ref refs/remotes/origin/HEAD` and extracts the branch name from the `refs/remotes/origin/<branch>` output. Most reliable when the remote has a HEAD ref set.
2. **Common branch names (local):** Checks for `main`, `master`, `develop`, `trunk` by running `git rev-parse --verify <branch>` for each. Returns the first one that exists locally.
3. **Common branch names (remote):** Runs `git ls-remote --heads origin` and checks for the same branch names in the remote refs.
4. **Current branch fallback:** Returns the currently checked-out branch via `get_current_branch`, or `"main"` if that also fails.

Returns a string (never raises). Used by the orchestrator to determine the correct merge target when projects use non-standard default branch names.

### `get_recent_commits(checkout_path, count=5)`

Returns the last `count` commits in one-line format.

- Runs `git log --oneline -<count>`.
- Returns `""` on `GitError`.
- Default `count` is `5`.

---

## 9. Utilities

Both utility methods are `@staticmethod` and do not require a `GitManager` instance.

### `slugify(text)`

Converts arbitrary text into a string safe for use in a branch name.

Transformation steps applied in order:

1. Lowercase and strip leading/trailing whitespace.
2. Remove all characters that are not word characters (`\w`), whitespace, or hyphens.
3. Replace whitespace and underscores (`[\s_]+`) with a single hyphen.
4. Collapse consecutive hyphens (`-+`) into one.
5. Strip leading and trailing hyphens.

Examples:
- `"Fix the OAuth2 bug!"` → `"fix-the-oauth2-bug"`
- `"  update_user_profile  "` → `"update-user-profile"`

### `make_branch_name(task_id, title)`

Combines a task ID and a title into a full branch name.

- Format: `"<task_id>/<slugify(title)>"`
- Example: `make_branch_name("clever-fox", "Fix login timeout")` → `"clever-fox/fix-login-timeout"`
- The slash separator creates a namespaced branch, which most git hosts display as a grouped branch hierarchy.

---

## 10. Async API Reference

Every public method on `GitManager` has an async counterpart prefixed with `a`. The async methods use `_arun` (for git commands) or `_arun_subprocess` (for non-git commands like `gh`) instead of `subprocess.run`. The behavior and semantics are identical to the synchronous versions.

### Async Method Mapping

| Sync Method | Async Method |
|---|---|
| `create_checkout` | `acreate_checkout` |
| `validate_checkout` | `avalidate_checkout` |
| `has_remote` | `ahas_remote` |
| `create_branch` | `acreate_branch` |
| `checkout_branch` | `acheckout_branch` |
| `list_branches` | `alist_branches` |
| `pull_latest_main` | `apull_latest_main` |
| `prepare_for_task` | `aprepare_for_task` |
| `switch_to_branch` | `aswitch_to_branch` |
| `mid_chain_sync` | `amid_chain_sync` |
| `pull_branch` | `apull_branch` |
| `push_branch` | `apush_branch` |
| `rebase_onto` | `arebase_onto` |
| `merge_branch` | `amerge_branch` |
| `sync_and_merge` | `async_and_merge` |
| `recover_workspace` | `arecover_workspace` |
| `delete_branch` | `adelete_branch` |
| `create_worktree` | `acreate_worktree` |
| `remove_worktree` | `aremove_worktree` |
| `init_repo` | `ainit_repo` |
| `get_diff` | `aget_diff` |
| `get_changed_files` | `aget_changed_files` |
| `commit_all` | `acommit_all` |
| `create_pr` | `acreate_pr` |
| `check_pr_merged` | `acheck_pr_merged` |
| `get_status` | `aget_status` |
| `get_current_branch` | `aget_current_branch` |
| `has_non_plan_changes` | `ahas_non_plan_changes` |
| `get_default_branch` | `aget_default_branch` |
| `get_recent_commits` | `aget_recent_commits` |
| `check_gh_auth` | `acheck_gh_auth` |
| `create_github_repo` | `acreate_github_repo` |
| `_is_worktree` | `_ais_worktree` |
| `_rebase_onto_default` | `_arebase_onto_default` |

### Usage

All production callers (orchestrator, command handler, Discord commands) use the async API exclusively. The synchronous API is retained for backward compatibility and tests only — no production code path invokes the sync methods.

```python
# Production code (async — all event-loop callers use this):
branch = await git.aget_current_branch(workspace_path)
await git.apush_branch(workspace_path, branch, force_with_lease=True)

# Tests and non-async contexts only:
branch = git.get_current_branch(workspace_path)
git.push_branch(workspace_path, branch, force_with_lease=True)
```

### Migration Status

The async migration is **complete**. Prior to this migration, all git operations used `subprocess.run()` which blocked the asyncio event loop for up to 120 seconds per call. This caused:

- Discord slash commands to time out (3-second interaction timeout)
- The orchestrator's 5-second cycle to be delayed
- All other async tasks to freeze

The migration converted all production callers to the async API, which uses `asyncio.create_subprocess_exec()` and yields control back to the event loop during subprocess execution.

---

## 11. Design Principles — Workspace Sync

These principles govern how `GitManager` and the orchestrator's workspace methods
interact to keep agent workspaces synchronized. They serve as invariants that must
be preserved when modifying the git sync workflow.

See also: `docs/git-sync-current-state.md` for a prose description of current
strengths with rationale.

### P1. Per-Agent Workspace Isolation

Each `(agent, project)` pair receives its own filesystem-level workspace. Two agents
working on the same project never share a working tree. This eliminates file-level
conflicts (dirty index, mixed staged changes) and makes branch operations safe for
concurrent execution.

**Maintained by:** `_prepare_workspace` in the orchestrator, via
`_compute_workspace_path` and the `agent_workspaces` SQLite cache.

### P2. Branch-per-Task Naming

Every task gets a unique branch named `<task-id>/<slugified-title>`. The task ID
prefix makes branches trivially traceable to their originating task. Plan subtasks
reuse the parent task's branch to accumulate commits sequentially.

**Maintained by:** `make_branch_name`, `prepare_for_task`, `switch_to_branch`.

### P3. Fresh Starting Point Before Each Task

Before creating a task branch, `prepare_for_task` fetches the latest remote state
(`git fetch origin`) and synchronizes the local default branch. Agents always start
from a reasonably recent version of the codebase.

**Maintained by:** `prepare_for_task` (fetch + pull/reset on default branch).

### P4. Atomic Post-Completion Commit

After every task, the orchestrator commits all agent work before any merge, push,
or PR operation. The `commit_all` method uses an add-all-then-check-staged pattern
(`git add -A` followed by `git diff --cached --quiet`) to avoid race conditions
between status checks and staging.

**Maintained by:** `commit_all`, called from `_complete_workspace`.

### P5. Graceful Degradation on Git Errors

Git operations that may legitimately fail (no remote configured, no upstream tracking
branch, network errors during fetch) are caught and suppressed. The outer
`_prepare_workspace` wraps all git operations in a catch-all that logs but still
returns a valid workspace path. An agent can always start work even if branch setup
fails.

**Maintained by:** `try/except GitError: pass` patterns in `prepare_for_task`,
`switch_to_branch`, and the catch-all in `_prepare_workspace`.

### P6. Dual Completion Paths (PR vs Direct Merge)

Tasks requiring approval push the branch and create a GitHub PR. Tasks without
approval merge the branch into the default branch and push. Both paths include
error handling with user-facing notifications. The orchestrator polls PR merge
status for approval-gated tasks.

**Maintained by:** `_complete_workspace`, `_merge_and_push`,
`_create_pr_for_task`, `check_pr_merged`.

### P7. Worktree-Aware Branching

When a checkout is a git worktree (not the main working tree), `prepare_for_task`
avoids checking out the default branch locally (which would conflict with the main
working tree) and instead creates the task branch directly from
`origin/<default_branch>`.

**Maintained by:** `_is_worktree`, worktree branch in `prepare_for_task`.

### P8. Retry Resilience

Both `prepare_for_task` and `switch_to_branch` handle existing branches gracefully
(e.g. after a crash or restart mid-task). Instead of failing, they switch to the
existing branch so work can resume without manual cleanup.

**Maintained by:** fallback `checkout` calls in `prepare_for_task`,
`create_branch`, and `switch_to_branch`.

### P9. Best-Effort Branch Cleanup

After successful merge or PR completion, task branches are deleted locally and
remotely. Cleanup failures are silently ignored to avoid blocking the workflow when
a branch was already deleted (e.g. by GitHub's "delete branch after merge" setting).

**Maintained by:** `delete_branch` (called from `_merge_and_push`).

---

## 12. Known Gaps — Workspace Sync

These are identified weaknesses in the current git sync workflow that cause
failures or data staleness when multiple agents work concurrently. Each gap is
labeled G1–G7 for cross-referencing from other documents and code comments.

### G1. No Pre-Merge Pull in `_merge_and_push` — **RESOLVED**

~~`_merge_and_push()` executes `checkout main → merge branch → push main`, but
never pulls remote changes before the merge.~~

**Resolution:** `merge_branch()` now fetches from origin and hard-resets the local
default branch to `origin/<default_branch>` before merging. The orchestrator's
`_merge_and_push` uses `sync_and_merge()` which encapsulates the full
fetch → checkout → reset → merge → push flow with automatic retry on push failures.

### G2. Push Failures Leave Workspace in a Dirty State — **RESOLVED**

~~After a failed push in `_merge_and_push`, the local `main` contains the merge
commit but `origin/main` does not.~~

**Resolution:** `recover_workspace()` resets the local default branch to
`origin/<default_branch>` after any failed merge-and-push, ensuring the workspace
is clean for the next task. The orchestrator's `_merge_and_push` calls
`recover_workspace` automatically after `sync_and_merge` returns a failure.

### G3. No Merge Conflict Recovery Strategy — **RESOLVED**

~~When `merge_branch()` detects conflicts, it aborts the merge and notifies the
user, but there is no automated attempt to rebase the task branch onto the
latest `main` and retry.~~

**Resolution:** `sync_and_merge()` now attempts rebase-before-merge when a direct
merge fails with conflicts. The task branch is rebased onto `origin/<default_branch>`
via `rebase_onto()`, and the merge is retried. If the rebase itself conflicts
(true content conflict), the original `merge_conflict` error is returned. This
resolves conflicts caused by branch staleness while still reporting true conflicts
to the user.

### G4. Retried Tasks Don't Rebase onto Latest Main — **RESOLVED**

~~When a task retries (branch already exists from a previous attempt),
`prepare_for_task()` falls back to `git checkout <branch_name>` without
rebasing it onto the latest `origin/main`.~~

**Resolution:** `prepare_for_task()` now uses hard-reset on the normal path
and calls `_rebase_onto_default()` on existing branches when a task is retried.
Both the normal clone path and the worktree path rebase existing branches onto
`origin/<default_branch>`, ensuring agents start from recent code even on retry.
If the rebase conflicts, the branch is left as-is (agent can still work with it).

### G5. No `--force-with-lease` for PR Branch Pushes — **RESOLVED**

~~`push_branch` could fail on retry if the branch was previously pushed.~~

**Resolution:** `push_branch` now accepts a `force_with_lease` keyword argument.
When `True`, `--force-with-lease` is added to the push command, making it
idempotent for retries. The orchestrator's `_create_pr_for_task` passes
`force_with_lease=True` when pushing task branches, since these branches are
agent-owned and safe to force-push. Plain push (the default) is still used for
the `sync_and_merge` flow where only the default branch is pushed.

### G6. Subtask Chains Accumulate Drift — **RESOLVED**

~~Plan subtasks share a branch and commit sequentially. Over a long chain,
the branch drifts progressively further from `main`.~~

**Resolution:** Two complementary mechanisms reduce drift:

1. `mid_chain_sync()` is called by the orchestrator's `_complete_workspace` after each
   non-final subtask when `auto_task.rebase_between_subtasks` is enabled. It pushes
   intermediate work to the remote and rebases the chain branch onto
   `origin/<default_branch>`, keeping the branch close to main.

2. `switch_to_branch(..., rebase=True)` is called by `_prepare_workspace` when setting
   up the workspace for the next subtask (also gated by `rebase_between_subtasks`),
   ensuring the branch is rebased before the agent starts work.

Both operations are non-fatal: if rebase conflicts, the branch is left as-is and
the subtask chain continues normally (just with the accumulated drift).

### G7. LINK Repos with Shared Filesystem — No File-Level Locking

Multiple agents assigned to the same LINK repo share a single filesystem
directory. Without worktrees, concurrent agents can clobber each other's
branch state, staged changes, and working tree files. There is no file-level
locking or worktree-per-agent strategy for LINK repos.

**Impact:** Concurrent agents on a LINK repo produce corrupted git state,
lost changes, and unpredictable behavior. Currently mitigated only by the
low probability of multiple agents being assigned to the same LINK project
simultaneously.

**Affected code:** `Orchestrator._prepare_workspace` (LINK path uses
`workspace = repo.source_path` without isolation),
`Orchestrator._compute_workspace_path` (returns shared path for LINK repos).

**Violates:** P1 (Per-Agent Workspace Isolation) — LINK repos are the
exception where isolation is not enforced.

### Gap Summary Table

| Gap | Severity | Root Cause | Status |
|-----|----------|------------|--------|
| G1 | ~~High~~ | ~~Missing `pull` before merge+push~~ | **RESOLVED** — `merge_branch` and `sync_and_merge` fetch+reset before merge |
| G2 | ~~High~~ | ~~No rollback after failed push~~ | **RESOLVED** — `recover_workspace` resets after failures |
| G3 | ~~Medium~~ | ~~No automated rebase-and-retry on conflict~~ | **RESOLVED** — `sync_and_merge` attempts rebase-before-merge |
| G4 | ~~Medium~~ | ~~Retry uses existing branch without rebase~~ | **RESOLVED** — `prepare_for_task` rebases on retry |
| G5 | ~~Low~~ | ~~Plain push instead of `--force-with-lease`~~ | **RESOLVED** — `push_branch` accepts `force_with_lease` flag |
| G6 | ~~Medium~~ | ~~No periodic rebase during subtask chains~~ | **RESOLVED** — `mid_chain_sync` + `switch_to_branch(rebase=True)` |
| G7 | High | LINK repos share filesystem without locking | Open — no file-level isolation for concurrent LINK repo agents |
