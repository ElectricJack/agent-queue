---
tags: [spec, command-handler, core]
---

# CommandHandler Specification

## 1. Overview

`CommandHandler` is the unified execution layer for all operational commands in AgentQueue. It is the single code path through which every operation must pass — both Discord slash commands and the LLM chat agent tools delegate their business logic here. Presentation and formatting are handled by the callers; this layer concerns itself only with execution and returning structured results.

The handler holds a reference to an [[specs/orchestrator|Orchestrator]] instance (which provides database access and git operations) and an `AppConfig`. It also maintains a small amount of conversational state: an optional `_active_project_id` (the currently focused project), and an optional `_on_project_deleted` callback that external layers (e.g. the Discord bot) can register to react to project deletions.

> **Future evolution:** See [[design/playbooks]] Section 15 for new playbook management commands.

The `db` property is a convenience accessor that returns `self.orchestrator.db`.

---

## Source Files
- `src/command_handler.py`

---

## 2. Architecture

### Command Dispatch

All commands are executed through a single public method:

```python
async def execute(self, name: str, args: dict) -> dict
```

Internally, `execute` looks up a method named `_cmd_{name}` on the instance using `getattr`. If found, the method is called with `args` and its return value is returned. If no such method exists, `{"error": "Unknown command: {name}"}` is returned. Any unhandled exception raised inside a command method is caught and returned as `{"error": str(e)}`.

### Command Registration

Commands are registered implicitly: any instance method named `_cmd_<command_name>` becomes a callable command. There is no explicit registry or decorator. Adding a new command means defining a new `_cmd_` method.

### Constructor Signature

```python
CommandHandler(orchestrator: Orchestrator, config: AppConfig)
```
See [[specs/models-and-state-machine]] for the domain models referenced throughout these commands.

After construction, callers may set `handler._on_project_deleted = callback` to register a post-deletion hook (signature: `callback(project_id: str) -> None`).

---

## 3. Return Format

Every command returns a `dict`. The conventions are:

- **Success:** A dict containing the relevant data. The exact keys vary by command. No `"success"` key is used explicitly; the absence of an `"error"` key signals success.
- **Error:** A dict with a single key `"error"` whose value is a human-readable string describing what went wrong.

Examples:
```python
{"created": "my-project", "name": "My Project", "workspace": "/path/to/workspace"}
{"error": "Project 'foo' not found"}
```

Some commands include both a primary result and an optional `"warning"` key when a non-fatal concern exists (e.g. IN_PROGRESS tasks during a destructive git operation).

---

## 4. Command Categories

### Status

---

#### `get_status`

Returns a system-wide snapshot of the orchestrator state.

**Parameters:** None.

**Behavior:** Queries all projects, agents, and tasks. For each agent that has a current task, the task's title, project, and status are included. Tasks are filtered into `in_progress` and `ready_to_work` lists.

**Returns on success:**
```python
{
    "projects": <int: total project count>,
    "agents": [
        {
            "id": <str>,
            "name": <str>,
            "state": <str: agent state value>,
            "working_on": {             # present only if agent has a current task
                "task_id": <str>,
                "title": <str>,
                "project_id": <str>,
                "status": <str>,
            }
        },
        ...
    ],
    "tasks": {
        "total": <int>,
        "by_status": {<status_value>: <count>, ...},
        "in_progress": [{"id": <str>, "title": <str>, "project_id": <str>, "assigned_agent": <str|None>}, ...],
        "ready_to_work": [{"id": <str>, "title": <str>, "project_id": <str>}, ...],
    },
    "orchestrator_paused": <bool>,
}
```

**Errors:** None expected.

---

### Projects

---

#### `list_projects`

Returns all projects.

**Parameters:** None.

**Behavior:** Fetches all projects from the database and returns their core fields. If a project has a Discord channel linked, `discord_channel_id` is included.

**Returns on success:**
```python
{
    "projects": [
        {
            "id": <str>,
            "name": <str>,
            "status": <str>,
            "credit_weight": <float>,
            "max_concurrent_agents": <int>,
            "workspace": <str>,
            "discord_channel_id": <str>,   # present only if set
        },
        ...
    ]
}
```

**Errors:** None expected.

---

#### `create_project`

Creates a new project and its workspace directory.

**Parameters:**
- `name` (required): Human-readable project name. The project ID is derived by lowercasing and replacing spaces with hyphens.
- `credit_weight` (optional, default `1.0`): Scheduler weight for this project.
- `max_concurrent_agents` (optional, default `2`): Maximum agents that can work on this project simultaneously.
- `auto_create_channels` (optional): Boolean override for whether the Discord layer should auto-create a channel. If not provided, falls back to `config.discord.per_project_channels.auto_create`.

**Behavior:** Derives the project ID from the name. Creates the workspace directory at `{config.workspace_dir}/{project_id}`. Saves the project to the database.

**Returns on success:**
```python
{
    "created": <str: project_id>,
    "name": <str>,
    "workspace": <str: path>,
    "auto_create_channels": <bool>,
}
```

**Errors:** None expected (directory creation errors will propagate as uncaught exceptions).

---

#### `pause_project`

Sets a project's status to PAUSED.

**Parameters:**
- `project_id` (required)

**Returns on success:**
```python
{"paused": <str: project_id>, "name": <str>}
```

**Errors:**
- Project not found.

---

#### `resume_project`

Sets a project's status to ACTIVE.

**Parameters:**
- `project_id` (required)

**Returns on success:**
```python
{"resumed": <str: project_id>, "name": <str>}
```

**Errors:**
- Project not found.

---

#### `edit_project`

Updates one or more mutable fields on a project.

**Parameters:**
- `project_id` (required)
- `name` (optional): New display name.
- `credit_weight` (optional): New scheduler weight.
- `max_concurrent_agents` (optional): New concurrency limit.

At least one optional field must be supplied.

**Returns on success:**
```python
{"updated": <str: project_id>, "fields": [<list of updated field names>]}
```

**Errors:**
- Project not found.
- No updatable fields provided.

---

#### `delete_project`

Deletes a project and all associated database records (cascade). Fires `_on_project_deleted` callback if registered.

**Parameters:**
- `project_id` (required)
- `archive_channels` (optional, default `False`): Passed through to the caller in the response so the Discord layer can act on it. This handler does not archive channels itself.

**Behavior:** Checks whether any tasks are currently IN_PROGRESS and refuses deletion if so. Captures the project's Discord channel ID before deletion. After the database cascade completes, calls `_on_project_deleted(project_id)` if the callback is registered.

**Returns on success:**
```python
{
    "deleted": <str: project_id>,
    "name": <str>,
    "channel_ids": {"channel": <str>},   # present only if a channel was linked
    "archive_channels": True,            # present only if archive_channels=True was passed
}
```

**Errors:**
- Project not found.
- One or more tasks are IN_PROGRESS (caller must stop them first).

---

### Channels

---

#### `set_project_channel`

Links an existing Discord channel to a project by storing the channel ID on the project record.

**Parameters:**
- `project_id` (required)
- `channel_id` (required): The Discord channel ID (as a string).

**Returns on success:**
```python
{
    "project_id": <str>,
    "channel_id": <str>,
    "status": "linked",
}
```

**Errors:**
- Project not found.

---

#### `set_control_interface`

Sets a project's channel by resolving a channel *name* to an ID, then delegating to `set_project_channel`.

**Parameters:**
- `project_id` (required; also accepted as `project_name`)
- `channel_name` (required): The Discord channel name (leading `#` is stripped).
- `_resolved_channel_id` (optional): If the caller (e.g. Discord slash command layer) has already resolved the channel ID, it may pass it here to skip the name-lookup step.
- `guild_channels` (optional): A list of `{"id": <str/int>, "name": <str>}` dicts representing all text channels in the guild. Required if `_resolved_channel_id` is not provided.

