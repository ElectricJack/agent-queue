---
tags: [git, workspace, sync]
---

# Git Sync for Agent Workspaces — Current State

**Source files:** `src/git/manager.py`, `src/orchestrator.py`
**Design principles reference:** [[specs/git/git]] §10

This document captures what the current git sync workflow does well, identifies
the design decisions already in place, and serves as a baseline for future
improvements to multi-agent workspace synchronization. The strengths documented
here are formalized as design invariants in the git spec (§10) and the
orchestrator spec (§10, "Design Invariants" table) to ensure they are preserved
during refactoring.

---

## What Works Well Today

### 1. Workspace Isolation

Each `(agent, project)` pair gets its own cloned directory, stored at
`{workspace_dir}/{project_id}/{agent.name}/{repo_name}`. This is cached in the
`workspaces` SQLite table so the mapping is stable across restarts.

**Why it matters:** Two agents working on the same project never touch the same
working tree, eliminating an entire class of filesystem-level conflicts (dirty
index, mixed staged changes, etc.).

Three source types are handled cleanly:

| Source type | Workspace strategy |
|---|---|
| **CLONE** | Per-agent clone in workspace dir |
| **LINK** | Shared local path (all agents see the same directory) |
| **INIT** | Per-agent new repo in workspace dir |

### 2. Branch-per-Task Model

Every task gets a unique branch named `<task-id>/<slugified-title>`
(e.g. `brave-fox/add-retry-logic`). The task ID prefix makes branches trivially
traceable back to their originating task, and the slug provides human context.

**Why it matters:** Agents' work is isolated at the git level, not just the
filesystem level. Concurrent agents can work on different branches in their own
clones without interfering with each other.

### 3. Pre-Task Fetch and Pull

`prepare_for_task()` always runs `git fetch origin` before creating the task
branch, ensuring the agent starts from the latest known state of the remote.
For normal clones, it also does `git pull origin <default_branch>` to
fast-forward the local default branch.

**Worktree-aware branching:** When the checkout is a git worktree (detected via
`_is_worktree()`), the code correctly avoids checking out the default branch
locally (which would conflict with the main working tree) and instead creates
the task branch directly from `origin/<default_branch>`.

**Why it matters:** Agents start each task from a reasonably fresh base,
reducing the chance that their work diverges too far from the remote.

### 4. Graceful Error Suppression

Git operations that may legitimately fail (no remote configured, no upstream
tracking branch, network errors during fetch) are wrapped in `try/except
GitError: pass` blocks. This allows LINK repos with no remote and newly-init'd
repos to go through the same code paths as fully-configured CLONE repos.

The outer `_prepare_workspace()` method wraps *all* git operations in a
catch-all that logs a warning but still returns the correct workspace path.
The agent can always start work even if branch setup fails.

**Why it matters:** The system degrades gracefully rather than failing
catastrophically when git operations don't succeed.

### 5. Post-Completion Commit

`_complete_workspace()` always commits agent work using `commit_all()`, which:

1. Runs `git add -A` to stage everything (including untracked files the agent
   created).
2. Checks `git diff --cached --quiet` to detect whether anything is staged.
3. Only creates a commit if there are actual changes.

The add-then-check pattern avoids the race condition of checking working-tree
status before staging.

**Why it matters:** Agent work is never silently lost — every modification is
captured in a commit before any merge/push/PR logic runs.

### 6. Plan Subtask Branch Accumulation

When a plan generates multiple subtasks, they all share the parent task's
branch name. Subtasks use `switch_to_branch()` (which fetches and pulls) rather
than `prepare_for_task()` (which would create a new branch off default). This
lets sequential subtasks accumulate commits on a single branch.

Only the *final* subtask in a chain triggers the merge-or-PR decision, and it
inherits the parent's `requires_approval` flag.

**Why it matters:** A multi-step plan produces a single coherent branch with
all changes, rather than N separate branches that would each need independent
review.

### 7. Dual Completion Paths (PR vs Direct Merge)

The system cleanly supports both:

- **Tasks requiring approval** → push branch + create PR via `gh pr create`.
  Task moves to `AWAITING_APPROVAL`. The orchestrator polls PR status every
  60 seconds via `gh pr view --json state,mergedAt`.
- **Tasks without approval** → merge branch into default + push (CLONE repos)
  or merge locally only (LINK repos).

Both paths include error handling with user-facing notifications on failure.

**Why it matters:** Teams that want human review before code lands can use the
PR path; solo developers or trusted automation can use direct merge.

### 8. Merge Conflict Detection

`merge_branch()` attempts the merge and, on failure, runs `git merge --abort`
to restore the working tree. The orchestrator notifies the user with a clear
message identifying the conflicting task and branch.

**Why it matters:** A failed merge never leaves the working tree in a broken
state, and the user is told exactly which branch needs manual resolution.

