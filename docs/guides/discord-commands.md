---
tags: [discord, commands, interface]
---

# Discord Commands

Agent Queue gives you two ways to interact through Discord. See [[specs/messaging/discord|Discord spec]] and [[specs/command-handler|CommandHandler spec]] for implementation details.

| Method | How it works | Best for |
|--------|-------------|----------|
| **Slash commands** | Type `/` and pick from the autocomplete menu | Structured operations with clear parameters |
| **Natural language chat** | Just type a message in the bot's channel | Quick requests, complex descriptions, multi-step workflows |

Both methods call the same underlying [[specs/command-handler|CommandHandler]] logic — slash commands are just a structured shortcut for what you can always ask the [[specs/supervisor|Supervisor]] to do in plain English.

**Example — creating a task both ways:**

```
Slash:  /add-task description: Fix the login timeout bug in auth.py

Chat:   hey, can you add a task to fix the login timeout bug in auth.py?
        the error is on line 47, JWT expires too early
```

The chat approach lets you include richer context naturally.

---

## Getting Oriented

These commands give you a quick picture of what's happening across your system.

| Command | Description |
|---------|-------------|
| `/status` | System overview — active/ready/completed task counts, agent states, queued work |
| `/projects` | List all projects with their status, credit weight, and linked channels |
| `/agents` | List all agents and what they're currently working on |
| `/usage` | Show Claude Code usage — active sessions, tokens, rate limits |
| `/events` | Recent system events (task completions, failures, etc.) |
| `/menu` | Show an interactive control panel with clickable buttons |

### `/status`

Shows a dashboard-style overview: task counts by state, each agent's current assignment, and the next tasks in queue. This is usually the first command you run.

### `/events`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `limit` | No | Number of events to show (default: 10) |

### `/menu`

Opens an interactive embed with buttons for common operations — a quick way to navigate without remembering command names.

---

## Managing Projects

### Creating and editing

| Command | Description |
|---------|-------------|
| `/new-project` | Create a new project with an interactive wizard |
| `/edit-project` | Change a project's name, weight, max agents, budget, or channel |
| `/delete-project` | Delete a project and all its data |
| `/set-default-branch` | Set the default branch for a project (creates it if needed) |

#### `/new-project`

Opens an interactive wizard modal to walk you through project creation. No parameters needed — everything is configured through the wizard UI.

#### `/edit-project`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | Yes | Project ID |
| `name` | No | New name |
| `credit_weight` | No | New scheduling weight |
| `max_concurrent_agents` | No | New max agents |
| `budget_limit` | No | Token budget limit (0 to clear) |
| `channel` | No | Discord channel to link to this project |

#### `/delete-project`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | Yes | Project ID to delete |
| `archive_channels` | No | Archive the project's Discord channels instead of leaving them |

Deletion removes all associated tasks, workspaces, results, and token records. Cannot delete a project with tasks currently in progress.

#### `/set-default-branch`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | Yes | Project ID |
| `branch` | Yes | Branch name to use as default (e.g. dev, main, master) |

### Pausing and resuming

| Command | Description |
|---------|-------------|
| `/pause` | Pause a project — no new tasks will be scheduled |
| `/resume` | Resume a paused project |

Both commands auto-detect the project from the channel you're in.

### Setting the active project

| Command | Description |
|---------|-------------|
| `/set-project` | Set or clear the active project for the Supervisor |

When an active project is set, commands that need a project will default to it. Leave `project_id` empty to clear.

### Channel setup

Each project can have a dedicated Discord channel. Commands run in that channel automatically target the right project — no need to specify `project_id` every time.

| Command | Description |
|---------|-------------|
| `/create-channel` | Create a new Discord channel and link it to a project |
| `/channel-map` | Show all project-to-channel mappings |

#### `/create-channel`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | Yes | Project ID |
| `channel_name` | No | Name for the new channel (defaults to project ID) |
| `category` | No | Category to create the channel in |

---

## Working with Tasks

### Creating tasks

#### `/add-task`

The quickest way to create a task from Discord.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `description` | Yes | What the task should do |

The project is auto-detected from the channel, or falls back to the active project. Returns an error if no project can be resolved.