**Behavior:** Strips `#` from `channel_name`. Uses `_resolved_channel_id` if present; otherwise iterates `guild_channels` looking for a name match. If no match is found, returns an error. On success, delegates to `_cmd_set_project_channel`.

**Returns on success:** Same as `set_project_channel`.

**Errors:**
- `project_id` or `channel_name` not provided.
- Channel name not found in `guild_channels`.
- No guild context available to resolve the name (neither `_resolved_channel_id` nor `guild_channels` was supplied).
- Project not found (from delegated call).

---

#### `get_project_channels`

Returns the Discord channel ID configured for a project.

**Parameters:**
- `project_id` (required)

**Returns on success:**
```python
{
    "project_id": <str>,
    "channel_id": <str | None>,
}
```

**Errors:**
- Project not found.

---

#### `get_project_for_channel`

Reverse lookup: given a Discord channel ID, returns the project linked to it.

**Parameters:**
- `channel_id` (required): The Discord channel ID (coerced to string).

**Behavior:** Scans all projects, comparing `discord_channel_id` to the given channel ID. Returns the first match. If no project is linked to this channel, returns the response with `project_id: null`.

**Returns on success (match found):**
```python
{
    "channel_id": <str>,
    "project_id": <str>,
    "project_name": <str>,
}
```

**Returns on success (no match):**
```python
{
    "channel_id": <str>,
    "project_id": None,
    "project_name": None,
}
```

**Errors:**
- `channel_id` not provided.

---

### Tasks

---

#### `list_tasks`

Returns up to 200 tasks, optionally filtered.

**Parameters:**
- `project_id` (optional): Filter by project.
- `status` (optional): Filter by status string (e.g. `"READY"`, `"IN_PROGRESS"`).

**Returns on success:**
```python
{
    "tasks": [
        {
            "id": <str>,
            "project_id": <str>,
            "title": <str>,
            "status": <str>,
            "priority": <int>,
            "assigned_agent": <str | None>,
        },
        ...
    ],
    "total": <int: total matching tasks before the 200-cap>,
}
```

**Errors:** None expected.

---

#### `create_task`

Creates a new task in READY status. If no `project_id` is given, the active project is used. Returns an error if no project can be resolved.

**Parameters:**
- `title` (required): Short task title.
- `description` (optional, falls back to `title`): Full task description/prompt for the agent.
- `project_id` (optional, falls back to active project): Project to assign the task to.
- `priority` (optional, default: `100`): Scheduling priority (lower value = higher priority).
- `requires_approval` (optional, default: `False`): If true, task moves to AWAITING_APPROVAL instead of COMPLETED when done.
- `task_type` (optional): Task type classification (feature, bugfix, docs, etc.).
- `profile_id` (optional): Agent profile to use for execution.
- `preferred_workspace_id` (optional): Specific workspace to use.
- `attachments` (optional): List of file paths or URLs for additional context.

**Behavior:** Generates a human-readable task ID using `generate_task_id`. Creates the task in READY status.

**Returns on success:**
```python
{
    "created": <str: task_id>,
    "title": <str>,
    "project_id": <str>,
    "repo_id": <str>,          # present only if repo_id was supplied
    "requires_approval": True, # present only if requires_approval=True
}
```

**Errors:** None expected (DB errors propagate as uncaught exceptions).

---

#### `get_task`

Returns full details for a single task.

**Parameters:**
- `task_id` (required)

**Returns on success:**
```python
{
    "id": <str>,
    "project_id": <str>,
    "title": <str>,
    "description": <str>,
    "status": <str>,
    "priority": <int>,
    "assigned_agent": <str | None>,
    "retry_count": <int>,
    "max_retries": <int>,
    "requires_approval": <bool>,
    "pr_url": <str>,   # present only if set
}
```

**Errors:**
- Task not found.

---

#### `edit_task`

Updates one or more mutable fields on a task.

**Parameters:**
- `task_id` (required)
- `title` (optional)
- `description` (optional)
- `priority` (optional)
- `project_id` (optional): Move task to a different project.
- `status` (optional): Change status (goes through proper transition logging).
- `task_type` (optional): Change task type classification.
- `max_retries` (optional): Update retry limit.
- `verification_type` (optional): Change verification mode.
- `profile_id` (optional): Change agent profile.

At least one optional field must be supplied.

**Returns on success:**
```python
{"updated": <str: task_id>, "fields": [<list of updated field names>]}
```

**Errors:**
- Task not found.
- No updatable fields provided.

---

#### `stop_task`

Stops a running task by delegating to `orchestrator.stop_task`.

**Parameters:**
- `task_id` (required)

**Returns on success:**
```python
{"stopped": <str: task_id>}
```

**Errors:**
- Any error string returned by the orchestrator's stop logic.

---

#### `restart_task`

Resets a task back to READY status, clearing retry count and agent assignment.

**Parameters:**
- `task_id` (required)

**Behavior:** Refuses if the task is currently IN_PROGRESS (caller must stop it first). Transitions the task to READY, sets `retry_count=0`, clears `assigned_agent_id`.

**Returns on success:**
```python
{
    "restarted": <str: task_id>,
    "title": <str>,
    "previous_status": <str>,
}
```

**Errors:**
- Task not found.
- Task is currently IN_PROGRESS.

---

#### `delete_task`

Deletes a task. If the task is IN_PROGRESS, it is stopped first.

**Parameters:**
- `task_id` (required)

**Behavior:** If the task is IN_PROGRESS, calls `orchestrator.stop_task` first and returns an error if stopping fails. After stopping (or if already stopped), deletes the task from the database.

**Returns on success:**
```python
{"deleted": <str: task_id>, "title": <str>}
```

**Errors:**
- Task not found.
- Could not stop the running task before deleting.

---

#### `approve_task`

Approves a task that is in AWAITING_APPROVAL status, moving it to COMPLETED.

**Parameters:**
- `task_id` (required)

**Behavior:** Validates the task is in AWAITING_APPROVAL. Transitions to COMPLETED and logs a `task_completed` event.

**Returns on success:**
```python
{"approved": <str: task_id>, "title": <str>}
```

**Errors:**
- Task not found.
- Task is not in AWAITING_APPROVAL status.

---

#### `set_task_status`

Administratively force a task into any status (bypasses normal state-machine guards).

**Parameters:**
- `task_id` (required)
- `status` (required): Target status string (e.g. `"READY"`, `"BLOCKED"`, `"COMPLETED"`).

**Returns on success:**
```python
{
    "task_id": <str>,
    "old_status": <str>,
    "new_status": <str>,
    "title": <str>,
}
```

**Errors:**
- Task not found.

---

#### `skip_task`

Skips a BLOCKED or FAILED task to unblock its dependency chain.

**Parameters:**
- `task_id` (required)

**Behavior:** Delegates to `orchestrator.skip_task`, which marks the task as skipped and promotes any downstream tasks that were waiting only on this one.

**Returns on success:**
```python
{
    "skipped": <str: task_id>,
    "unblocked_count": <int>,
    "unblocked": [{"id": <str>, "title": <str>}, ...],
}
```

**Errors:**
- Any error string returned by the orchestrator's skip logic.

---

#### `get_chain_health`

Reports on dependency chain health — identifies downstream tasks that are stuck because an upstream task is BLOCKED.

**Parameters:**
- `task_id` (optional): Check a specific task's downstream chain.
- `project_id` (optional): Check all blocked tasks in a project.

If neither is provided, falls back to `_active_project_id`.

**Behavior (task_id supplied):**
- If the task is not BLOCKED, returns immediately with an empty `stuck_downstream` list.
- If BLOCKED, calls `orchestrator._find_stuck_downstream(task_id)` and returns the list of stuck tasks.