### 9. Branch Cleanup

After a successful merge or PR completion, the system attempts to delete the
task branch both locally (`git branch -D`) and remotely
(`git push origin --delete`). This is best-effort — failures are silently
ignored.

**Why it matters:** Prevents branch proliferation without risking errors if
the branch was already cleaned up (e.g. by GitHub's "delete branch after merge"
setting).

### 10. Task Retry Resilience

Both `prepare_for_task()` and `switch_to_branch()` handle the case where the
task branch already exists (e.g. after a crash or restart mid-task). Instead of
failing, they switch to the existing branch so work can resume.

**Why it matters:** The system survives restarts and retries without requiring
manual cleanup of stale branches.

### 11. Approval Polling with Escalation

The `_check_awaiting_approval()` loop handles edge cases thoughtfully:

- **PR-backed tasks:** Polls merge status; transitions to COMPLETED on merge
  or BLOCKED on close-without-merge (with downstream chain notifications).
- **Tasks without a PR URL that don't require approval:** Auto-completes after
  a grace period (handles intermediate subtasks that end up in
  AWAITING_APPROVAL without actually needing review).
- **Tasks without a PR URL that do require approval:** Sends periodic reminders
  (hourly) and escalates after 24 hours to prevent tasks from rotting silently.

**Why it matters:** No task gets permanently stuck in AWAITING_APPROVAL without
the user being notified.

---

## Summary of Existing Strengths

| Capability | Implementation |
|---|---|
| Workspace isolation | Per-agent clone directories, cached in `workspaces` table |
| Branch isolation | Unique `<task-id>/<slug>` branches per task |
| Fresh starting point | `git fetch` + `git pull` before each task |
| Worktree support | Detects worktrees, avoids default-branch checkout conflicts |
| Graceful degradation | Silent error suppression for optional git operations |
| Atomic commits | Add-all-then-check-staged pattern in `commit_all()` |
| Subtask accumulation | Shared branch across plan subtasks with final-step merge |
| PR workflow | `gh` CLI integration for create + poll + complete |
| Direct merge workflow | Merge + push with conflict detection and abort |
| Retry resilience | Existing branches reused on task retry |
| Stuck task detection | Escalating reminders for approval-blocked tasks |

---

## Identified Gaps

The following gaps have been identified in the current workflow. Each is
labeled G1--G7 for traceability. See [[specs/git/git]] §11 for the formal
gap catalogue with affected code references and violated design principles.
For the full gap analysis, see [[git-sync-gaps]].

### G1. `_merge_and_push` Never Pulls Main Before Merging — **RESOLVED**

Now delegates to `sync_and_merge()` which does fetch → hard-reset → merge → push with retry.

### G2. Push Failures Leave Workspace in a Dirty State — **RESOLVED**

`recover_workspace()` resets to `origin/<default_branch>` after any failure. `prepare_for_task()` also uses hard-reset instead of pull.

### G3. No Merge Conflict Recovery Strategy — **RESOLVED**

`sync_and_merge()` attempts rebase-before-merge when the initial merge fails, then retries.

### G4. Retried Tasks Don't Rebase onto Latest Main — **RESOLVED**

`prepare_for_task()` now calls `_rebase_onto_default()` when the branch already exists.

### G5. No `--force-with-lease` for PR Branch Pushes — **RESOLVED**

`push_branch()` accepts `force_with_lease=True` for idempotent PR branch pushes.

### G6. Subtask Chains Accumulate Drift — **RESOLVED**

`switch_to_branch()` accepts a `rebase` parameter. Enable with `auto_task.rebase_between_subtasks: true` in config.

### G7. LINK Repos with Shared Filesystem — OPEN

LINK repos use the source path directly as the workspace for all agents.
Without worktrees or per-agent clones, concurrent agents share the git index,
staging area, and working tree.

**Current mitigation:** Low probability — most LINK projects are single-agent.
But the system does not *enforce* this constraint, so it is a latent failure
mode.

---

## Gap Summary

| Gap | Severity | Status |
|-----|----------|--------|
| G1 | ~~High~~ | **RESOLVED** — `sync_and_merge()` fetches + hard-resets before merging |
| G2 | ~~High~~ | **RESOLVED** — `recover_workspace()` cleans up after failures |
| G3 | ~~Medium~~ | **RESOLVED** — rebase-before-merge recovery in `sync_and_merge()` |
| G4 | ~~Medium~~ | **RESOLVED** — `prepare_for_task()` rebases on retry |
| G5 | ~~Low~~ | **RESOLVED** — `push_branch` supports `--force-with-lease` |
| G6 | ~~Medium~~ | **RESOLVED** — configurable rebase between subtasks |
| G7 | **High** | **OPEN** — LINK repos share filesystem across agents |