**Via chat** — you can provide much richer context:

```
Create a task for my-app to refactor the database connection pooling.
Currently it creates a new connection per request which is causing
timeouts under load. Use SQLAlchemy's built-in pool with max 20
connections. The relevant code is in src/db/connection.py.
```

### Monitoring tasks

| Command | Description |
|---------|-------------|
| `/tasks` | List tasks grouped by status, with interactive expand/collapse |
| `/active-tasks` | List active tasks across ALL projects |
| `/task` | Show full details of a specific task |
| `/task-result` | View a task's output: summary, files changed, tokens used |
| `/task-diff` | Show the git diff for a task's branch |
| `/task-deps` | Show dependency graph for a task (what it needs and blocks) |
| `/agent-error` | Inspect the last error for a failed task |
| `/chain-health` | Check dependency chains for stuck tasks |

#### `/tasks`

Auto-detects the project from the channel. Returns an interactive view with collapsible status sections and a dropdown to inspect individual tasks.

#### `/active-tasks`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `show_completed` | No | Include completed/failed/blocked tasks (default: hide) |

Shows active tasks across all projects — useful for a global view.

#### `/task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID (e.g., `bold-falcon`) |

Shows title, status, project, priority, assigned agent, retry count, and description.

#### `/task-result`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Shows the agent's summary, list of files changed, token usage, and any error messages.

#### `/task-diff`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Shows the git diff of the task's branch against the base branch. Large diffs are attached as `.patch` files.

#### `/task-deps`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID to inspect |

Shows the dependency graph for a task — what it depends on and what depends on it.

#### `/agent-error`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Shows error classification, error detail, suggested fix, and the agent's summary of what went wrong.

#### `/chain-health`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | No | Check a specific blocked task's downstream |

Auto-detects the project from the channel if no task is specified. Use this when tasks seem stuck in DEFINED — it reveals which blocked or failed task is holding up the chain.

### Controlling tasks

| Command | Description |
|---------|-------------|
| `/edit-task` | Change a task's title, description, priority, type, status, and more |
| `/stop-task` | Cancel an in-progress task (marks it BLOCKED) |
| `/restart-task` | Reset a completed/failed/blocked task back to READY |
| `/reopen-with-feedback` | Reopen a completed/failed task with feedback for rework |
| `/delete-task` | Delete a task (cannot delete in-progress tasks) |
| `/approve-task` | Approve a task that's AWAITING_APPROVAL |
| `/skip-task` | Skip a blocked/failed task to unblock its dependents |

#### `/edit-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |
| `title` | No | New title |
| `description` | No | New description |
| `priority` | No | New priority (lower number = higher priority) |
| `task_type` | No | New task type: feature, bugfix, refactor, test, docs, chore, research, plan |
| `status` | No | Admin status override: DEFINED, READY, IN_PROGRESS, COMPLETED, FAILED, BLOCKED |
| `max_retries` | No | Max retry attempts |
| `verification_type` | No | How to verify output: auto_test, qa_agent, human |

#### `/stop-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Cancels the agent working on the task and marks it as BLOCKED.

#### `/restart-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Resets the task to READY so it gets picked up by an agent again. Works on completed, failed, or blocked tasks.

#### `/reopen-with-feedback`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID to reopen |
| `feedback` | Yes | QA feedback explaining what went wrong or needs fixing |

Reopens a completed or failed task and attaches feedback so the agent knows what to fix on the next attempt.

#### `/delete-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID to delete |

Permanently deletes a task. Cannot delete tasks that are currently in progress.

#### `/approve-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

For tasks in AWAITING_APPROVAL status — marks them as COMPLETED so downstream dependents can proceed.

#### `/skip-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID |

Marks a BLOCKED or FAILED task as COMPLETED without actually doing the work. Use this to unblock dependency chains when a task is no longer needed. Shows how many downstream tasks were unblocked.

### Task Archiving

| Command | Description |
|---------|-------------|
| `/archive-tasks` | Archive completed tasks (DB + markdown notes in workspace) |
| `/archive-task` | Archive a single completed/failed/blocked task |
| `/list-archived` | View archived tasks |
| `/restore-task` | Restore an archived task back to active status |
| `/archive-settings` | View auto-archive configuration and status |

