"""Tiered tool registry for on-demand tool loading.

Splits the monolithic TOOLS list into core tools (always loaded) and
named categories (loaded on demand via browse_tools/load_tools).

The registry only manages tool *definitions* (JSON Schema dicts).
Execution still flows through CommandHandler.execute() regardless
of whether a tool is "loaded" in the LLM's context.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryMeta:
    """Metadata for a tool category."""
    name: str
    description: str


# Category definitions with human-readable descriptions
CATEGORIES: dict[str, CategoryMeta] = {
    "git": CategoryMeta(
        name="git",
        description=(
            "Branch, commit, push, PR, and merge operations "
            "for project repositories"
        ),
    ),
    "project": CategoryMeta(
        name="project",
        description=(
            "Project CRUD, workspace management, channel configuration"
        ),
    ),
    "agent": CategoryMeta(
        name="agent",
        description=(
            "Agent management, agent profiles, profile import/export"
        ),
    ),
    "hooks": CategoryMeta(
        name="hooks",
        description=(
            "Direct hook management -- create, edit, list, delete, "
            "fire hooks"
        ),
    ),
    "memory": CategoryMeta(
        name="memory",
        description=(
            "Memory operations beyond search -- notes, project profiles, "
            "compaction, reindexing"
        ),
    ),
    "files": CategoryMeta(
        name="files",
        description=(
            "Filesystem tools — read, write, edit files, glob pattern "
            "matching, grep/ripgrep-style content search"
        ),
    ),
    "system": CategoryMeta(
        name="system",
        description=(
            "Token usage, config, diagnostics, advanced task operations "
            "(archive, approve, dependencies), prompt management, "
            "daemon control"
        ),
    ),
}

# Which category each tool belongs to.
# Tools not listed here are "core" (always loaded).
_TOOL_CATEGORIES: dict[str, str] = {
    # git
    "get_git_status": "git",
    "git_commit": "git",
    "git_pull": "git",
    "git_push": "git",
    "git_create_branch": "git",
    "git_merge": "git",
    "git_create_pr": "git",
    "git_changed_files": "git",
    "git_log": "git",
    "git_diff": "git",
    "checkout_branch": "git",
    # project
    "list_projects": "project",
    "create_project": "project",
    "pause_project": "project",
    "resume_project": "project",
    "edit_project": "project",
    "set_default_branch": "project",
    "get_project_channels": "project",
    "get_project_for_channel": "project",
    "delete_project": "project",
    "add_workspace": "project",
    "list_workspaces": "project",
    "find_merge_conflict_workspaces": "project",
    "release_workspace": "project",
    "remove_workspace": "project",
    "sync_workspaces": "project",
    "set_active_project": "project",
    # agent
    "list_agents": "agent",
    "create_agent": "agent",
    "edit_agent": "agent",
    "pause_agent": "agent",
    "resume_agent": "agent",
    "delete_agent": "agent",
    "get_agent_error": "agent",
    "list_profiles": "agent",
    "create_profile": "agent",
    "get_profile": "agent",
    "edit_profile": "agent",
    "delete_profile": "agent",
    "list_available_tools": "agent",
    "check_profile": "agent",
    "install_profile": "agent",
    "export_profile": "agent",
    "import_profile": "agent",
    # hooks
    "create_hook": "hooks",
    "list_hooks": "hooks",
    "edit_hook": "hooks",
    "delete_hook": "hooks",
    "list_hook_runs": "hooks",
    "fire_hook": "hooks",
    "hook_schedules": "hooks",
    "fire_all_scheduled_hooks": "hooks",
    "schedule_hook": "hooks",
    "list_scheduled": "hooks",
    "cancel_scheduled": "hooks",
    # memory
    "memory_stats": "memory",
    "memory_reindex": "memory",
    "view_profile": "memory",
    "regenerate_profile": "memory",
    "compact_memory": "memory",
    "list_notes": "memory",
    "write_note": "memory",
    "delete_note": "memory",
    "read_note": "memory",
    "append_note": "memory",
    "promote_note": "memory",
    "compare_specs_notes": "memory",
    # files
    "read_file": "files",
    "write_file": "files",
    "edit_file": "files",
    "glob_files": "files",
    "grep": "files",
    "search_files": "files",
    "list_directory": "files",
    # system
    "get_token_usage": "system",
    "run_command": "system",
    "get_status": "system",
    "get_recent_events": "system",
    "get_task_result": "system",
    "get_task_diff": "system",
    "list_active_tasks_all_projects": "system",
    "get_task_tree": "system",
    "stop_task": "system",
    "restart_task": "system",
    "reopen_with_feedback": "system",
    "delete_task": "system",
    "archive_tasks": "system",
    "archive_task": "system",
    "list_archived": "system",
    "restore_task": "system",
    "approve_task": "system",
    "process_task_completion": "system",
    "approve_plan": "system",
    "reject_plan": "system",
    "delete_plan": "system",
    "process_plan": "system",
    "skip_task": "system",
    "get_task_dependencies": "system",
    "add_dependency": "system",
    "remove_dependency": "system",
    "get_chain_health": "system",
    "restart_daemon": "system",
    "orchestrator_control": "system",
    "list_prompts": "system",
    "read_prompt": "system",
    "render_prompt": "system",
    # analyzer_status, analyzer_toggle, analyzer_history: deprecated (Phase 6)
    # plugins
    "plugin_list": "system",
    "plugin_info": "system",
    "plugin_install": "system",
    "plugin_update": "system",
    "plugin_remove": "system",
    "plugin_enable": "system",
    "plugin_disable": "system",
    "plugin_reload": "system",
    "plugin_config": "system",
    "plugin_prompts": "system",
    "plugin_reset_prompts": "system",
}



_ALL_TOOL_DEFINITIONS = [
    {
        "name": "list_projects",
        "description": "List all projects in the system.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_project",
        "description": (
            "Create a new project.  Optionally auto-create a dedicated Discord "
            "channel for the project.  When "
            "auto_create_channels is omitted the behaviour is determined by "
            "the per_project_channels.auto_create config flag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "credit_weight": {
                    "type": "number",
                    "description": "Scheduling weight (default 1.0)",
                    "default": 1.0,
                },
                "max_concurrent_agents": {
                    "type": "integer",
                    "description": "Max agents working on this project simultaneously",
                    "default": 2,
                },
                "repo_url": {
                    "type": "string",
                    "description": "Git repository URL for this project (optional)",
                },
                "default_branch": {
                    "type": "string",
                    "description": "Default branch name (default: main)",
                    "default": "main",
                },
                "auto_create_channels": {
                    "type": "boolean",
                    "description": (
                        "If true, auto-create dedicated Discord channels for "
                        "this project after creation.  If false, skip channel "
                        "creation.  When omitted, falls back to the global "
                        "per_project_channels.auto_create config setting."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "pause_project",
        "description": "Pause a project so no new tasks are scheduled.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to pause"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "resume_project",
        "description": "Resume a paused project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to resume"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "edit_project",
        "description": (
            "Edit a project's properties: name, credit_weight, max_concurrent_agents, "
            "budget_limit, discord_channel_id, default_profile_id, or repo_default_branch. "
            "Use this to rename projects, adjust scheduling weight, set token budgets, "
            "link Discord channels, set a default agent profile, or change the default git branch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {"type": "string", "description": "New project name (optional)"},
                "credit_weight": {"type": "number", "description": "New scheduling weight (optional)"},
                "max_concurrent_agents": {"type": "integer", "description": "New max concurrent agents (optional)"},
                "budget_limit": {"type": ["integer", "null"], "description": "Token budget limit (optional, null to clear)"},
                "discord_channel_id": {"type": ["string", "null"], "description": "Discord channel ID to link (optional, null to unlink)"},
                "default_profile_id": {"type": ["string", "null"], "description": "Default agent profile ID for tasks in this project (optional, null to clear)"},
                "repo_default_branch": {"type": "string", "description": "Default git branch for the project (e.g. main, dev, master)"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "set_default_branch",
        "description": (
            "Set (or change) the default git branch for a project. If the branch "
            "does not exist on the remote yet, it will be created automatically "
            "from the current default branch. Use this when a project should branch "
            "off of and merge into a branch other than 'main' (e.g. 'dev')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "branch": {"type": "string", "description": "Branch name to use as the new default (e.g. dev, main, master)"},
            },
            "required": ["project_id", "branch"],
        },
    },
    # Note: set_project_channel and set_control_interface have been removed.
    # Use edit_project with discord_channel_id instead.
    {
        "name": "get_project_channels",
        "description": "Get the Discord channel ID configured for a project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_project_for_channel",
        "description": (
            "Reverse lookup: given a Discord channel ID, find which project it belongs to. "
            "Returns the project ID, or null if no project is linked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID to look up",
                },
            },
            "required": ["channel_id"],
        },
    },
    {
        "name": "list_tasks",
        "description": (
            "List tasks, optionally filtered by project or status. "
            "IMPORTANT: By default, completed/failed/blocked tasks are HIDDEN "
            "and only active tasks are returned. When presenting results from "
            "the default filter, say 'N active tasks' (not just 'N tasks'). "
            "Use show_all=true or include_completed=true to include finished "
            "tasks. Use completed_only=true to see only finished tasks. "
            "An explicit status filter overrides all convenience flags. "
            "Supports three display modes: 'flat' (default list), 'tree' "
            "(hierarchical view with subtasks), and 'compact' (root tasks "
            "with progress bars). Tree and compact modes require project_id. "
            "Use show_dependencies=true to annotate each task with its upstream "
            "depends_on and downstream blocks relationships — useful for "
            "understanding blocking chains."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Filter by project ID (optional, but required for tree/compact modes)",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by exact status: DEFINED, READY, IN_PROGRESS, "
                        "COMPLETED, etc. When provided, show_all, "
                        "include_completed, and completed_only are ignored."
                    ),
                },
                "show_all": {
                    "type": "boolean",
                    "description": (
                        "When true, return ALL tasks regardless of status "
                        "(active + completed + failed + blocked). "
                        "Default false (only active tasks are shown)."
                    ),
                },
                "include_completed": {
                    "type": "boolean",
                    "description": (
                        "When true, return all tasks including completed/failed/"
                        "blocked. Alias for show_all. Default false."
                    ),
                },
                "completed_only": {
                    "type": "boolean",
                    "description": (
                        "When true, return ONLY completed/failed/blocked tasks. "
                        "Default false."
                    ),
                },
                "display_mode": {
                    "type": "string",
                    "enum": ["flat", "tree", "compact"],
                    "description": (
                        "How to format the task list. 'flat' (default) returns "
                        "a JSON array of task objects. 'tree' returns a "
                        "hierarchical view using box-drawing characters showing "
                        "parent/subtask relationships — ideal for plans with "
                        "subtasks. 'compact' returns each root task with a "
                        "progress bar summarizing subtask completion. When "
                        "display_mode is 'tree' or 'compact', the response "
                        "includes a 'display' field with pre-formatted text."
                    ),
                },
                "show_dependencies": {
                    "type": "boolean",
                    "description": (
                        "When true, each task in the result includes "
                        "'depends_on' (list of upstream tasks with id and "
                        "status) and 'blocks' (list of downstream task IDs). "
                        "Also adds a 'dependency_display' field with "
                        "pre-formatted text showing dependency relationships. "
                        "Use this when the user asks about task dependencies, "
                        "blocking chains, or why a task is waiting. "
                        "Default false."
                    ),
                },
            },
        },
    },
    {
        "name": "list_active_tasks_all_projects",
        "description": (
            "List active tasks across ALL projects, grouped by project. "
            "Returns only non-terminal tasks (excludes COMPLETED, FAILED, "
            "BLOCKED) by default. Use this when the user wants a cross-project "
            "overview of everything that is queued, in-progress, or actionable. "
            "Response includes 'by_project' (grouped), 'tasks' (flat list), "
            "'total', 'project_count', and 'hidden_completed' (number of "
            "terminal tasks not shown). When presenting results, say "
            "'N active tasks across M projects'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_completed": {
                    "type": "boolean",
                    "description": (
                        "When true, include completed/failed/blocked tasks too. "
                        "Default false (active tasks only)."
                    ),
                },
            },
        },
    },
    {
        "name": "get_task_tree",
        "description": (
            "Get the subtask hierarchy for a specific parent task, rendered as "
            "a tree with box-drawing characters. Returns a 'display' field with "
            "pre-formatted text showing the full parent->child hierarchy, status "
            "emojis, and a progress summary. Use this when the user asks about "
            "subtasks of a specific task or wants to inspect a plan's structure. "
            "For a project-wide tree view, use list_tasks with "
            "display_mode='tree' instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The root/parent task ID whose subtree to display",
                },
                "compact": {
                    "type": "boolean",
                    "description": (
                        "When true, show only the root task with a subtask "
                        "count and progress bar instead of the full expanded "
                        "tree. Default false (full tree)."
                    ),
                },
                "max_depth": {
                    "type": "integer",
                    "description": (
                        "Maximum nesting depth to render (default 4). "
                        "Deeper subtasks are collapsed into a summary."
                    ),
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "create_task",
        "description": "Create a new task. If no project_id is given, it inherits from the active project; errors if none is set.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (optional — inferred from active project)",
                },
                "title": {"type": "string", "description": "Short task title"},
                "description": {
                    "type": "string",
                    "description": "Detailed task description for the agent",
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority (lower = higher priority, default 100)",
                    "default": 100,
                },
                "requires_approval": {
                    "type": "boolean",
                    "description": "If true, agent work creates a PR instead of auto-merging. Human must approve/merge the PR.",
                    "default": False,
                },
                "task_type": {
                    "type": "string",
                    "enum": ["feature", "bugfix", "refactor", "test", "docs", "chore", "research", "plan"],
                    "description": "Categorize the task type for display and filtering (optional)",
                },
                "profile_id": {
                    "type": "string",
                    "description": "Agent profile ID to configure the agent with specific tools/capabilities (optional)",
                },
                "preferred_workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace ID to prefer when assigning this task to an agent. "
                        "Use this when the task must run in a specific workspace (e.g. "
                        "one that contains a merge conflict). Get the ID from "
                        "find_merge_conflict_workspaces or list_workspaces."
                    ),
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of absolute file paths to images or files that the "
                        "agent should have access to when working on this task. "
                        "These are typically paths to Discord attachment images "
                        "that were downloaded locally. The agent will be told to "
                        "read these files using the Read tool."
                    ),
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "add_workspace",
        "description": (
            "Add a workspace directory for a project. Source types: 'clone' (auto-clones "
            "from the project's repo_url), 'link' (link an existing directory on disk). "
            "Workspaces are project-scoped and dynamically acquired by agents when assigned tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to add the workspace to",
                },
                "source": {
                    "type": "string",
                    "enum": ["clone", "link"],
                    "description": "How to set up the workspace: clone (from project repo_url), link (existing dir)",
                },
                "path": {
                    "type": "string",
                    "description": "Directory path (required for link, auto-generated for clone)",
                },
                "name": {
                    "type": "string",
                    "description": "Workspace name (optional)",
                },
            },
            "required": ["project_id", "source"],
        },
    },
    {
        "name": "list_workspaces",
        "description": "List workspaces, optionally filtered by project. Shows lock status (which agent/task holds each workspace).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Filter by project ID (optional)",
                },
            },
        },
    },
    {
        "name": "find_merge_conflict_workspaces",
        "description": (
            "Scan project workspaces to find which ones have branches with merge conflicts "
            "against the default branch (main). Returns workspace IDs, conflicting branches, "
            "and file details. Use this BEFORE creating a merge-conflict resolution task so you "
            "can pass the correct preferred_workspace_id to create_task — ensuring the agent "
            "gets assigned the workspace that actually contains the conflict."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to scan workspaces for (optional if active project is set)",
                },
            },
        },
    },
    {
        "name": "release_workspace",
        "description": "Force-release a stuck workspace lock. Use when a workspace is locked by a dead agent or stale task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace ID to release"},
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "remove_workspace",
        "description": (
            "Delete a workspace from a project. The workspace must not be locked by an agent. "
            "Accepts a workspace ID or name. Does not delete files on disk — only removes the "
            "workspace record from the database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID or name to delete",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project ID (used for name lookup; optional if active project is set)",
                },
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "sync_workspaces",
        "description": (
            "Synchronize all project workspaces to the latest main branch. "
            "Fetches latest changes, pushes any unpushed local commits, and rebases "
            "feature branches onto the updated main. Locked workspaces (in use by agents) "
            "are skipped. Workspaces with unresolvable conflicts are reported for manual "
            "intervention. Returns a per-workspace status report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to sync workspaces for (optional if active project is set)",
                },
                "skip_locked": {
                    "type": "boolean",
                    "description": "Skip workspaces locked by an agent (default: true). Set to false to sync all workspaces.",
                },
            },
        },
    },
    {
        "name": "list_agents",
        "description": "List all configured agents and their current state.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_agent",
        "description": (
            "Register a new agent. If no name is provided, a creative unique "
            "name is auto-generated. Agents start in IDLE state and immediately "
            "begin receiving tasks. Agents dynamically acquire workspace locks "
            "from available project workspaces when assigned tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Agent display name. Leave empty to auto-generate a creative name.",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Agent type (claude, codex, cursor, aider)",
                    "default": "claude",
                },
            },
        },
    },
    {
        "name": "edit_agent",
        "description": (
            "Edit an agent's properties: name or agent_type. "
            "Use this to rename agents or change their type."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID"},
                "name": {"type": "string", "description": "New display name (optional)"},
                "agent_type": {
                    "type": "string",
                    "description": "New agent type: claude, codex, cursor, aider (optional)",
                },
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "pause_agent",
        "description": (
            "Pause an agent so it stops receiving new tasks. If the agent is "
            "currently BUSY, it will finish its current task then stay paused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to pause"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "resume_agent",
        "description": "Resume a paused agent so it can receive tasks again.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to resume"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "delete_agent",
        "description": "Delete an agent and all its workspace mappings. Cannot delete a BUSY agent — stop its task first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to delete"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "set_active_project",
        "description": "Set or clear the active project. When set, all commands default to this project without needing to specify project_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to set as active, or empty/null to clear",
                },
            },
        },
    },
    {
        "name": "get_task",
        "description": "Get full details of a specific task including its description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "edit_task",
        "description": (
            "Edit a task's properties: project_id, title, description, priority, task_type, "
            "status, max_retries, verification_type, or profile_id. Use this to move a task "
            "to a different project, rename tasks, change priority, override status (admin), "
            "assign a profile, or adjust retry/verification settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "project_id": {"type": "string", "description": "Move task to a different project (optional)"},
                "title": {"type": "string", "description": "New title (optional)"},
                "description": {"type": "string", "description": "New description (optional)"},
                "priority": {"type": "integer", "description": "New priority (optional)"},
                "task_type": {
                    "type": ["string", "null"],
                    "enum": ["feature", "bugfix", "refactor", "test", "docs", "chore", "research", "plan", None],
                    "description": "New task type (optional, set to null to clear)",
                },
                "status": {
                    "type": "string",
                    "enum": ["DEFINED", "READY", "IN_PROGRESS", "COMPLETED", "FAILED", "BLOCKED"],
                    "description": "New status — admin override, bypasses state machine (optional)",
                },
                "max_retries": {"type": "integer", "description": "Max retry attempts (optional)"},
                "verification_type": {
                    "type": "string",
                    "enum": ["auto_test", "qa_agent", "human"],
                    "description": "How to verify task output (optional)",
                },
                "profile_id": {
                    "type": ["string", "null"],
                    "description": "Agent profile ID (optional, set to null to clear)",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "stop_task",
        "description": "Stop a task that is currently in progress. Cancels the agent working on it and marks the task as BLOCKED.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to stop"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "restart_task",
        "description": "Reset a completed, failed, or blocked task back to READY so it gets picked up by an agent again.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to restart"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "reopen_with_feedback",
        "description": (
            "Reopen a completed or failed task with feedback. Use this when a "
            "task needs rework — the feedback is appended to the task "
            "description and stored as a structured context entry so the agent "
            "sees it on re-execution. The task is reset to READY, retry count "
            "is cleared, and the PR URL is removed so a fresh PR can be created."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to reopen"},
                "feedback": {
                    "type": "string",
                    "description": (
                        "Feedback explaining what went wrong or what needs "
                        "to be fixed (appended to task description and stored "
                        "as a task context entry)"
                    ),
                },
            },
            "required": ["task_id", "feedback"],
        },
    },
    {
        "name": "delete_task",
        "description": "Delete a task. Cannot delete a task that is currently in progress.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to delete"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "archive_tasks",
        "description": (
            "Archive completed tasks to clear them from active task lists. "
            "Tasks are moved to the archived_tasks DB table (viewable with "
            "list_archived, restorable with restore_task) and a markdown "
            "reference note is written to the project workspace. Optionally "
            "also archive FAILED and BLOCKED tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to scope archiving to (optional — omit for all projects)",
                },
                "include_failed": {
                    "type": "boolean",
                    "description": (
                        "If true, also archive FAILED and BLOCKED tasks. "
                        "Default false."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "archive_task",
        "description": (
            "Archive a single task by ID. The task must be in a terminal status "
            "(COMPLETED, FAILED, or BLOCKED). Archived tasks are removed from "
            "active views but preserved for reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to archive"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "list_archived",
        "description": (
            "List archived tasks. Shows tasks that were previously completed and "
            "archived. Use this when the user wants to see old/finished tasks "
            "that have been cleared from the active view."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Filter by project (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of tasks to return (default 50)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "restore_task",
        "description": (
            "Restore an archived task back into the active task list. The task "
            "is restored with DEFINED status so it re-enters the normal "
            "orchestrator lifecycle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Archived task ID to restore",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "approve_task",
        "description": "Manually approve and complete a task that is AWAITING_APPROVAL. Use for tasks on LINK repos that don't have GitHub PRs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to approve"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "process_task_completion",
        "description": "Internal: Process a task completion to discover and archive plan files. Called by Supervisor after task completion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID that completed"},
                "workspace_path": {"type": "string", "description": "Path to the workspace where the task was executed"},
            },
            "required": ["task_id", "workspace_path"],
        },
    },
    {
        "name": "approve_plan",
        "description": "Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan and marks the task completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID whose plan to approve"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "reject_plan",
        "description": "Reject a plan for a task in AWAITING_PLAN_APPROVAL status with feedback. Reopens the task so the agent can revise the plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID whose plan to reject"},
                "feedback": {"type": "string", "description": "Feedback describing what changes are needed in the plan"},
            },
            "required": ["task_id", "feedback"],
        },
    },
    {
        "name": "delete_plan",
        "description": "Delete a plan for a task in AWAITING_PLAN_APPROVAL status. Completes the task without creating any subtasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID whose plan to delete"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "process_plan",
        "description": "Manually scan project workspaces for plan.md files and present them for approval. Use when the supervisor missed auto-detection or a plan was dropped into a workspace manually.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to scan for plan files. Uses active project if omitted."},
                "task_id": {"type": "string", "description": "Optional existing task ID to attach the plan to. If omitted, a new task is created."},
            },
        },
    },
    {
        "name": "skip_task",
        "description": "Skip a BLOCKED or FAILED task to unblock its dependency chain. Marks the task as COMPLETED so downstream dependents can proceed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to skip"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_dependencies",
        "description": (
            "Get the full dependency graph for a specific task: what it depends "
            "on (upstream) and what it blocks (downstream). Each entry includes "
            "the task's id, title, and current status. Use this when the user "
            "asks why a task is blocked, what depends on a task, or wants to "
            "understand the dependency chain for a specific task. "
            "Example: 'Task X is blocked because it depends on Y which is "
            "still IN_PROGRESS.'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to inspect dependencies for",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "add_dependency",
        "description": (
            "Add a dependency between two tasks: task_id will depend on "
            "depends_on (i.e. task_id cannot start until depends_on is "
            "completed). Validates that both tasks exist and performs cycle "
            "detection to prevent circular dependency chains. Use this when "
            "the user wants to link tasks so one must finish before another "
            "can begin."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task that should wait (downstream task)",
                },
                "depends_on": {
                    "type": "string",
                    "description": "The task that must complete first (upstream task)",
                },
            },
            "required": ["task_id", "depends_on"],
        },
    },
    {
        "name": "remove_dependency",
        "description": (
            "Remove a dependency between two tasks: task_id will no longer "
            "depend on depends_on. Use this when the user wants to unlink "
            "tasks or remove a blocking relationship."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The downstream task to unlink",
                },
                "depends_on": {
                    "type": "string",
                    "description": "The upstream task to remove as a dependency",
                },
            },
            "required": ["task_id", "depends_on"],
        },
    },
    {
        "name": "get_chain_health",
        "description": "Check dependency chain health. Shows downstream tasks stuck because of blocked tasks. Pass task_id for a specific task, or project_id for all stuck chains in a project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "(Optional) Check a specific blocked task"},
                "project_id": {"type": "string", "description": "(Optional) Check all blocked chains in a project"},
            },
        },
    },
    {
        "name": "get_status",
        "description": "Get a high-level overview of the system: projects, agents, tasks counts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_events",
        "description": "Get recent system events (task completions, failures, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of events to return (default 10)",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "get_task_result",
        "description": "Retrieve a task's output: summary, files changed, error message, tokens used. Use this when the user asks about what a task did or its results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_diff",
        "description": "Show the git diff for a task's branch against the base branch. Use when the user asks to see code changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file's contents from a workspace. Path can be absolute or relative to the workspaces root. Supports offset/limit for reading specific portions of large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
                "max_lines": {
                    "type": "integer",
                    "description": "Max lines to return (default 2000)",
                    "default": 2000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-based, default 1)",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. If set, overrides max_lines.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file (and parent directories) if it doesn't exist, or overwrites if it does. Path can be absolute or relative to the workspaces root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
                "content": {"type": "string", "description": "Content to write to the file"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Perform targeted string replacement in a file. Finds old_string and replaces it with new_string. "
            "The old_string must be unique in the file (include surrounding context to disambiguate). "
            "Use replace_all=true to replace every occurrence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
                "old_string": {"type": "string", "description": "Exact text to find and replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false — requires unique match)",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "glob_files",
        "description": (
            "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
            "Returns matching file paths sorted by modification time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match files (e.g. '**/*.py', 'src/components/**/*.tsx')"},
                "path": {"type": "string", "description": "Directory to search in (absolute or relative to workspaces root)"},
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "grep",
        "description": (
            "Search file contents using regex patterns (ripgrep-style). Supports context lines, "
            "case-insensitive search, file type filtering, and multiple output modes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "File or directory to search in (absolute or relative to workspaces root)"},
                "context": {
                    "type": "integer",
                    "description": "Number of context lines before and after each match",
                    "default": 0,
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default false)",
                    "default": False,
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py', '*.{ts,tsx}')",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Output mode: 'content' shows matching lines, 'files_with_matches' shows file paths only, 'count' shows match counts (default 'content')",
                    "default": "content",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of result lines to return (default 100)",
                    "default": 100,
                },
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories at a given path within a project workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "path": {"type": "string", "description": "Relative path within the workspace (default: root)"},
                "workspace": {"type": "string", "description": "Workspace name or ID (default: first workspace)"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "run_command",
        "description": "Execute a shell command in a workspace directory. Use when the user asks to run tests, check status, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "working_dir": {
                    "type": "string",
                    "description": "Working directory (absolute path or project ID)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30, max 120)",
                    "default": 30,
                },
            },
            "required": ["command", "working_dir"],
        },
    },
    {
        "name": "delete_project",
        "description": "Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the project's Discord channels.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to delete"},
                "archive_channels": {
                    "type": "boolean",
                    "description": "If true, archive the project's Discord channels (rename + set read-only) instead of leaving them as-is. Default: false.",
                    "default": False,
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "search_files",
        "description": "Search for files or content in a workspace. Use 'grep' mode to search file contents, 'find' mode to search filenames.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex for grep, glob for find)"},
                "path": {"type": "string", "description": "Directory to search in (absolute or relative to workspaces root)"},
                "mode": {
                    "type": "string",
                    "enum": ["grep", "find"],
                    "description": "Search mode: 'grep' for content, 'find' for filenames",
                    "default": "grep",
                },
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "get_token_usage",
        "description": "Get token usage breakdown by project or task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional)"},
                "task_id": {"type": "string", "description": "Task ID (optional)"},
            },
        },
    },
    {
        "name": "create_hook",
        "description": (
            "Create a hook that automatically triggers actions. Hooks gather context "
            "(shell commands, file reads, HTTP checks, DB queries) and send a prompt "
            "to an LLM that has access to all system tools (create_task, list_tasks, etc.). "
            "Trigger types: 'periodic' (interval_seconds), 'event' (event_type). "
            "Context step types: 'shell' (command, timeout, skip_llm_if_exit_zero), "
            "'read_file' (path, max_lines), 'http' (url, skip_llm_if_status_ok), "
            "'db_query' (query name, params), 'git_diff' (workspace, base_branch). "
            "Use {{step_0}}, {{step_1}}, {{event}} in prompt_template."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {"type": "string", "description": "Hook name (used as ID slug)"},
                "trigger": {
                    "type": "object",
                    "description": "Trigger config: {type: 'periodic', interval_seconds: N} or {type: 'event', event_type: '...'}",
                },
                "context_steps": {
                    "type": "array",
                    "description": "Array of context step configs",
                    "items": {"type": "object"},
                },
                "prompt_template": {
                    "type": "string",
                    "description": "Prompt template with {{step_N}} and {{event}} placeholders",
                },
                "cooldown_seconds": {
                    "type": "integer",
                    "description": "Min seconds between runs (default 3600)",
                    "default": 3600,
                },
                "llm_config": {
                    "type": "object",
                    "description": "Optional LLM config override: {provider, model, base_url}",
                },
            },
            "required": ["project_id", "name", "trigger", "prompt_template"],
        },
    },
    {
        "name": "list_hooks",
        "description": "List hooks, optionally filtered by project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter by project ID (optional)"},
            },
        },
    },
    {
        "name": "edit_hook",
        "description": (
            "Edit a hook's configuration: name, enabled, trigger, context_steps, "
            "prompt_template, cooldown_seconds, llm_config, or max_tokens_per_run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID"},
                "name": {"type": "string", "description": "New hook name (optional)"},
                "enabled": {"type": "boolean", "description": "Enable/disable the hook"},
                "trigger": {"type": "object", "description": "New trigger config"},
                "context_steps": {"type": "array", "description": "New context steps", "items": {"type": "object"}},
                "prompt_template": {"type": "string", "description": "New prompt template"},
                "cooldown_seconds": {"type": "integer", "description": "New cooldown"},
                "llm_config": {"type": "object", "description": "New LLM config override"},
                "max_tokens_per_run": {"type": ["integer", "null"], "description": "Max tokens per run (null to clear)"},
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "delete_hook",
        "description": "Delete a hook and its run history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID to delete"},
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "list_hook_runs",
        "description": "Show recent execution history for a hook.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID"},
                "limit": {"type": "integer", "description": "Number of runs to show (default 10)", "default": 10},
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "fire_hook",
        "description": "Manually trigger a hook immediately, ignoring cooldown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {"type": "string", "description": "Hook ID to fire"},
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "hook_schedules",
        "description": (
            "Show upcoming hook executions with human-readable next-run times. "
            "Lists all enabled periodic hooks, their schedule constraints, last "
            "run time, and when they are expected to fire next."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional project ID to filter hooks",
                },
            },
        },
    },
    {
        "name": "fire_all_scheduled_hooks",
        "description": (
            "Manually trigger all enabled periodic hooks, optionally filtered by project. "
            "Useful for testing or forcing immediate execution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional project ID to filter hooks",
                },
            },
        },
    },
    {
        "name": "schedule_hook",
        "description": (
            "Schedule a one-shot hook to fire at a specific time or after a delay. "
            "The hook runs once, executes its prompt with full tool access, then auto-deletes. "
            "Use for deferred work: reminders, delayed checks, timed actions. "
            "Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h', '1d')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {
                    "type": "string",
                    "description": "Descriptive name for the scheduled hook (optional, used as ID slug)",
                    "default": "scheduled-hook",
                },
                "prompt_template": {
                    "type": "string",
                    "description": "Prompt template to execute when the scheduled time arrives",
                },
                "fire_at": {
                    "type": ["number", "string"],
                    "description": "When to fire: epoch timestamp (number) or ISO-8601 datetime string. Mutually exclusive with 'delay'.",
                },
                "delay": {
                    "type": "string",
                    "description": "Delay before firing: e.g. '30s', '5m', '2h', '1d', '2h30m'. Mutually exclusive with 'fire_at'.",
                },
                "context_steps": {
                    "type": "array",
                    "description": "Optional context-gathering steps (same as create_hook)",
                    "items": {"type": "object"},
                },
                "llm_config": {
                    "type": "object",
                    "description": "Optional LLM config override: {provider, model, base_url}",
                },
            },
            "required": ["project_id", "prompt_template"],
        },
    },
    {
        "name": "list_scheduled",
        "description": (
            "List all pending scheduled (one-shot) hooks. Shows when each will fire, "
            "how long until it fires, and a preview of the prompt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Optional project ID to filter scheduled hooks",
                },
            },
        },
    },
    {
        "name": "cancel_scheduled",
        "description": "Cancel a scheduled hook before it fires. Removes it from the queue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hook_id": {
                    "type": "string",
                    "description": "Scheduled hook ID to cancel",
                },
            },
            "required": ["hook_id"],
        },
    },
    {
        "name": "list_notes",
        "description": "List all notes for a project. Returns name (filename), title, and size for each note. Use the 'name' field when calling read_note or delete_note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "write_note",
        "description": "Create or overwrite a project note. Use to create new notes or to save edits (read with read_file first, modify, then write back).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (used as filename)"},
                "content": {"type": "string", "description": "Full markdown content"},
            },
            "required": ["project_id", "title", "content"],
        },
    },
    {
        "name": "delete_note",
        "description": (
            "Delete a project note by title. If the user provides the note name "
            "directly, call this tool immediately — no need to list notes first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "read_note",
        "description": (
            "Read a note's full contents. Returns the markdown content, path, and size. "
            "Use the 'name' field from list_notes (e.g. 'my-note.md') as the title parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "append_note",
        "description": (
            "Append content to an existing note, or create a new note if it doesn't exist. "
            "Ideal for stream-of-consciousness input — appends with a blank line separator "
            "without needing to read and rewrite the entire note."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (used as filename)"},
                "content": {
                    "type": "string",
                    "description": "Content to append (or initial content if creating)",
                },
            },
            "required": ["project_id", "title", "content"],
        },
    },
    {
        "name": "promote_note",
        "description": (
            "Explicitly incorporate a note's content into the project profile. "
            "Uses an LLM to integrate the note's knowledge into the living profile "
            "rather than simply appending. Use when a note contains important knowledge "
            "that should be part of the project's core understanding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {
                    "type": "string",
                    "description": (
                        "Note filename from list_notes 'name' field (e.g. 'my-note.md'), "
                        "or the note title"
                    ),
                },
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "compare_specs_notes",
        "description": (
            "List all spec files and note files for a project side by side. "
            "Returns raw file listings (names, titles, sizes) for gap analysis. "
            "Use this when the user asks to compare specs with notes or find "
            "what's missing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "specs_path": {
                    "type": "string",
                    "description": "Override path to specs directory (optional)",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "list_prompts",
        "description": (
            "List all prompt templates for a project. Returns name, description, "
            "category, tags, and variable schemas for each template. "
            "Optionally filter by category (system, task, hooks, custom) or tag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "category": {
                    "type": "string",
                    "description": (
                        "Filter by category: system, task, hooks, or custom (optional)"
                    ),
                },
                "tag": {
                    "type": "string",
                    "description": "Filter by tag (optional)",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "read_prompt",
        "description": (
            "Read a prompt template's full content and metadata. Returns the "
            "template body, variable definitions, tags, and category. "
            "Use the 'name' field from list_prompts as the name parameter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {
                    "type": "string",
                    "description": (
                        "Template name from list_prompts (e.g. 'plan-generation'), "
                        "or the filename (e.g. 'plan-generation.md')"
                    ),
                },
            },
            "required": ["project_id", "name"],
        },
    },
    {
        "name": "render_prompt",
        "description": (
            "Render a prompt template with variable substitution. Replaces "
            "{{variable}} placeholders with provided values. Returns the "
            "fully rendered prompt text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "name": {
                    "type": "string",
                    "description": "Template name to render",
                },
                "variables": {
                    "type": "object",
                    "description": (
                        "Key-value pairs for template variables "
                        "(e.g. {\"task_title\": \"Fix login bug\"})"
                    ),
                },
            },
            "required": ["project_id", "name"],
        },
    },
    {
        "name": "get_git_status",
        "description": (
            "Get the git status of a project's repository. Shows current branch, "
            "working tree status, and recent commits. Reports status for all workspaces "
            "registered to the project, or falls back to the project workspace path. "
            "Operates on the active project's repository."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (optional — inferred from active project)",
                },
            },
        },
    },
    {
        "name": "git_commit",
        "description": (
            "Stage all changes and create a commit in a repository. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "git_pull",
        "description": (
            "Pull (fetch + merge) a branch from the remote origin. Defaults to the current branch if not specified. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "branch": {"type": "string", "description": "Branch name to pull (defaults to current branch)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "git_push",
        "description": (
            "Push a branch to the remote origin. Defaults to the current branch if not specified. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "branch": {"type": "string", "description": "Branch name to push (defaults to current branch)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "git_create_branch",
        "description": (
            "Create and switch to a new git branch in a repository. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string", "description": "Name for the new branch"},
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "git_merge",
        "description": (
            "Merge a branch into the default branch. Returns whether the merge "
            "succeeded or had conflicts (conflicts are automatically aborted). "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string", "description": "Branch to merge"},
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "default_branch": {
                    "type": "string",
                    "description": "Target branch to merge into (defaults to repo's default branch)",
                },
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "git_create_pr",
        "description": (
            "Create a GitHub pull request using the gh CLI. Requires gh to be authenticated. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "body": {"type": "string", "description": "PR description body (optional)", "default": ""},
                "branch": {"type": "string", "description": "Head branch (defaults to current branch)"},
                "base": {"type": "string", "description": "Base branch (defaults to repo's default branch)"},
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "git_changed_files",
        "description": (
            "List files changed compared to a base branch. Lighter than a full diff. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "base_branch": {
                    "type": "string",
                    "description": "Branch to compare against (defaults to repo's default branch)",
                },
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "git_log",
        "description": (
            "Show recent git commits for a project's repository. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "count": {"type": "integer", "description": "Number of commits to show (default 10)", "default": 10},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "git_diff",
        "description": (
            "Show the git diff for a project's repository. Without base_branch shows working tree changes; "
            "with base_branch shows diff against that branch. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "base_branch": {"type": "string", "description": "Base branch to diff against (optional — shows working tree diff if omitted)"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
        },
    },
    {
        "name": "checkout_branch",
        "description": (
            "Switch to an existing git branch in a project's repository. "
            "Operates on the active project's repository. "
            "Use the workspace parameter to target a specific workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID (optional — inferred from active project)"},
                "branch_name": {"type": "string", "description": "Branch name to check out"},
                "workspace": {"type": "string", "description": "Workspace ID or name to operate on (optional — defaults to first workspace)"},
            },
            "required": ["branch_name"],
        },
    },
    {
        "name": "restart_daemon",
        "description": "Restart the agent-queue daemon process. The bot will disconnect briefly and reconnect. A reason is required.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the restart is being requested",
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "orchestrator_control",
        "description": "Pause, resume, or check the status of the orchestrator (task scheduler). When paused, no new tasks will be assigned to agents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "status"],
                    "description": "Action to perform: pause, resume, or status",
                },
            },
            "required": ["action"],
        },
    },
    # Note: set_task_status has been removed. Use edit_task with status instead.
    {
        "name": "get_agent_error",
        "description": "Get the last error recorded for a task, including error classification, suggested fix, and agent summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to inspect"},
            },
            "required": ["task_id"],
        },
    },
    # --- Agent Profile tools ---
    {
        "name": "list_profiles",
        "description": "List all agent profiles. Profiles are capability bundles that configure agents with specific tools, MCP servers, and system prompts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_profile",
        "description": (
            "Create a new agent profile. Profiles configure agents with specific tools, "
            "MCP servers, model overrides, and system prompt additions. Assign profiles "
            "to tasks (profile_id) or set as project defaults (default_profile_id)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Profile slug ID (e.g. 'reviewer', 'web-developer')",
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable display name",
                },
                "description": {
                    "type": "string",
                    "description": "What this profile is for (optional)",
                },
                "model": {
                    "type": "string",
                    "description": "Model override (optional, empty = use default)",
                },
                "permission_mode": {
                    "type": "string",
                    "description": "Permission mode override (optional)",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool whitelist (e.g. ['Read', 'Glob', 'Grep', 'Bash'])",
                },
                "mcp_servers": {
                    "type": "object",
                    "description": "MCP server configurations (name -> {command, args})",
                },
                "system_prompt_suffix": {
                    "type": "string",
                    "description": "Text appended to the agent's system prompt (optional)",
                },
            },
            "required": ["id", "name"],
        },
    },
    {
        "name": "get_profile",
        "description": "Get details of a specific agent profile.",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to look up"},
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "edit_profile",
        "description": "Edit an existing agent profile's properties.",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to edit"},
                "name": {"type": "string", "description": "New display name (optional)"},
                "description": {"type": "string", "description": "New description (optional)"},
                "model": {"type": "string", "description": "New model override (optional)"},
                "permission_mode": {"type": "string", "description": "New permission mode (optional)"},
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New tool whitelist (optional)",
                },
                "mcp_servers": {
                    "type": "object",
                    "description": "New MCP server configurations (optional)",
                },
                "system_prompt_suffix": {
                    "type": "string",
                    "description": "New system prompt suffix (optional)",
                },
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "delete_profile",
        "description": "Delete an agent profile. Any tasks or projects referencing it will have their profile cleared.",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to delete"},
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "list_available_tools",
        "description": (
            "Discover available Claude Code tools and well-known MCP servers "
            "for use in agent profiles."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_profile",
        "description": (
            "Validate an agent profile's install dependencies. Checks that "
            "required commands, npm packages, and pip packages are available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to check"},
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "install_profile",
        "description": (
            "Install missing npm/pip dependencies for a profile's install manifest. "
            "System commands that are missing are reported for manual installation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to install deps for"},
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "export_profile",
        "description": (
            "Export an agent profile as YAML. Optionally create a public GitHub gist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "profile_id": {"type": "string", "description": "Profile ID to export"},
                "create_gist": {
                    "type": "boolean",
                    "description": "If true, create a public GitHub gist and return the URL",
                },
            },
            "required": ["profile_id"],
        },
    },
    {
        "name": "import_profile",
        "description": (
            "Import an agent profile from YAML text or a GitHub gist URL. "
            "If the profile has an install manifest, dependencies are auto-installed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "YAML text or gist URL to import from",
                },
                "id": {
                    "type": "string",
                    "description": "Override the profile ID (optional)",
                },
                "name": {
                    "type": "string",
                    "description": "Override the profile name (optional)",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "If true, overwrite existing profile with same ID",
                },
            },
            "required": ["source"],
        },
    },
    # --- Memory tools ---
    {
        "name": "memory_search",
        "description": (
            "Search project memory for relevant context. Returns semantically "
            "similar past task results, notes, and knowledge-base entries. "
            "Use this when the user asks about past work, wants to find related "
            "context, or says 'search memory', 'what do we know about', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to search memory for",
                },
                "query": {
                    "type": "string",
                    "description": "Semantic search query",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default 10)",
                    "default": 10,
                },
            },
            "required": ["project_id", "query"],
        },
    },
    {
        "name": "memory_stats",
        "description": (
            "Get memory index statistics for a project. Shows whether memory "
            "is enabled, the collection name, embedding provider, and "
            "auto-recall/auto-remember settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to get memory stats for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "memory_reindex",
        "description": (
            "Force a full reindex of a project's memory. Re-scans all markdown "
            "files in memory/ and notes/ directories, re-embeds changed content, "
            "and updates the vector index. Use when memory seems stale or after "
            "bulk file changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to reindex memory for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "view_profile",
        "description": (
            "View the project profile — a synthesized understanding of the project's "
            "architecture, conventions, key decisions, common patterns, and pitfalls. "
            "The profile evolves automatically as tasks complete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to view profile for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "edit_profile",
        "description": (
            "Replace the project profile with new content. Use this to manually "
            "correct or enhance the project's synthesized understanding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to edit profile for",
                },
                "content": {
                    "type": "string",
                    "description": "New profile content (markdown)",
                },
            },
            "required": ["project_id", "content"],
        },
    },
    {
        "name": "regenerate_profile",
        "description": (
            "Force LLM regeneration of the project profile from the full task "
            "history. Use this when the profile has drifted or you want a fresh "
            "synthesis of everything the project has learned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to regenerate profile for",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "compact_memory",
        "description": (
            "Trigger memory compaction for a project. Groups task memories "
            "by age: recent (kept as-is), medium (LLM-summarized into weekly "
            "digests), old (deleted after digesting). Returns stats on tasks "
            "inspected, digests created, and files removed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to compact memory for",
                },
            },
            "required": ["project_id"],
        },
    },
    # analyzer tool definitions removed (Phase 6)
    # --- Plugin management tools ---
    {
        "name": "plugin_list",
        "description": "List all installed plugins with their status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "plugin_info",
        "description": "Get detailed information about a specific plugin.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "plugin_install",
        "description": "Install a plugin from a git repository URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Git repository URL for the plugin",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch to install (optional)",
                },
                "name": {
                    "type": "string",
                    "description": "Override plugin name (optional)",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "plugin_update",
        "description": "Update an installed plugin to the latest version.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name to update",
                },
                "rev": {
                    "type": "string",
                    "description": "Specific revision to update to (optional)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "plugin_remove",
        "description": "Completely remove an installed plugin.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name to remove",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "plugin_enable",
        "description": "Enable a disabled plugin.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name to enable",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "plugin_disable",
        "description": "Disable a plugin without removing it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name to disable",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "plugin_reload",
        "description": "Reload a plugin (shutdown, reimport, reinitialize).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name to reload",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "plugin_config",
        "description": "Get or set a plugin's configuration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name",
                },
                "config": {
                    "type": "string",
                    "description": "New config as JSON string (optional, omit to read current)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "plugin_prompts",
        "description": "List a plugin's prompt templates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "plugin_reset_prompts",
        "description": "Reset a plugin's prompts to defaults from source.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Plugin name",
                },
            },
            "required": ["name"],
        },
    },
]

class ToolRegistry:
    """Registry that categorizes tools into core and on-demand categories.

    Initialized with a list of tool definition dicts (JSON Schema format).
    Each tool is either "core" (always loaded) or belongs to a named category.
    """

    def __init__(self, tools: list[dict] | None = None):
        """Initialize with tool definitions.

        Args:
            tools: List of tool definition dicts. If None, uses the
                   built-in _ALL_TOOL_DEFINITIONS.
        """
        if tools is None:
            tools = list(_ALL_TOOL_DEFINITIONS)
        self._all_tools: dict[str, dict] = {t["name"]: t for t in tools}
        self._plugin_registry = None
        # Add new tools that don't exist in the legacy TOOLS list
        self._ensure_navigation_tools()

    def set_plugin_registry(self, plugin_registry) -> None:
        """Set the plugin registry for dynamic tool merging."""
        self._plugin_registry = plugin_registry

    def _ensure_navigation_tools(self) -> None:
        """Add browse_tools, load_tools, send_message, and rule stubs
        if not already present."""
        if "browse_tools" not in self._all_tools:
            self._all_tools["browse_tools"] = {
                "name": "browse_tools",
                "description": (
                    "List available tool categories. Returns category "
                    "names, descriptions, and tool counts. Use this to "
                    "discover what tools are available, then call "
                    "load_tools to load a category."
                ),
                "input_schema": {"type": "object", "properties": {}},
            }
        if "load_tools" not in self._all_tools:
            self._all_tools["load_tools"] = {
                "name": "load_tools",
                "description": (
                    "Load all tools from a specific category, making "
                    "them available for the remainder of this "
                    "interaction. Call browse_tools first to see "
                    "available categories."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": (
                                "Category name to load "
                                "(e.g. 'git', 'project')"
                            ),
                        },
                    },
                    "required": ["category"],
                },
            }
        if "send_message" not in self._all_tools:
            self._all_tools["send_message"] = {
                "name": "send_message",
                "description": (
                    "Post a message to a Discord channel. Use this to "
                    "notify users, post updates, or communicate outside "
                    "the current conversation thread."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "channel_id": {
                            "type": "string",
                            "description": (
                                "Discord channel ID to post to"
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "Message content to post",
                        },
                    },
                    "required": ["channel_id", "content"],
                },
            }
        # reply_to_user — mandatory response delivery tool
        if "reply_to_user" not in self._all_tools:
            self._all_tools["reply_to_user"] = {
                "name": "reply_to_user",
                "description": (
                    "Deliver your final response to the user. You MUST call "
                    "this tool when you are done processing a request. Do not "
                    "stop calling tools until you have gathered enough "
                    "information to provide a complete answer, then call this "
                    "tool with your response. The message should directly "
                    "address the user's request — not just list what tools "
                    "you called."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": (
                                "The complete response to send to the user. "
                                "Must directly answer their question or "
                                "confirm the action taken with relevant "
                                "details."
                            ),
                        },
                    },
                    "required": ["message"],
                },
            }
        # Rule stubs (Phase 2 placeholders)
        for rule_tool in self._get_rule_tool_stubs():
            if rule_tool["name"] not in self._all_tools:
                self._all_tools[rule_tool["name"]] = rule_tool

    @staticmethod
    def _get_rule_tool_stubs() -> list[dict]:
        """Return stub tool definitions for Phase 2 rule tools."""
        return [
            {
                "name": "browse_rules",
                "description": (
                    "List active rules for current project and globals."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Project ID (optional, defaults to "
                                "active project)"
                            ),
                        },
                    },
                },
            },
            {
                "name": "load_rule",
                "description": "Load a specific rule's full detail.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Rule ID",
                        },
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "save_rule",
                "description": "Create or update a rule.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": (
                                "Rule ID (auto-generated if omitted)"
                            ),
                        },
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Project ID (null = global)"
                            ),
                        },
                        "type": {
                            "type": "string",
                            "enum": ["active", "passive"],
                            "description": "Rule type",
                        },
                        "content": {
                            "type": "string",
                            "description": "Rule content (markdown)",
                        },
                    },
                    "required": ["type", "content"],
                },
            },
            {
                "name": "delete_rule",
                "description": "Remove a rule.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Rule ID",
                        },
                    },
                    "required": ["id"],
                },
            },
        ]

    def get_core_tools(self) -> list[dict]:
        """Return tool definitions that are always loaded."""
        return [
            t for name, t in self._all_tools.items()
            if name not in _TOOL_CATEGORIES
        ]

    def get_categories(self) -> list[dict]:
        """Return category metadata list for browse_tools response."""
        result = []
        for cat_name, meta in CATEGORIES.items():
            tools = self.get_category_tools(cat_name)
            result.append({
                "name": meta.name,
                "description": meta.description,
                "tool_count": len(tools) if tools else 0,
            })
        return result

    def get_category_tools(self, category: str) -> list[dict] | None:
        """Return all tool definitions for a category, or None if
        unknown."""
        if category not in CATEGORIES:
            return None
        return [
            self._all_tools[name]
            for name, cat in _TOOL_CATEGORIES.items()
            if cat == category and name in self._all_tools
        ]

    def get_category_tool_names(
        self, category: str
    ) -> list[str] | None:
        """Return tool names for a category, or None if unknown."""
        if category not in CATEGORIES:
            return None
        return [
            name for name, cat in _TOOL_CATEGORIES.items()
            if cat == category and name in self._all_tools
        ]

    def get_all_tools(self) -> list[dict]:
        """Return all tool definitions (core + all categories + plugins)."""
        tools = list(self._all_tools.values())
        if self._plugin_registry:
            tools.extend(self._plugin_registry.get_all_tool_definitions())
        return tools
