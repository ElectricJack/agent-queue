---
tags: [git, workspace, gaps]
---

# Git Sync for Agent Workspaces — Identified Gaps

**Source files:** `src/git/manager.py`, `src/orchestrator.py`
**Companion doc:** [[git-sync-current-state|Current State]]

This document catalogues the gaps in the current git sync workflow that can
cause merge conflicts, failed pushes, and stale code when multiple agents work
concurrently on the same repository. Each gap includes the relevant code
locations, a description of the failure mode, and the impact on the system.

---

## Gap 1 — `_merge_and_push()` Never Pulls Main Before Merging — **RESOLVED**

**Resolution:** `_merge_and_push()` now delegates to `GitManager.sync_and_merge()`,
which performs a full fetch → hard-reset to `origin/<default_branch>` → merge → push
cycle. The local default branch is always synchronized with the remote before merging,
preventing non-fast-forward push failures.

If the push still fails (e.g. another agent pushed between merge and push),
`sync_and_merge` retries with `pull --rebase` before each retry attempt.

**Previously:** `merge_branch()` merged into the local `main` without fetching
or pulling from origin first, causing non-fast-forward push failures when
another agent had pushed since the workspace's last fetch.

---

## Gap 2 — Push Failures Leave Workspace in a Dirty State — **RESOLVED**

**Resolution:** After any merge or push failure, `_merge_and_push()` now calls
`GitManager.recover_workspace()`, which hard-resets the default branch to
`origin/<default_branch>`, discarding any un-pushed merge commits. This ensures
the workspace is always clean for the next task.

Additionally, `prepare_for_task()` now uses hard-reset instead of pull to
synchronize the default branch, so even if recovery was skipped, the next task
starts from a clean state.

**Previously:** A failed push left the local `main` with un-pushed merge commits,
poisoning the workspace for all future tasks.

---

## Gap 3 — No Merge Conflict Recovery Strategy — **RESOLVED**

**Resolution:** `GitManager.sync_and_merge()` now implements a rebase-before-merge
recovery strategy. When the initial merge fails due to conflicts:

1. The merge is aborted.
2. The task branch is rebased onto `origin/<default_branch>` via `rebase_onto()`.
3. If the rebase succeeds, the merge is retried on a freshly-reset default branch.
4. If the rebase itself conflicts, the operation returns `merge_conflict` and the
   user is notified.

This automatically resolves trivially-resolvable conflicts (e.g. lockfile changes)
without human intervention.

**Previously:** A merge conflict caused an immediate abort + notification with
no automated recovery attempt.

---

## Gap 4 — Retried Tasks Don't Rebase onto Latest Main — **RESOLVED**

**Resolution:** `prepare_for_task()` now calls `_rebase_onto_default()` when it
detects that the task branch already exists (retry scenario). After checking out
the existing branch, it rebases onto `origin/<default_branch>` so the agent
starts with the latest upstream changes. If the rebase encounters conflicts,
it is aborted and the branch is left as-is — the agent can still work, just
without the latest main changes.

This applies to both the normal-repo and worktree code paths.

**Previously:** Both code paths fell back to a bare `git checkout <branch>`
without rebasing, leaving the agent working on stale code.

---

## Gap 5 — No `--force-with-lease` for PR Branch Pushes — **RESOLVED**

**Resolution:** `push_branch()` now accepts a `force_with_lease` keyword argument.
When `True`, `--force-with-lease` is added to the push command.  The orchestrator's
`_create_pr_for_task()` passes `force_with_lease=True` when pushing task branches
for PR creation, making retries idempotent while preventing accidental overwrites
of other people's changes.

**Previously:** `push_branch` used a plain `git push origin <branch>` without
`--force-with-lease`, causing retry failures when the branch had already been
pushed in a previous attempt.

---

## Gap 6 — Subtask Chains Accumulate Drift from Main — **RESOLVED**

**Resolution:** `switch_to_branch()` now accepts a `rebase` parameter. When
`True`, it calls `_rebase_onto_default()` after switching to the branch,
incorporating the latest `origin/<default_branch>` changes.

The orchestrator passes `rebase=True` when the `auto_task.rebase_between_subtasks`
config option is enabled (default: `False`). Enable it in `config.yaml`:

```yaml
auto_task:
  rebase_between_subtasks: true
```

If a rebase encounters conflicts, it is aborted and the subtask continues with
the branch as-is — no work is lost.

**Previously:** `switch_to_branch()` fetched and pulled the task branch but never
rebased onto `origin/main`, causing progressive drift over long subtask chains.

---

## Gap 7 — LINK Repos with Shared Filesystem (No Agent Isolation)

**File:** `src/orchestrator.py` (`_compute_workspace_path`, lines 655–677)

### What happens today

```python
def _compute_workspace_path(self, agent, project_id, repo):
    if repo.source_type == RepoSourceType.LINK:
        return repo.source_path  # ← same path for ALL agents
    ...
```

For LINK repos, every agent is given the same `workspace_path`. Unlike CLONE
repos (which get per-agent directories), LINK repos share a single filesystem
directory. There is no file-level locking, no worktree isolation, and no
mechanism to prevent concurrent access.

### Failure mode

Two agents assigned tasks on the same LINK repo will:

1. **Clobber each other's branch state:** Agent A checks out branch-A, then
   Agent B checks out branch-B. Agent A's working tree now has branch-B's
   files — any subsequent commit by Agent A goes to the wrong branch.
2. **Corrupt the index:** Concurrent `git add -A` + `git commit` operations
   can produce corrupt or mixed commits.
3. **Race on checkout:** `prepare_for_task()` does `checkout main → pull →
   checkout -b <branch>`. If two agents interleave these steps, one may create
   its branch from the other's task branch rather than from main.

### Impact

- **Data loss:** Agent work can be committed to the wrong branch or lost
  entirely.
- **Silent corruption:** The system won't detect that agents are interfering
  with each other — commits simply end up in unexpected places.
- **Workaround exists but isn't used:** `GitManager` already has
  `create_worktree()` / `remove_worktree()` methods, but the workspace
  preparation code never uses them for LINK repos.

---

## Summary of Gaps

| # | Gap | Root Cause | Severity |
|---|-----|-----------|----------|
| 1 | ~~No pull before merge in `_merge_and_push()`~~ | ~~Missing `fetch` + `pull` before `merge_branch()`~~ | **RESOLVED** |
| 2 | ~~Push failure leaves diverged local main~~ | ~~No rollback after failed push~~ | **RESOLVED** |
| 3 | ~~No merge conflict recovery~~ | ~~Abort + notify only, no rebase/retry~~ | **RESOLVED** |
| 4 | ~~Retried tasks work on stale branches~~ | ~~No rebase on retry, just `checkout`~~ | **RESOLVED** |
| 5 | ~~PR branch push lacks `--force-with-lease`~~ | ~~Plain `git push` without safety flags~~ | **RESOLVED** |
| 6 | ~~Subtask chains drift from main~~ | ~~No periodic rebase during subtask chain~~ | **RESOLVED** (configurable) |
| 7 | LINK repos share filesystem across agents | `_compute_workspace_path` returns same path | **High** |

### Priority Assessment

**Remaining gap (Gap 7):**
LINK repos with concurrent agents still share a single filesystem directory.
This is the only remaining high-severity gap. The mitigation is that most LINK
projects are single-agent, but the system does not enforce this constraint.