#### `/archive-tasks`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | No | Project to archive completed tasks from (omit for all projects) |
| `include_failed` | No | Also archive FAILED and BLOCKED tasks (default: false) |

#### `/archive-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Task ID to archive |

#### `/list-archived`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project_id` | No | Filter by project |
| `limit` | No | Max tasks to show (default: 25) |

#### `/restore-task`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `task_id` | Yes | Archived task ID to restore |

### Task Dependencies

Dependencies control execution order — a task in DEFINED state won't promote to READY until all its dependencies are COMPLETED. The orchestrator checks dependencies every cycle (~5 seconds).

| Command | Description |
|---------|-------------|
| `/chain-health` | Diagnose stuck dependency chains |
| `/task-deps` | Show what a task depends on and what depends on it |
| `/skip-task` | Skip a blocked task to unblock its dependents |

#### Dependency workflow: skip to unblock

When a task in a dependency chain fails or gets blocked, all downstream tasks stay stuck in DEFINED. You have two options:

1. **Fix and retry** — Use `/restart-task` to re-queue the blocked task
2. **Skip and unblock** — Use `/skip-task` to mark it COMPLETED without doing the work, which unblocks all downstream dependents

The `/chain-health` command helps you diagnose these situations by showing which blocked or failed task is holding up the chain and how many downstream tasks are affected.

```
Example workflow:
  task-A (BLOCKED) → task-B (DEFINED) → task-C (DEFINED)

  /skip-task task_id: task-A
  ✓ Skipped task-A. 1 downstream task unblocked.

  Next cycle: task-B promotes to READY → gets assigned → starts working
  When task-B completes: task-C promotes to READY
```

---

## Agents

| Command | Description |
|---------|-------------|
| `/agents` | List all agents and their states |
| `/create-agent` | Register a new agent |
| `/edit-agent` | Edit an agent's properties |
| `/delete-agent` | Delete an agent and its workspace mappings |
| `/pause-agent` | Pause an agent so it stops receiving new tasks |
| `/resume-agent` | Resume a paused agent so it can receive tasks again |

#### `/create-agent`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | No | Agent display name (leave empty for auto-generated creative name) |

#### `/edit-agent`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agent_id` | Yes | Agent ID |
| `name` | No | New display name |
| `agent_type` | No | New agent type: `claude`, `codex`, `cursor`, or `aider` |

#### `/delete-agent`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agent_id` | Yes | Agent ID to delete |

#### `/pause-agent`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agent_id` | Yes | Agent ID to pause |

#### `/resume-agent`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `agent_id` | Yes | Agent ID to resume |

---

## Workspaces

Workspaces are directories where agents do their work. Each project can have multiple workspaces.

| Command | Description |
|---------|-------------|
| `/workspaces` | List project workspaces |
| `/add-workspace` | Add a workspace directory for a project |
| `/remove-workspace` | Delete a workspace from a project (must not be locked) |
| `/release-workspace` | Force-release a stuck workspace lock |
| `/sync-workspaces` | Sync all project workspaces to the latest main branch |

#### `/add-workspace`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `source` | Yes | How to set up the workspace: `clone` or `link` |
| `path` | No | Directory path (required for `link`, auto-generated for `clone`) |
| `name` | No | Workspace name |

#### `/remove-workspace`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `workspace_id` | Yes | Workspace ID or name to delete |

#### `/release-workspace`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `workspace_id` | Yes | Workspace ID to release |

---

## Git Operations

All git commands auto-detect the project from the channel and default to the project's first workspace. You can override with the `workspace` parameter.

### Browsing

| Command | Description |
|---------|-------------|
| `/git-status` | Current branch, working tree status, recent commits |
| `/git-branches` | List branches, or create a new one by providing `name` |
| `/git-log` | Show recent commits |
| `/git-diff` | Show working tree diff, or diff against a base branch |
| `/git-files` | List files changed compared to a base branch |

#### `/git-log`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `count` | No | Number of commits to show (default: 10) |
| `workspace` | No | Workspace ID or name (defaults to first workspace) |