**Behavior (project_id or active project):**
- Fetches all BLOCKED tasks for the project, checks each one for stuck downstream tasks, and aggregates the chains.

**Returns on success (single task, not blocked):**
```python
{
    "task_id": <str>,
    "status": <str>,
    "stuck_downstream": [],
    "message": "Task is not blocked — no stuck chain.",
}
```

**Returns on success (single task, blocked):**
```python
{
    "task_id": <str>,
    "title": <str>,
    "status": <str>,
    "stuck_downstream": [{"id": <str>, "title": <str>, "status": <str>}, ...],
    "stuck_count": <int>,
}
```

**Returns on success (project scope):**
```python
{
    "project_id": <str | None>,
    "stuck_chains": [
        {
            "blocked_task": {"id": <str>, "title": <str>},
            "stuck_downstream": [{"id": <str>, "title": <str>}, ...],
            "stuck_count": <int>,
        },
        ...
    ],
    "total_stuck_chains": <int>,
}
```

**Errors:**
- Task not found (when `task_id` is supplied).

---

#### `get_task_result`

Returns the stored result record for a task (the structured output saved when the agent finished).

**Parameters:**
- `task_id` (required)

**Returns on success:** The raw result dict as stored in the database.

**Errors:**
- No result record found for the task.

---

#### `get_task_diff`

Returns the git diff for a task's branch relative to the repository's default branch.

**Parameters:**
- `task_id` (required)

**Behavior:** Looks up the task's `repo_id` and `branch_name`. Determines the checkout path: first tries the assigned agent's `checkout_path`, then falls back to `repo.source_path`. Calls `git.get_diff(checkout_path, repo.default_branch)`.

**Returns on success:**
```python
{
    "diff": <str: diff text or "(no changes)">,
    "branch": <str: task branch name>,
}
```

**Errors:**
- Task not found.
- Task has no associated repository.
- Repository not found.
- Task has no branch name.
- Could not determine checkout path.

---

#### `get_agent_error`

Returns diagnostic information about a task's most recent failure.

**Parameters:**
- `task_id` (required)

**Behavior:** Fetches the task and its result. Classifies the error type using `classify_error`. Truncates the error message to 2000 characters and the agent summary to 1000 characters.

**Returns on success:**
```python
{
    "task_id": <str>,
    "title": <str>,
    "status": <str>,
    "retries": "<retry_count> / <max_retries>",
    "message": "No result recorded yet for this task",  # if no result exists
    # OR, if a result exists:
    "result": <str: result value>,
    "error_type": <str: classified error type>,
    "error_message": <str | None: truncated to 2000 chars>,
    "suggested_fix": <str: from classify_error>,
    "agent_summary": <str>,  # present only if summary exists, truncated to 1000 chars
}
```

**Errors:**
- Task not found.

---

### Plan Approval

---

#### `approve_plan`

Approve a plan and create subtasks from it.

**Parameters:**
- `task_id` (required)

**Behavior:** The task must be in `AWAITING_PLAN_APPROVAL` status. Reads stored plan data from `task_context` entries (saved by `_discover_and_store_plan` as `plan_raw`). Creates subtasks directly with dependency chains, delegating to the [[specs/supervisor|Supervisor]] for LLM-based plan splitting when needed. Then calls `_cleanup_plan_files_after_approval(task)` to delete plan files from the workspace and branch. Transitions the task to `COMPLETED` with context `"plan_approved"` and logs a `"plan_approved"` event.

**Returns on success:**
```python
{
    "approved": <str: task_id>,
    "title": <str>,
    "subtask_count": <int>,
    "subtasks": [{"id": <str>, "title": <str>}, ...],
}
```

**Errors:**
- Task not found.
- Task is not awaiting plan approval.

---

#### `reject_plan`

Reject a plan and reopen the task with feedback for revision.

**Parameters:**
- `task_id` (required)
- `feedback` (required): revision instructions for the agent.

**Behavior:** The task must be in `AWAITING_PLAN_APPROVAL` status. Appends feedback to the task description (under a `**Plan Revision Requested:**` separator). Transitions the task back to `READY` with context `"plan_rejected"`, resetting `retry_count`, `assigned_agent_id`, and `pr_url`. Stores feedback as a `plan_revision_feedback` task context entry and logs a `"plan_rejected"` event.

**Returns on success:**
```python
{
    "rejected": <str: task_id>,
    "title": <str>,
    "status": "READY",
    "feedback_added": True,
}
```

**Errors:**
- Task not found.
- Task is not awaiting plan approval.
- Feedback is required (empty feedback).

---

#### `delete_plan`

Delete a plan and complete the task without creating subtasks.

**Parameters:**
- `task_id` (required)

**Behavior:** The task must be in `AWAITING_PLAN_APPROVAL` status. Calls `_cleanup_plan_files_after_approval(task)` to delete plan files from the workspace. Transitions the task to `COMPLETED` with context `"plan_deleted"` and logs a `"plan_deleted"` event.

**Returns on success:**
```python
{
    "deleted": <str: task_id>,
    "title": <str>,
}
```

**Errors:**
- Task not found.
- Task is not awaiting plan approval.

---

### Plan File Cleanup Helper

#### `_cleanup_plan_files_after_approval(task)`

Private helper called by `approve_plan` and `delete_plan`.

**Behavior:** Retrieves the workspace for the task via `db.get_workspace_for_task(task.id)`. Uses `ws.workspace_path` to locate the workspace directory.

1. **Delete archived plan:** Looks up the `plan_archived_path` task context entry to find the archived file path (`.claude/plans/<task_id>-plan.md`). Removes it if it exists.
2. **Delete original plan files:** Checks for `.claude/plan.md` and `plan.md` in the workspace root — these may still exist if archival failed or a copy was left behind.
3. **Commit deletions:** If any files were deleted and the workspace is a valid git checkout, commits via `git.acommit_all` with a `chore: delete plan file after approval` message.

All I/O errors are caught and logged as warnings — cleanup failures never propagate.

---

### Agents

---

#### `list_agents`

Returns all registered agents.

**Parameters:** None.

**Returns on success:**
```python
{
    "agents": [
        {
            "id": <str>,
            "name": <str>,
            "type": <str: agent_type>,
            "state": <str: agent state value>,
            "current_task": <str | None: task_id>,
        },
        ...
    ]
}
```

**Errors:** None expected.

---

#### `create_agent`

Registers a new agent.

**Parameters:**
- `name` (required): Human-readable agent name. Agent ID is derived by lowercasing and replacing spaces with hyphens.
- `agent_type` (optional, default `"claude"`): Agent type identifier.
- `repo_id` (optional): Associate the agent with a specific repo. Validated to exist.

**Returns on success:**
```python
{
    "created": <str: agent_id>,
    "name": <str>,
    "repo_id": <str>,  # present only if repo_id was supplied
}
```

**Errors:**
- Repo not found (if `repo_id` was supplied).

---

### Repos

> **Note:** The `add_repo` and `list_repos` commands no longer exist in the implementation.
> They have been replaced by the workspace subsystem. See Workspaces section below.

---

### Workspaces

---

#### `add_workspace`

Creates a workspace for a project.

**Parameters:**
- `project_id` (required)
- `path` (required): Filesystem path for the workspace.
- `source` (optional): One of `"clone"`, `"link"`, or `"init"`.
- `name` (optional): Human-readable workspace name.

---

#### `list_workspaces`

Lists workspaces with lock status.

**Parameters:**
- `project_id` (optional)

---

#### `remove_workspace`

Deletes a workspace.

**Parameters:**
- `workspace_id` (required)

---

#### `release_workspace`

Admin force-release a stuck workspace lock.

**Parameters:**
- `workspace_id` (required)

