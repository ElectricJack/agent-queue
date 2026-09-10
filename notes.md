# Workspace Routing Bug Investigation: mech-fighters commit landed in agent-queue

<!-- aq:historical -->
> **Historical working note.** Dated scratch material, kept for the record and
> never revised. Not instructions. Start at [the documentation
> home](docs/README.md); see [historical material](docs/history/README.md).

## 1. Root Cause

**The task was created under the wrong project.** The task `clear-orbit` ("Fix 2D brush raycasting when fill all depths is disabled") was assigned `project_id = "agent-queue"` instead of `"mech-fighters"`. Because workspace acquisition is project-scoped (`db.acquire_workspace(task.project_id, ...)`), the task was assigned an `agent-queue` workspace (`ws-agent-queue-1` at `/mnt/d/Dev/agent-queue2`) and the agent committed mech-fighters game code into the agent-queue repository.

**How did the wrong project_id happen?** The task was created via the chat interface (Discord) while the user was in the `agent-queue` project channel. The `create_task` tool infers `project_id` from the current channel context when it's not explicitly specified. The user likely asked for this mech-fighters task while chatting in the agent-queue channel, and the system used the channel's project context.

A related task `swift-rapids` ("Plan layer z-depths and page tiling implementation") was also created under `agent-queue` — this is clearly mech-fighters content too, confirming this was a user-side project context error that affected multiple related tasks.

## 2. Specific Task/Workspace Details

| Field | Value |
|-------|-------|
| **Task ID** | `clear-orbit` |
| **Title** | Fix 2D brush raycasting when fill all depths is disabled |
| **Assigned Project** | `agent-queue` (WRONG — should be `mech-fighters`) |
| **Correct Project** | `mech-fighters` |
| **Workspace Used** | `ws-agent-queue-1` → `/mnt/d/Dev/agent-queue2` |
| **Commit** | `71ef1dc` — "Fix 2D brush raycasting when fill_all_depths is disabled" |
| **Branch** | `clear-orbit/fix-2d-brush-raycasting-when-fill-all-depths-is-disabled` |
| **Files Created** | `src/editor/brush.py`, `src/editor/models.py`, `src/editor/__init__.py`, `tests/test_editor_brush.py` |
| **Related Wrong-Project Task** | `swift-rapids` (Plan layer z-depths) — also under `agent-queue` |

### Additional Database Issue Found

`ws-moss-and-spade-inventory-manager-3` points to `/mnt/d/Dev/agent-queue2` — the agent-queue repo path. This is a workspace misconfiguration that could cause similar cross-project contamination for `moss-and-spade-inventory-manager` tasks.

## 3. Recommended Fix: Git Remote URL Validation

The system has **zero validation** that the workspace's git repository actually belongs to the project. There is no check at task execution time that the repo's remote URL matches the project's `repo_url`. This means any project misconfiguration silently routes work to the wrong repo.

### Fix A: Pre-execution remote URL validation (recommended)

Add a check in `_prepare_workspace()` after workspace acquisition that verifies the git remote URL matches the project's configured `repo_url`. This catches misrouted tasks before any work begins.

**File: `src/orchestrator.py`** — in `_prepare_workspace()`, after line 1584 (`workspace = ws.workspace_path`):

```python
# Validate that the workspace's git remote matches the project's repo_url.
# This prevents cross-project contamination when a task is accidentally
# created under the wrong project or a workspace path is misconfigured.
if project and project.repo_url and await self.git.avalidate_checkout(workspace):
    try:
        actual_remote = await self.git.aget_remote_url(workspace)
        expected = project.repo_url.rstrip("/").removesuffix(".git")
        actual = (actual_remote or "").rstrip("/").removesuffix(".git")
        if actual and expected and actual.lower() != expected.lower():
            logger.error(
                "Workspace %s remote URL mismatch: expected %s, got %s "
                "(task %s, project %s)",
                workspace, project.repo_url, actual_remote,
                task.id, task.project_id,
            )
            await self._notify_channel(
                f"**Workspace Mismatch:** Task `{task.id}` workspace remote "
                f"`{actual_remote}` doesn't match project repo `{project.repo_url}`. "
                f"Task paused to prevent cross-project commits.",
                project_id=task.project_id,
            )
            await self.db.release_workspace(ws.id)
            return None
    except Exception as e:
        logger.warning("Remote URL check failed (non-fatal): %s", e)
```

**File: `src/git/manager.py`** — add a helper method:

```python
async def aget_remote_url(self, checkout_path: str) -> str | None:
    """Get the origin remote URL for a checkout."""
    result = await self._arun(
        ["git", "remote", "get-url", "origin"],
        cwd=checkout_path,
    )
    return result.stdout.strip() if result and result.stdout else None
```

### Fix B: Workspace creation validation

When linking or creating workspaces (`_cmd_add_workspace`), validate that the directory's git remote matches the project's `repo_url`. This prevents misconfigured workspaces from being created in the first place.

**File: `src/command_handler.py`** — in `_cmd_add_workspace()`:

```python
# After resolving workspace_path, before creating the DB record:
if project.repo_url:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=workspace_path, capture_output=True, text=True
    )
    if result.returncode == 0:
        actual = result.stdout.strip().rstrip("/").removesuffix(".git")
        expected = project.repo_url.rstrip("/").removesuffix(".git")
        if actual.lower() != expected.lower():
            return {"error": f"Workspace remote '{result.stdout.strip()}' "
                    f"doesn't match project repo '{project.repo_url}'"}
```

### Fix C: Post-commit validation in `_complete_workspace`

As a defense-in-depth measure, validate the remote URL before merging/pushing in `_complete_workspace()`. This is a last-resort check that prevents pushing to the wrong remote even if earlier checks were bypassed.

## 4. Steps to Prevent Recurrence

1. **Implement Fix A** (pre-execution remote URL validation) — this is the highest-impact fix, catching misrouted tasks before any work begins.

2. **Clean up the bad workspace** — Remove `ws-moss-and-spade-inventory-manager-3` which incorrectly points to `/mnt/d/Dev/agent-queue2`:
   ```sql
   DELETE FROM workspaces WHERE id = 'ws-moss-and-spade-inventory-manager-3';
   ```

3. **Revert commit 71ef1dc from agent-queue** — The mech-fighters code (`src/editor/`, `tests/test_editor_brush.py`) needs to be removed from the agent-queue repo:
   ```bash
   git revert 71ef1dc
   ```

4. **Re-create the task under the correct project** — Create a new task under `mech-fighters` with the same description to get the work done in the right repo.

5. **Consider adding project_id confirmation in chat** — When the chat agent creates tasks, it could confirm the target project when the task description doesn't seem to match the project (e.g., "voxel brush" in a task queue project). This would require LLM-based semantic matching, which conflicts with the "zero LLM for orchestration" principle, so it may be better as an optional feature.

6. **Audit existing workspaces** — Run a one-time check of all workspace paths to verify their git remotes match their assigned project's `repo_url`:
   ```sql
   SELECT w.id, w.project_id, w.workspace_path, p.repo_url
   FROM workspaces w
   JOIN projects p ON w.project_id = p.id
   WHERE p.repo_url != '';
   ```
   Then verify each workspace's `git remote get-url origin` matches.