#### `/git-diff`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `base_branch` | No | Branch to diff against (omit for working tree diff) |
| `workspace` | No | Workspace ID or name (defaults to first workspace) |

#### `/git-files`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `base_branch` | No | Branch to compare against (defaults to repo default) |
| `workspace` | No | Workspace ID or name (defaults to first workspace) |

### Branching

| Command | Description | Aliases |
|---------|-------------|---------|
| `/create-branch` | Create and switch to a new branch | `/git-branch`, `/project-create-branch` |
| `/git-checkout` | Switch to an existing branch | `/checkout-branch` |

All accept `branch_name` (required) and optional `workspace`.

### Committing and pushing

| Command | Description | Aliases |
|---------|-------------|---------|
| `/commit` | Stage all changes and commit | `/project-commit`, `/git-commit` |
| `/push` | Push a branch to the remote | `/project-push`, `/git-push` |
| `/merge` | Merge a branch into the default branch | `/project-merge`, `/git-merge` |
| `/git-pull` | Pull (fetch + merge) from remote origin | — |
| `/git-pr` | Create a GitHub pull request | — |

#### `/commit`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `message` | Yes | Commit message |
| `workspace` | No | Workspace ID or name (defaults to first workspace) |

#### `/push`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `branch_name` | No | Branch to push (defaults to current branch) |
| `workspace` | No | Workspace ID or name (defaults to first workspace) |

#### `/git-pull`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `branch` | No | Branch name to pull (defaults to current branch) |
| `workspace` | No | Workspace ID or name (defaults to first workspace) |

#### `/merge`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `branch_name` | Yes | Branch to merge |
| `workspace` | No | Workspace ID or name (defaults to first workspace) |

Note: The `/git-merge` alias also accepts a `default_branch` parameter to override the target branch.

#### `/git-pr`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | Yes | PR title |
| `body` | No | PR description |
| `branch` | No | Head branch (defaults to current) |
| `base` | No | Base branch (defaults to repo default) |
| `workspace` | No | Workspace ID or name (defaults to first workspace) |

Requires the `gh` CLI to be authenticated on the host machine.

---

## File Browser & Editor

| Command | Description |
|---------|-------------|
| `/browse` | Browse project repository files and directories interactively |
| `/edit-file` | Open a text editor dialog for any file in the project |

#### `/browse`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `path` | No | Subdirectory to start browsing from (default: root) |
| `workspace` | No | Workspace name or ID to browse (default: first workspace) |

Shows an interactive embed with directory navigation via dropdown menus, file viewing, parent directory button, and pagination for large directories.

#### `/edit-file`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `path` | Yes | Relative file path within the project workspace (e.g. `src/main.py`) |

Opens a Discord modal with the file's current content pre-filled. Edit and submit to save, or dismiss to discard.

---

## Automation (Hooks)

Hooks run automated workflows — they trigger on events or schedules, gather context, and send prompts to an LLM.

| Command | Description |
|---------|-------------|
| `/hooks` | List configured hooks |
| `/create-hook` | Create an automation hook (interactive wizard) |
| `/add-hook` | Alias for `/create-hook` |
| `/edit-hook` | Edit a hook's settings (name, enable/disable, prompt, cooldown, token limit) |
| `/delete-hook` | Delete a hook and its run history |
| `/hook-runs` | Show recent execution history for a hook |
| `/fire-hook` | Manually trigger a hook immediately, ignoring cooldown |

#### `/create-hook`

Opens an interactive wizard modal to configure the hook. No inline parameters — everything is set through the wizard UI.

**Via chat** — managing hooks is often easier through natural language:

```
Create a hook for my-app that runs every 30 minutes
and checks if there are any failing tests. If tests
are failing, create a task to fix them.
```

```
Disable the test-monitor hook
```

```
Show me the last 5 runs of hook deploy-checker
```

#### `/edit-hook`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `hook_id` | Yes | Hook ID |
| `name` | No | New hook name |
| `enabled` | No | Enable or disable the hook |
| `prompt_template` | No | New prompt template |
| `cooldown_seconds` | No | New cooldown in seconds |
| `max_tokens_per_run` | No | Max tokens per run (0 to clear) |