---

#### `queue_sync_workspaces`

Queues a high-priority `SYNC` task that orchestrates a full workspace sync workflow (see
orchestrator spec §9c for the execution flow).  Before creating the task, two early-out
checks prevent unnecessary work.

**Parameters:**
- `project_id` (required; falls back to `_active_project_id`)

**Early-out: duplicate sync task.**
Queries `db.list_active_tasks` for the project, filtering to tasks with
`task_type=SYNC` and `status in (READY, ASSIGNED, IN_PROGRESS)`.  If any exist, returns
immediately without creating a new task:

```python
{
    "already_queued": True,
    "existing_task_ids": [<str>, ...],
    "project_id": <str>,
    "message": "Sync Workspaces task already exists for '...': .... Skipping duplicate.",
}
```

**Early-out: workspaces already synced.**
Iterates over all workspaces for the project and checks each with
`git.aget_current_branch` and `git.alist_branches`.  If every workspace is on the
default branch with no feature branches, returns immediately:

```python
{
    "already_synced": True,
    "project_id": <str>,
    "workspace_count": <int>,
    "default_branch": <str>,
    "message": "All N workspace(s) for '...' are already on '...' with no feature branches. No sync needed.",
}
```

If a workspace directory does not exist or a git check raises an exception, assumes the
workspace needs syncing (errs on the side of creating the task).

**Normal path.**
Creates a `Task` with `task_type=SYNC`, `priority=1` (highest), `status=READY`.
Returns:

```python
{
    "queued": <str: task_id>,
    "project_id": <str>,
    "title": "Sync Workspaces — <project_id>",
    "priority": 1,
    "workspace_count": <int>,
    "default_branch": <str>,
    "message": "Sync Workspaces task queued with highest priority ...",
}
```

**Errors:**
- `project_id` not provided (and no active project set).
- Project not found.
- No workspaces found for the project.

---

### Git High-Level Commands

These commands operate at the project level and are intended for human-facing use from Discord. They resolve the repo path via `_resolve_repo_path` and may include a `"warning"` field if IN_PROGRESS tasks exist.

---

#### `create_branch`

Creates and checks out a new branch in a project's repository.

**Parameters:**
- `project_id` (required)
- `branch_name` (required)
- `repo_id` (optional): Specific repo; otherwise the project's first repo is used.

**Returns on success:**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "branch": <str: branch_name>,
    "status": "created",
}
```

**Errors:**
- `branch_name` not provided.
- Repo path resolution failure (see `_resolve_repo_path`).
- Git error (e.g. branch already exists).

---

#### `checkout_branch`

Checks out an existing branch.

**Parameters:**
- `project_id` (required)
- `branch_name` (required)
- `repo_id` (optional)

**Behavior:** After successful checkout, calls `_warn_if_in_progress` and includes the warning in the result if any tasks are running.

**Returns on success:**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "branch": <str: branch_name>,
    "status": "checked_out",
    "warning": <str>,  # present only if IN_PROGRESS tasks exist
}
```

**Errors:**
- `branch_name` not provided.
- Repo path resolution failure.
- Git error (e.g. branch not found, dirty working tree).

---

#### `commit_changes`

Stages all changes and creates a commit.

**Parameters:**
- `project_id` (required)
- `message` (required): Commit message.
- `repo_id` (optional)

**Behavior:** Calls `git.commit_all`. If there is nothing to commit, returns `status: "nothing_to_commit"` (not an error). Calls `_warn_if_in_progress` on success.

**Returns on success (committed):**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "commit_message": <str>,
    "status": "committed",
    "warning": <str>,  # present only if IN_PROGRESS tasks exist
}
```

**Returns on success (nothing to commit):**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "status": "nothing_to_commit",
    "message": "No changes to commit",
}
```

**Errors:**
- `message` not provided.
- Repo path resolution failure.
- Git error.

---

#### `push_branch`

Pushes the current or specified branch to origin.

**Parameters:**
- `project_id` (required)
- `branch_name` (optional): Branch to push; defaults to the currently checked-out branch.
- `repo_id` (optional)

**Returns on success:**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "branch": <str>,
    "status": "pushed",
}
```

**Errors:**
- Repo path resolution failure.
- Could not determine current branch.
- Git error.

---

#### `merge_branch`

Merges a branch into the repository's default branch.

**Parameters:**
- `project_id` (required)
- `branch_name` (required)
- `repo_id` (optional)

**Behavior:** Uses `repo.default_branch` if available, otherwise `"main"`. Calls `_warn_if_in_progress` regardless of merge outcome.

**Returns on success (merged):**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "branch": <str>,
    "target": <str: default_branch>,
    "status": "merged",
    "warning": <str>,  # present only if IN_PROGRESS tasks exist
}
```