#### `/hook-runs`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `hook_id` | Yes | Hook ID |
| `limit` | No | Number of runs to show (default: 10) |

---

## Project Notes

Notes are markdown documents stored per-project.

| Command | Description |
|---------|-------------|
| `/notes` | List notes for a project (opens an interactive thread) |
| `/write-note` | Create or update a project note |
| `/delete-note` | Delete a project note |

#### `/notes`

Auto-detects the project from the channel. Lists all notes and opens a thread where you can ask the bot to read, create, or edit notes interactively.

#### `/write-note`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | Yes | Note title |
| `content` | Yes | Note content (markdown) |

#### `/delete-note`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `title` | Yes | Note title |

---

## Memory

The memory system provides semantic search across project knowledge — task results, notes, and knowledge-base entries indexed by a vector database.

| Command | Description |
|---------|-------------|
| `/memory-stats` | Show memory index configuration and status for a project |
| `/memory-search` | Semantic search across a project's indexed memories |

#### `/memory-stats`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `project` | No | Project ID (auto-detected from channel) |

Shows whether memory is enabled, the embedding provider, Milvus collection name, and auto-recall/remember settings. If memory is not enabled, shows instructions to enable it.

#### `/memory-search`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `query` | Yes | Semantic search query |
| `project` | No | Project ID (auto-detected from channel) |
| `top_k` | No | Number of results to return (default: 5) |

Returns ranked results with source file, relevance score, heading, and a content preview. Uses the project's configured embedding provider for semantic matching.

---

## System Control

| Command | Description |
|---------|-------------|
| `/orchestrator` | Pause, resume, or check status of task scheduling |
| `/restart` | Restart the agent-queue daemon (brief disconnect) |
| `/shutdown` | Shut down the bot and all running agents |
| `/update` | Pull latest source, install deps, and restart the daemon |
| `/clear` | Clear messages from the current channel |

#### `/orchestrator`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `action` | Yes | `pause`, `resume`, or `status` |

When paused, no new tasks will be assigned to agents. Running tasks continue to completion.

#### `/restart`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `reason` | Yes | Why are you restarting? |

#### `/shutdown`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `reason` | Yes | Why are you shutting down? |
| `force` | No | Force-stop all running agents immediately (default: graceful) |

#### `/update`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `reason` | No | Why are you updating? (auto-filled if omitted) |

#### `/clear`

| Parameter | Required | Description |
|-----------|----------|-------------|
| `count` | No | Number of messages to delete (default: all, max: 1000) |

---

## Chat-Only Capabilities

These features are only available through natural language chat:

### File operations

```
Read the file src/auth.py in my-app
Search for "def connect" in the my-app workspace
Find all .yaml files in the config directory
```

### Shell commands

```
Run "pytest tests/" in my-app
Run "npm test" in the frontend project
```

Commands run with a 30-second default timeout (max 120 seconds).

### Token usage

```
How many tokens has my-app used?
What's the token breakdown for task bold-falcon?
```

---

## Tips & Tricks

**Channel context saves typing.** Run commands from a project's dedicated channel and you won't need to specify `project_id` on every command. Use `/channel-map` to see which channels are linked.

**Task IDs are human-readable.** Tasks get names like `bold-falcon` or `quiet-owl` — easy to remember and type. Use them with any task command.

**Dependency chains.** When an agent produces a plan file, the orchestrator automatically creates chained subtasks with dependencies. Use `/chain-health` to diagnose stuck chains and `/skip-task` to unblock them.

**Just ask.** If you're not sure which command to use, just describe what you want in plain English. The Supervisor has access to all the same operations as slash commands, plus file reading, shell commands, and more.

**Git command aliases.** Many git commands have multiple names: `/commit` and `/project-commit` and `/git-commit` all do the same thing. Use whichever feels natural.

**Task archiving.** Use `/archive-tasks` to clean up completed tasks and keep your task list focused. Archived tasks are preserved in markdown notes and can be restored with `/restore-task`.

**Interactive controls.** Use `/menu` for a button-based control panel, or `/browse` to navigate files with clickable dropdowns instead of typing paths.