**Returns on success (conflict):**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "branch": <str>,
    "target": <str>,
    "status": "conflict",
    "message": "Merge conflict — merge was aborted",
    "warning": <str>,  # present only if IN_PROGRESS tasks exist
}
```

**Errors:**
- `branch_name` not provided.
- Repo path resolution failure.
- Git error.

---

### Git Status Command

---

#### `get_git_status`

Reports git status across all repos for a project, or falls back to the project workspace if no repos are configured.

**Parameters:**
- `project_id` (required)

**Behavior:** For each repo, determines the checkout path (LINK uses `source_path`; CLONE uses `checkout_base_path`). Validates the path exists and is a git repo. Calls `git.get_current_branch`, `git.get_status`, and `git.get_recent_commits(count=5)`. Does not use `_resolve_repo_path`; performs its own path resolution across all repos in the project.

**Returns on success:**
```python
{
    "project_id": <str>,
    "project_name": <str>,
    "repos": [
        {
            "repo_id": <str>,
            "path": <str>,
            "branch": <str>,
            "status": <str: status output or "(clean)">,
            "recent_commits": <str>,
        },
        ...
    ],
}
```

Per-repo errors are embedded as `{"repo_id": <str>, "error": <str>}` entries rather than aborting the whole command.

**Errors:**
- Project not found.
- Project has no repos and no valid workspace path.
- Project workspace is not a git repository.

---

### Workspace Maintenance Commands

These commands operate across all workspaces for a project and are intended for the chat agent to diagnose and fix workspace issues. All git operations use `asyncio.to_thread(subprocess.run, ...)` or `git._arun()` to avoid blocking the event loop.

---

#### `find_merge_conflict_workspaces`

Scans all workspaces for a project to detect branches with merge conflicts against the default branch without modifying any working tree.

**Parameters:**
- `project_id` (optional): Falls back to `_active_project_id`.

**Behavior:** For each workspace, fetches from origin, then iterates all remote branches. For each branch (excluding the default branch, HEAD, and `dependabot/*`), runs `git merge-base` to find the common ancestor, then `git merge-tree` to simulate a three-way merge. If the merge-tree output contains conflict markers (`+<<<<<<< `), the branch is flagged as conflicting. Also checks for active working tree conflicts via `git status --porcelain` (looking for `UU`, `AA`, `DD` status codes).

All git operations are run via `asyncio.to_thread(subprocess.run, ...)` since they are one-off diagnostic commands not covered by `GitManager` methods.

**Returns on success:**
```python
{
    "project_id": <str>,
    "workspaces_scanned": <int>,
    "workspaces_with_conflicts": <int>,
    "conflicts": [
        {
            "workspace_id": <str>,
            "workspace_name": <str>,
            "workspace_path": <str>,
            "current_branch": <str>,
            "locked_by_task_id": <str | None>,
            "locked_by_agent_id": <str | None>,
            "has_working_tree_conflict": <bool>,
            "branch_conflicts": [
                {
                    "branch": <str>,
                    "task_id": <str>,
                    "conflicting_files": [<str>, ...],
                    "commits_behind_main": <str>,
                },
                ...
            ],
        },
        ...
    ],
}
```

**Errors:**
- `project_id` not provided and no active project set.
- Project not found.
- No workspaces found for the project.

---

---

### Git Low-Level Commands

These commands use `_resolve_repo_path` for path lookup. The first group (`git_commit`, `git_push`, `git_create_branch`, `git_merge`, `git_create_pr`, `git_changed_files`) use `repo_id` as the primary key and require it. The second group (`git_log`, `git_branch`, `git_checkout`, `git_diff`) use `project_id` as the primary key with `repo_id` as an optional filter.

---

#### `git_commit`

Stage all changes and commit in a repository.

**Parameters:**
- `repo_id` (required)
- `message` (required): Commit message.
- `project_id` (optional)

**Returns on success (committed):**
```python
{"repo_id": <str>, "committed": True, "commit_message": <str>}
```

**Returns on success (nothing to commit):**
```python
{"repo_id": <str>, "committed": False, "message": "Nothing to commit — working tree clean"}
```

**Errors:**
- Repo path resolution failure.
- Git error.

---

#### `git_push`

Push a branch to the remote origin.

**Parameters:**
- `repo_id` (required)
- `branch` (optional): Branch to push; defaults to current branch.
- `project_id` (optional)

**Returns on success:**
```python
{"repo_id": <str>, "pushed": <str: branch>}
```

**Errors:**
- Repo path resolution failure.
- Could not determine current branch.
- Git error.

---

#### `git_create_branch`

Create and switch to a new branch.

**Parameters:**
- `repo_id` (required)
- `branch_name` (required)
- `project_id` (optional)

**Returns on success:**
```python
{"repo_id": <str>, "created_branch": <str>}
```

**Errors:**
- Repo path resolution failure.
- Git error.

---

#### `git_merge`

Merge a branch into the default branch.

**Parameters:**
- `repo_id` (required)
- `branch_name` (required): The branch to merge.
- `default_branch` (optional): Target branch; falls back to `repo.default_branch` then `"main"`.
- `project_id` (optional)

**Returns on success (merged):**
```python
{"repo_id": <str>, "merged": True, "branch": <str>, "into": <str>}
```

**Returns on success (conflict):**
```python
{
    "repo_id": <str>,
    "merged": False,
    "into": <str>,
    "message": "Merge conflict — merge of '<branch>' into '<default_branch>' was aborted",
}
```

**Errors:**
- Repo path resolution failure.
- Git error.

---

#### `git_create_pr`

Create a GitHub pull request using the `gh` CLI.

**Parameters:**
- `repo_id` (required)
- `title` (required): PR title.
- `body` (optional, default `""`): PR description body.
- `branch` (optional): Source branch; defaults to current branch.
- `base` (optional): Target branch; defaults to `repo.default_branch` then `"main"`.
- `project_id` (optional)

**Returns on success:**
```python
{"repo_id": <str>, "pr_url": <str>, "branch": <str>, "base": <str>}
```

**Errors:**
- Repo path resolution failure.
- Could not determine current branch.
- Git error (e.g. `gh` CLI not installed, not authenticated).

---

#### `git_changed_files`

List files changed compared to a base branch.

**Parameters:**
- `repo_id` (required)
- `base_branch` (optional): Comparison base; defaults to `repo.default_branch` then `"main"`.
- `project_id` (optional)

**Returns on success:**
```python
{
    "repo_id": <str>,
    "base_branch": <str>,
    "files": [<str>, ...],
    "count": <int>,
}
```

**Errors:**
- Repo path resolution failure.

---

#### `git_log`

Show recent commit history for a repository.

**Parameters:**
- `project_id` (required)
- `count` (optional, default `10`): Number of commits to return.
- `repo_id` (optional)

**Returns on success:**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "branch": <str>,
    "log": <str: formatted commit log or "(no commits)">,
}
```

**Errors:**
- Repo path resolution failure.

---

#### `git_branch`

List branches or create a new branch.

**Parameters:**
- `project_id` (required)
- `name` (optional): If provided, a new branch is created and checked out; otherwise branches are listed.
- `repo_id` (optional)

**Returns on success (list):**
```python
{
    "project_id": <str>,
    "current_branch": <str>,
    "branches": [<str>, ...],
}
```

**Returns on success (create):**
```python
{
    "project_id": <str>,
    "created": <str: branch_name>,
    "message": "Created and switched to branch '<name>'",
}
```

**Errors:**
- Repo path resolution failure.
- Git error (branch creation only).

---

#### `git_checkout`

Switch to an existing branch.

**Parameters:**
- `project_id` (required)
- `branch` (required): Name of the branch to check out.
- `repo_id` (optional)

**Returns on success:**
```python
{
    "project_id": <str>,
    "old_branch": <str>,
    "new_branch": <str>,
    "message": "Switched from '<old>' to '<new>'",
}
```

**Errors:**
- Repo path resolution failure.
- Git error.

---

#### `git_diff`

Show a diff of the working tree or against a base branch.

**Parameters:**
- `project_id` (required)
- `base_branch` (optional): If provided, runs `git.get_diff(path, base_branch)`; otherwise shows the working tree diff (unstaged changes via `git diff`).
- `repo_id` (optional)

**Returns on success:**
```python
{
    "project_id": <str>,
    "repo_id": <str>,
    "base_branch": <str: base or "(working tree)">,
    "diff": <str: diff text or "(no changes)">,
}
```

**Errors:**
- Repo path resolution failure.
- Git error.

---

### Hooks

---

#### `create_hook`

Creates a new hook for a project.

**Parameters:**
- `project_id` (required): Must exist.
- `name` (required): Human-readable hook name. Hook ID is derived by lowercasing and replacing spaces with hyphens.
- `trigger` (required): Dict describing when the hook fires (serialized as JSON).
- `prompt_template` (required): Jinja/string template for the LLM prompt.
- `context_steps` (optional, default `[]`): List of context-gathering steps (serialized as JSON).
- `cooldown_seconds` (optional, default `3600`): Minimum seconds between firings.
- `llm_config` (optional): Dict of LLM configuration overrides (serialized as JSON).

**Returns on success:**
```python
{"created": <str: hook_id>, "name": <str>, "project_id": <str>}
```

**Errors:**
- Project not found.

---

#### `list_hooks`

Lists all hooks, optionally filtered by project.

**Parameters:**
- `project_id` (optional)

**Returns on success:**
```python
{
    "hooks": [
        {
            "id": <str>,
            "project_id": <str>,
            "name": <str>,
            "enabled": <bool>,
            "trigger": <dict: deserialized from JSON>,
            "cooldown_seconds": <int>,
        },
        ...
    ]
}
```

**Errors:** None expected.

---

#### `edit_hook`

Updates one or more fields on an existing hook.

**Parameters:**
- `hook_id` (required)
- `enabled` (optional): Boolean.
- `trigger` (optional): Dict (serialized to JSON).
- `context_steps` (optional): List (serialized to JSON).
- `prompt_template` (optional): String.
- `cooldown_seconds` (optional): Integer.
- `llm_config` (optional): Dict (serialized to JSON).

At least one optional field must be supplied.

**Returns on success:**
```python
{"updated": <str: hook_id>, "fields": [<list of updated field names>]}
```

**Errors:**
- Hook not found.
- No updatable fields provided.

---

#### `delete_hook`

Deletes a hook.

**Parameters:**
- `hook_id` (required)

**Returns on success:**
```python
{"deleted": <str: hook_id>, "name": <str>}
```

**Errors:**
- Hook not found.

---

#### `list_hook_runs`

Returns recent execution records for a hook.

**Parameters:**
- `hook_id` (required)
- `limit` (optional, default `10`): Maximum number of run records to return.

**Returns on success:**
```python
{
    "hook_id": <str>,
    "hook_name": <str>,
    "runs": [
        {
            "id": <str>,
            "trigger_reason": <str>,
            "status": <str>,
            "tokens_used": <int>,
            "skipped_reason": <str | None>,
            "started_at": <str | float>,
            "completed_at": <str | float | None>,
        },
        ...
    ],
}
```

**Errors:**
- Hook not found.

---

#### `fire_hook`

Manually fires a hook immediately (bypasses cooldown).

**Parameters:**
- `hook_id` (required)

**Behavior:** Delegates to `orchestrator.hooks.fire_hook(hook_id)`. The hook runs asynchronously.

**Returns on success:**
```python
{"fired": <str: hook_id>, "status": "running"}
```

**Errors:**
- Hook engine is not enabled (`orchestrator.hooks` is None).
- Hook not found (raised as `ValueError` by the hooks engine).

---

### Notes

Notes are stored as Markdown files in `{workspace_path}/notes/`, where `workspace_path` is resolved from the `workspaces` table via `db.get_project_workspace_path()`. If the project has no workspaces, note commands return an error. Filenames are derived from the title using `git.slugify`.

---

#### `list_notes`

Lists all notes for a project.

**Parameters:**
- `project_id` (required)

**Behavior:** Reads filenames from the `notes/` directory (`.md` files only, sorted alphabetically). For each file, reads the first line to extract a title (if it starts with `# `); otherwise derives the title from the filename.

**Returns on success:**
```python
{
    "project_id": <str>,
    "notes": [
        {
            "name": <str: filename>,
            "title": <str>,
            "size_bytes": <int>,
            "modified": <float: mtime>,
            "path": <str: absolute path>,
        },
        ...
    ],
}
```

If the `notes/` directory does not exist, returns an empty `notes` list (not an error).

**Errors:**
- Project not found.

---

#### `write_note`

Creates or overwrites a note.

**Parameters:**
- `project_id` (required)
- `title` (required): Note title. Slugified for the filename.
- `content` (required): Full Markdown content to write.

**Behavior:** Creates the `notes/` directory if it does not exist. Slugifies the title to form the filename. Writes the content (overwriting any existing file with the same slug).

**Returns on success:**
```python
{
    "path": <str: absolute file path>,
    "title": <str>,
    "status": "created" | "updated",
}
```

**Errors:**
- Project not found.
- Title produces an empty slug after slugification.

---

#### `delete_note`

Deletes a note by title.

**Parameters:**
- `project_id` (required)
- `title` (required): Title of the note to delete. Slugified to find the file.

**Returns on success:**
```python
{"deleted": <str: absolute file path>, "title": <str>}
```

**Errors:**
- Project not found.
- Note not found (file does not exist).

---

#### `read_note`

Reads a note's contents by title without needing the full path.

**Parameters:**
- `project_id` (required)
- `title` (required): Note title. Slugified to resolve the filename.

**Behavior:** Resolves the file path as `{workspace}/notes/{slugify(title)}.md` and reads the full content.

**Returns on success:**
```python
{
    "content": <str: full file content>,
    "title": <str>,
    "path": <str: absolute file path>,
    "size_bytes": <int>,
}
```

**Errors:**
- Project not found.
- Note not found (file does not exist).

---

#### `append_note`

Appends content to an existing note, or creates a new note if one does not exist.

**Parameters:**
- `project_id` (required)
- `title` (required): Note title. Slugified for the filename.
- `content` (required): Content to append.

**Behavior:** Resolves the file path as `{workspace}/notes/{slugify(title)}.md`. If the file exists, appends `\n\n` followed by the new content. If the file does not exist, creates it with `# {title}\n\n{content}`. Creates the `notes/` directory if it does not exist.

**Returns on success:**
```python
{
    "path": <str: absolute file path>,
    "title": <str>,
    "status": "appended" | "created",
    "size_bytes": <int>,
}
```

**Errors:**
- Project not found.
- Title produces an empty slug after slugification.

---

#### `compare_specs_notes`

Lists specs and notes files for gap analysis. Returns raw file listings — no LLM call is made.

**Parameters:**
- `project_id` (required)
- `specs_path` (optional): Override for the specs directory path. If not provided, the command checks for a `specs/` directory in the project's repo first, then falls back to `{workspace}/specs/`.

**Behavior:** Resolves the specs directory (repo `specs/` first, then `{workspace}/specs/`). Lists all `.md` files in both the specs directory and the `{workspace}/notes/` directory, returning titles and sizes for each.

**Returns on success:**
```python
{
    "specs": [{"name": <str>, "title": <str>, "size_bytes": <int>}, ...],
    "notes": [{"name": <str>, "title": <str>, "size_bytes": <int>}, ...],
    "specs_path": <str: absolute path to specs dir>,
    "notes_path": <str: absolute path to notes dir>,
    "project_id": <str>,
}
```

**Errors:**
- Project not found.

---

### System

---

#### `get_recent_events`

Returns recent system events from the event log.

**Parameters:**
- `limit` (optional, default `10`): Number of events to return.

**Returns on success:**
```python
{"events": [<event dicts>, ...]}
```

**Errors:** None expected.

---

#### `get_token_usage`

Returns token usage statistics, scoped to a task, project, or the entire system.

**Parameters:**
- `task_id` (optional): If provided, returns per-agent token breakdown for this task.
- `project_id` (optional): If provided (and `task_id` not given), returns per-task/agent breakdown for this project.
- Neither: Returns per-project totals system-wide.

**Returns on success (task scope):**
```python
{
    "task_id": <str>,
    "breakdown": [{"agent_id": <str>, "tokens": <int>, "entries": <int>}, ...],
    "total": <int>,
}
```

**Returns on success (project scope):**
```python
{
    "project_id": <str>,
    "breakdown": [{"task_id": <str>, "agent_id": <str>, "tokens": <int>}, ...],
    "total": <int>,
}
```

**Returns on success (system scope):**
```python
{
    "breakdown": [{"project_id": <str>, "tokens": <int>}, ...],
    "total": <int>,
}
```

**Errors:** None expected.

---

#### `set_active_project`

Sets or clears the `_active_project_id` on the handler, which is used as a fallback scope for certain commands (e.g. `get_chain_health`).

**Parameters:**
- `project_id` (optional): If not provided or empty, the active project is cleared.

**Returns on success (set):**
```python
{"active_project": <str: project_id>, "name": <str>}
```

**Returns on success (cleared):**
```python
{"active_project": None, "message": "Active project cleared"}
```

**Errors:**
- Project not found (when `project_id` is provided).

---

#### `orchestrator_control`

Pauses, resumes, or checks the status of the orchestrator loop.

**Parameters:**
- `action` (required): One of `"pause"`, `"resume"`, `"status"`.

**Returns on success (pause):**
```python
{"status": "paused", "message": "Orchestrator paused — no new tasks will be scheduled"}
```

**Returns on success (resume):**
```python
{"status": "running", "message": "Orchestrator resumed"}
```

**Returns on success (status):**
```python
{"status": "paused" | "running", "running_tasks": <int>}
```

**Errors:** None expected (unrecognized action falls through to the `status` case).

---

#### `restart_daemon`

Logs a restart notification to the notification channel, then sends `SIGTERM` to the current process, causing the daemon to shut down (and presumably restart via a process manager). Sets `orchestrator._restart_requested = True` before sending the signal.

When `wait_for_tasks` is true and there are running tasks, the orchestrator is paused (no new tasks scheduled) and the command waits up to 5 minutes for running tasks to complete before sending the restart signal.

**Parameters:**
- `reason` (optional, default `"No reason provided"`): Human-readable reason for the restart. Logged to the notification channel as `"🔄 **Daemon restart initiated** — {reason}"`.
- `wait_for_tasks` (optional, default `false`): If true, pause the orchestrator and wait for all running tasks to complete before restarting.

**Returns on success:**
```python
{"status": "restarting", "message": "Daemon restart initiated", "reason": <str>, "waited_for_tasks": <bool>}
```

**Errors:** None expected.

---

#### `update_and_restart`

Pulls the latest source from git and restarts the daemon. Determines the repo root from the source file location. Runs `git pull --ff-only` followed by `pip install -e .` to pick up dependency changes. Both commands are run in a thread via `asyncio.to_thread(subprocess.run, ...)` to avoid blocking the event loop. On success, logs a notification and triggers a restart via `SIGTERM`.

When `wait_for_tasks` is true and there are running tasks, the orchestrator is paused (no new tasks scheduled) and the command waits up to 5 minutes for running tasks to complete before sending the restart signal. The git pull and pip install happen first, so the code is already updated by the time the wait begins.

**Parameters:**
- `reason` (optional, default `"No reason provided"`): Human-readable reason for the update.
- `wait_for_tasks` (optional, default `false`): If true, pause the orchestrator and wait for all running tasks to complete before restarting.

**Returns on success:**
```python
{"status": "updating", "message": "Update pulled and daemon restart initiated", "pull_output": <str>, "reason": <str>, "waited_for_tasks": <bool>}
```

**Errors:**
- `git pull` failed (non-zero exit code).
- `pip install` failed (non-zero exit code).

---

#### `read_file`

Reads a file from within an allowed directory. Supports offset and line limits for reading specific sections of large files. Intended for the chat agent, not Discord slash commands.

**Parameters:**
- `path` (required): File path. If not absolute, it is joined with `config.workspace_dir`.
- `offset` (optional, default `0`): Line number to start reading from (0-based). Lines before this offset are skipped.
- `max_lines` (optional, default `500`): Maximum lines to return. If the file is longer, a truncation notice is appended.

**Behavior:** Resolves the path and validates it via `_validate_path`. Skips `offset` lines, then reads up to `max_lines` lines. Returns total line count in response metadata. Returns an error for binary files (UnicodeDecodeError).

**Returns on success:**
```python
{"content": <str: file contents>, "path": <str: validated absolute path>, "total_lines": <int>}
```

**Errors:**
- Path is outside allowed directories.
- File not found.
- Binary file (cannot display).

---

#### `write_file`

Writes content to a file. Creates the file if it doesn't exist, overwrites if it does. Creates parent directories automatically.

**Parameters:**
- `path` (required): File path. If not absolute, it is joined with `config.workspace_dir`.
- `content` (required): Full file content to write.

**Behavior:** Resolves the path and validates it via `_validate_path`. Writes the content to the file.

**Returns on success:**
```python
{"path": <str: validated absolute path>, "written": <int: number of characters written>}
```

**Errors:**
- Path is outside allowed directories.
- Permission denied.
- Write failed (OS error).

---

#### `edit_file`

Edits a file by replacing an exact string match. Designed for targeted, surgical edits — the `old_string` must appear exactly once in the file unless `replace_all` is set.

**Parameters:**
- `path` (required): File path. If not absolute, it is joined with `config.workspace_dir`.
- `old_string` (required): The exact text to find and replace.
- `new_string` (required): The replacement text.
- `replace_all` (optional, default `false`): If true, replace all occurrences. If false, fails when `old_string` matches multiple locations.

**Behavior:** Validates path, reads the file, counts occurrences of `old_string`. If `replace_all=false` and count != 1, returns an error with the match count. Otherwise performs the replacement and writes the file back.

**Returns on success:**
```python
{"path": <str>, "replacements_made": <int>}
```

**Errors:**
- Path is outside allowed directories.
- File not found.
- `old_string` not found in file.
- Multiple matches found (when `replace_all=false`).

---

#### `glob_files`

Finds files matching a glob pattern within a directory. Uses Python's `pathlib.Path.glob()` with recursive support for `**` patterns.

**Parameters:**
- `pattern` (required): Glob pattern (e.g. `**/*.py`, `src/*.ts`).
- `path` (required): Root directory to search in. If not absolute, joined with `config.workspace_dir`.
- `max_results` (optional, default `100`): Maximum number of results to return.

**Behavior:** Validates the path, runs the glob, sorts results by modification time (newest first), and limits to `max_results`. Returns relative paths from the search root.

**Returns on success:**
```python
{"path": <str>, "pattern": <str>, "matches": [<str>, ...], "count": <int>, "truncated": <bool>}
```

**Errors:**
- Path is outside allowed directories.
- Directory not found.

---

#### `grep`

Searches file contents using regex patterns. More powerful than `search_files` — supports context lines, file type filtering, case-insensitive search, and file-paths-only mode. Uses `rg` (ripgrep) when available, falls back to `grep -rn`.

**Parameters:**
- `pattern` (required): Regex pattern to search for.
- `path` (required): Directory or file to search. If not absolute, joined with `config.workspace_dir`.
- `glob` (optional): File glob filter (e.g. `*.py`, `*.{ts,tsx}`).
- `context_lines` (optional, default `0`): Number of context lines before and after each match.
- `case_insensitive` (optional, default `false`): Case-insensitive search.
- `max_results` (optional, default `50`): Maximum number of matching lines to return.
- `files_only` (optional, default `false`): Only return file paths with matches, not matching lines.

**Behavior:** Validates the path, builds the search command with appropriate flags, runs it via subprocess. Output is truncated to 4000 characters.

**Returns on success:**
```python
{"results": <str: matching lines or file paths>, "pattern": <str>, "path": <str>}
```

**Errors:**
- Path is outside allowed directories.
- Directory not found.
- Search timed out.

---

#### `run_command`

Executes a shell command in a validated working directory. Intended for the chat agent.

**Parameters:**
- `command` (required): Shell command string (executed via `shell=True`).
- `working_dir` (required): Directory to run the command in. If not an absolute path, the handler first tries to look it up as a project ID; if that fails, it joins with `config.workspace_dir`.
- `timeout` (optional, default `30`, max `120`): Execution timeout in seconds.

**Behavior:** Validates the working directory via `_validate_path`. Runs the command in a thread via `asyncio.to_thread(subprocess.run, command, shell=True, ...)` to avoid blocking the event loop. Stdout is truncated to 4000 characters; stderr to 2000 characters.

**Returns on success:**
```python
{
    "returncode": <int>,
    "stdout": <str>,
    "stderr": <str>,
}
```

**Errors:**
- Working directory is outside allowed directories.
- Directory not found.
- Command timed out.

---

#### `search_files`

Searches for patterns in files within a validated directory. Intended for the chat agent.

**Parameters:**
- `pattern` (required): Search pattern (regex for `grep` mode; glob for `find` mode).
- `path` (required): Directory to search. If not absolute, joined with `config.workspace_dir`.
- `mode` (optional, default `"grep"`): Either `"grep"` (recursive regex search, up to 50 matches) or `"find"` (filename glob search).

**Behavior:** Validates the directory via `_validate_path`. Runs the search command in a thread via `asyncio.to_thread(subprocess.run, ...)`. In `grep` mode: `grep -rn --include=* -m 50 <pattern> <path>`. In `find` mode: `find <path> -name <pattern> -type f`. Output is truncated to 4000 characters.

**Returns on success:**
```python
{"results": <str: output or "(no matches)">, "mode": <str>}
```

**Errors:**
- Path is outside allowed directories.
- Directory not found.
- Search timed out.

---

#### `list_directory`

Lists files and directories at a given path within a project workspace. Used by both the chat agent and the `/browse` Discord command.

**Parameters:**
- `project_id` (optional if active project is set): Project whose workspace to browse.
- `workspace` (optional): Workspace name or ID. If omitted, the first workspace for the project is used. Looked up by name first, then by ID.
- `path` (optional, default `""`): Relative path within the workspace to list. Empty string lists the workspace root.

**Behavior:** Resolves the workspace path to an absolute path via `os.path.realpath()` to prevent CWD-relative resolution issues (e.g., if a workspace path was stored as a relative path, it could otherwise resolve relative to the bot's working directory). Joins the relative `path` with the workspace root, validates it via `_validate_path`, then lists the directory contents. Entries are sorted alphabetically and separated into directories and files (with sizes).

**Returns on success:**
```python
{
    "project_id": <str>,
    "path": <str: relative path or "/">,
    "workspace_path": <str: resolved absolute workspace root>,
    "workspace_name": <str>,
    "directories": [<str>, ...],
    "files": [{"name": <str>, "size": <int>}, ...],
}
```

**Errors:**
- `project_id` is required (no active project set).
- Workspace not found for project.
- Project has no workspaces.
- Access denied: path is outside allowed directories.
- Directory not found.
- Permission denied.

---

## 5. Path Validation (`_validate_path`)

```python
async def _validate_path(self, path: str) -> str | None
```

This method is a security gate that ensures file operations cannot escape designated directories.

**Logic:**
1. Resolves the given path to its canonical realpath (resolves symlinks).
2. Resolves `config.workspace_dir` to its canonical realpath.
3. If the resolved path is within (starts with `{workspace_real}/`) or equal to `workspace_real`, the canonical path is returned (allowed).
4. Otherwise, fetches all repos from the database. For each repo that has a `source_path`, resolves that `source_path` to its canonical realpath. If the given path is within or equal to any repo's `source_path`, the canonical path is returned (allowed).
5. If none of the above match, returns `None` (denied).

The caller is responsible for checking whether `None` was returned and returning an access-denied error to the user.

---

## 6. Repo Path Resolution (`_resolve_repo_path`)

```python
async def _resolve_repo_path(self, args: dict) -> tuple[str | None, Project | None, dict | None]
```

Returns a 3-tuple: `(checkout_path, project, error_dict)`. On success, `error_dict` is `None`. On failure, `checkout_path` is `None`. Note: the second element is a `Project`, not a `RepoConfig`.

**Logic (workspace-first resolution):**

1. Reads `project_id`, `repo_id`, and `workspace_id` from `args`.
2. If neither `project_id` nor `repo_id` is provided, returns an error.
3. **Path resolution order:**
   - If `workspace_id` is provided, fetches that specific workspace and uses its path.
   - If `project_id` is provided, fetches the project's first workspace and uses its path.
   - Falls back to legacy repos table lookup if no workspaces exist.
4. Validates that the determined path exists as a directory (error if not).
5. Calls `git.validate_checkout(checkout_path)` to confirm it is a git repository (error if not).
6. Returns `(checkout_path, project, None)`.

**Summary of error conditions:**
- Neither `project_id` nor `repo_id` provided.
- Project not found.
- Repo not found (when `repo_id` specified).
- Repo has no usable path configured.
- No repo found and no project context.
- Project has no repos and no valid workspace.
- Path does not exist on disk.
- Path is not a valid git repository.

---

## 7. In-Progress Warning (`_warn_if_in_progress`)

```python
async def _warn_if_in_progress(self, project_id: str) -> str | None
```

Queries the database for any tasks with status `IN_PROGRESS` for the given project. If any are found, returns a warning string of the form:

```
⚠️ {n} task(s) currently IN_PROGRESS for this project — this operation may disrupt running agent(s).
```

If no tasks are in progress, returns `None`.

This method is called by the high-level git commands `checkout_branch`, `commit_changes`, and `merge_branch` after a successful git operation. The warning is included in the response dict under the `"warning"` key when present. It is never a blocking error — the git operation proceeds regardless.

---

## 8. Tool Navigation Commands

### `browse_tools`

Returns list of tool categories with name, description, and tool_count.
No arguments required.

### `load_tools`

Accepts `category` (string). Returns confirmation of loaded category with
the list of tool names added. The actual tool schema injection happens in
the chat layer (ChatAgent), not in CommandHandler.

Returns error if category is unknown, listing available categories.

### `send_message`

Posts a message to a Discord channel via the bot reference. Accepts
`channel_id` and `content`. Returns error if the Discord bot is not
available or the channel cannot be found.

### Rule System Commands

- `browse_rules` — List rules, optionally filtered by project and scope
- `load_rule` — Load a rule's full content by ID
- `save_rule` — Create or update a rule (markdown with YAML frontmatter)
- `delete_rule` — Delete a rule and its associated hook

See [[specs/rule-system]] for full details.

---

## 9. Undocumented Command Subsystems

> The following command groups exist in the implementation but are not yet fully
> documented in this spec. Each heading lists the `_cmd_*` methods that exist.

### Task Hierarchy & Dependencies
- `get_task_tree` — hierarchical subtask tree view with formatted text
- `task_deps` / `get_task_dependencies` — focused dependency view (upstream/downstream)
- `add_dependency` — add a dependency edge between tasks
- `remove_dependency` — remove a dependency edge

### Task Lifecycle Extensions
- `reopen_with_feedback` — reopen a completed/failed task with appended feedback
- `provide_input` — answer an agent's question (WAITING_INPUT → READY)
- `process_task_completion` — post-completion plan discovery (called by [[specs/supervisor|Supervisor]])
- `process_plan` — manually trigger plan file scanning
- `list_active_tasks_all_projects` — cross-project active task listing

### Task Archive
- `archive_tasks` — bulk archive completed tasks
- `archive_task` — archive a single task
- `list_archived` — list archived tasks
- `restore_task` — restore an archived task
- `archive_settings` — view/update auto-archive settings

### Agent Profile Commands
- `list_profiles` — list agent profiles
- `create_profile` — create an agent profile
- `get_profile` — get profile details
- `edit_profile` — update profile fields
- `delete_profile` — delete a profile
- `list_available_tools` — list known Claude Code tools and MCP servers
- `check_profile` — check profile install manifest
- `install_profile` — install profile dependencies
- `export_profile` / `import_profile` — YAML/gist export/import

### Memory Commands
- `memory_search` — semantic search of project memory
- `memory_stats` — memory index statistics
- `memory_reindex` — force reindex
- `view_profile` — view project profile (synthesized understanding)
- `regenerate_profile` — regenerate project profile via LLM
- `compact_memory` — LLM-powered memory compaction

### Prompt Template Commands
- `list_prompts` — list prompt templates
- `read_prompt` — read a specific template
- `render_prompt` — render template with variable substitution

### Hook Schedule Extensions
- `hook_schedules` — show upcoming hook executions
- `fire_all_scheduled_hooks` — trigger all matching periodic hooks
- `toggle_project_hooks` — enable/disable all hooks in a project
- `schedule_hook` — create a one-shot scheduled hook
- `list_scheduled` / `cancel_scheduled` — manage one-shot hooks

### Note Extensions
- `promote_note` — incorporate a note into the project profile via LLM

### Git Extensions
- `git_pull` — pull (fetch + merge) from remote
- `create_github_repo` — create a GitHub repo via `gh` CLI
- `generate_readme` — generate and commit a README.md
- `set_default_branch` — set/change a project's default branch

### System Extensions
- `reload_config` — manual config hot-reload
- `claude_usage` — Claude Code usage stats from session data
- `shutdown` — graceful/force shutdown

### Deprecated Agent Commands (return error stubs)
- `edit_agent`, `pause_agent`, `resume_agent`, `delete_agent`

### Deprecated Analyzer Commands (return error stubs)
- `analyzer_status`, `analyzer_toggle`, `analyzer_history`
