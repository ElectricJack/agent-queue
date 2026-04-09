"""Tiered tool registry for on-demand tool loading.

Splits the monolithic TOOLS list into core tools (always loaded) and
named categories (loaded on demand via ``browse_tools``/``load_tools``).
This keeps the LLM's initial context window small — only ~10 core tools
are loaded at conversation start.  When the LLM needs specialised tools
(git, hooks, memory, etc.) it calls ``browse_tools`` to discover categories,
then ``load_tools`` to inject that category's definitions into the active
tool set for subsequent turns.

The registry only manages tool *definitions* (JSON Schema dicts that
describe each tool's name, description, and input schema).  Execution
still flows through ``CommandHandler.execute()`` regardless of whether
a tool is "loaded" in the LLM's context — the loading mechanism is
purely an attention/context optimisation.

Key components:

- ``CATEGORIES`` — named groups (git, project, agent, hooks, memory,
  files, system) with human-readable descriptions.
- ``_TOOL_CATEGORIES`` — mapping of tool name → category.  Tools not
  listed here are "core" (always loaded).
- ``_ALL_TOOL_DEFINITIONS`` — the master list of ~80 tool JSON Schema
  dicts.  Each entry corresponds to a ``CommandHandler._cmd_*`` method.
- ``ToolRegistry`` — the public API: ``get_core_tools()``,
  ``get_category_tools(cat)``, ``get_all_tools()``.

See ``specs/supervisor.md`` for the tool-use loop that drives loading.
"""

from __future__ import annotations

import re
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
            "Branch, commit, push, PR, merge, and remote URL operations for project repositories"
        ),
    ),
    "project": CategoryMeta(
        name="project",
        description=(
            "Project CRUD, workspace management, channel configuration, "
            "project metadata (repo URL, GitHub URL, workspace path)"
        ),
    ),
    "agent": CategoryMeta(
        name="agent",
        description=("Agent management, agent profiles, profile import/export"),
    ),
    "rules": CategoryMeta(
        name="rules",
        description=("Automation rules — list, view, create, fire, toggle, run history"),
    ),
    "memory": CategoryMeta(
        name="memory",
        description=("Semantic memory — search, project profiles, compaction, reindexing"),
    ),
    "notes": CategoryMeta(
        name="notes",
        description=("Project notes — list, read, write, append, delete, promote notes to specs"),
    ),
    "files": CategoryMeta(
        name="files",
        description=(
            "Filesystem tools — read, write, edit files, glob pattern "
            "matching, grep/ripgrep-style content search"
        ),
    ),
    "task": CategoryMeta(
        name="task",
        description=("Task lifecycle, approval, dependencies, archives, and results"),
    ),
    "playbook": CategoryMeta(
        name="playbook",
        description=("Playbook compilation, run management, human-in-the-loop review and resume"),
    ),
    "plugin": CategoryMeta(
        name="plugin",
        description=("Plugin installation, configuration, and lifecycle management"),
    ),
    "system": CategoryMeta(
        name="system",
        description=("Token usage, config reload, diagnostics, prompt management, daemon control"),
    ),
}

# Which category each tool belongs to.
# Tools not listed here are "core" (always loaded).
_TOOL_CATEGORIES: dict[str, str] = {
    # git — migrated to aq-git internal plugin (src/plugins/internal/git.py)
    # project
    "list_projects": "project",
    "create_project": "project",
    "pause_project": "project",
    "resume_project": "project",
    "edit_project": "project",
    "set_default_branch": "project",
    "get_project": "project",
    "get_project_channels": "project",
    "get_project_for_channel": "project",
    "delete_project": "project",
    "add_workspace": "project",
    "list_workspaces": "project",
    "find_merge_conflict_workspaces": "project",
    "release_workspace": "project",
    "remove_workspace": "project",
    "queue_sync_workspaces": "project",
    "set_active_project": "project",
    "set_project_channel": "project",
    "set_control_interface": "project",
    # agent (workspace-as-agent model — deprecated CRUD still available)
    "list_agents": "agent",
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
    "create_agent": "agent",
    "edit_agent": "agent",
    "delete_agent": "agent",
    "pause_agent": "agent",
    "resume_agent": "agent",
    # hooks (read-only / execution — all creation goes through rules)
    "browse_rules": "rules",
    "list_rules": "rules",
    "load_rule": "rules",
    "save_rule": "rules",
    "delete_rule": "rules",
    "fire_rule": "rules",
    "rule_runs": "rules",
    "toggle_rule": "rules",
    "refresh_rules": "rules",
    # memory
    "recall_topic_context": "memory",
    # memory — migrated to aq-memory internal plugin (src/plugins/internal/memory.py)
    # notes — migrated to aq-notes internal plugin (src/plugins/internal/notes.py)
    # files — migrated to aq-files internal plugin (src/plugins/internal/files.py)
    # task — lifecycle, approval, dependencies, archives, results
    "stop_task": "task",
    "restart_task": "task",
    "reopen_with_feedback": "task",
    "delete_task": "task",
    "skip_task": "task",
    "set_task_status": "task",
    "restore_task": "task",
    "approve_task": "task",
    "process_task_completion": "task",
    "approve_plan": "task",
    "reject_plan": "task",
    "delete_plan": "task",
    "process_plan": "task",
    "archive_task": "task",
    "archive_tasks": "task",
    "archive_settings": "task",
    "list_archived": "task",
    "get_task_result": "task",
    "get_task_diff": "task",
    "get_task_tree": "task",
    "get_task_dependencies": "task",
    "task_deps": "task",
    "add_dependency": "task",
    "remove_dependency": "task",
    "get_chain_health": "task",
    "list_active_tasks_all_projects": "task",
    # playbook — compilation, run management, human-in-the-loop resume
    "compile_playbook": "playbook",
    "show_playbook_graph": "playbook",
    "list_playbooks": "playbook",
    "list_playbook_runs": "playbook",
    "inspect_playbook_run": "playbook",
    "resume_playbook": "playbook",
    # plugin — installation, configuration, lifecycle
    "plugin_list": "plugin",
    "plugin_info": "plugin",
    "plugin_install": "plugin",
    "plugin_update": "plugin",
    "plugin_remove": "plugin",
    "plugin_enable": "plugin",
    "plugin_disable": "plugin",
    "plugin_reload": "plugin",
    "plugin_config": "plugin",
    "plugin_prompts": "plugin",
    "plugin_reset_prompts": "plugin",
    # system — diagnostics, config, prompts, daemon control
    "get_status": "system",
    "get_recent_events": "system",
    "get_token_usage": "system",
    "token_audit": "system",
    "claude_usage": "system",
    "reload_config": "system",
    "orchestrator_control": "system",
    "provide_input": "system",
    "list_prompts": "system",
    "read_prompt": "system",
    "render_prompt": "system",
    "shutdown": "system",
    "restart_daemon": "system",
    "update_and_restart": "system",
    "run_command": "system",
    # NOTE: send_message, reply_to_user are intentionally NOT categorized —
    # they are "core" tools always available to the supervisor LLM.
    # NOTE: browse_tools / load_tools are intentionally NOT categorized —
    # they are "core" meta-tools always loaded in the supervisor LLM's context.
    # NOTE: create_task, list_tasks, get_task, edit_task are intentionally
    # NOT categorized — they are core task operations always available to the
    # supervisor LLM.  The CLI places them in the "task" group via
    # _CLI_CATEGORY_OVERRIDES.
}

# Commands that are intentionally uncategorized for the LLM (core tools)
# but should appear in a specific CLI group.
_CLI_CATEGORY_OVERRIDES: dict[str, str] = {
    "create_task": "task",
    "list_tasks": "task",
    "get_task": "task",
    "edit_task": "task",
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
                "credit_weight": {
                    "type": "number",
                    "description": "New scheduling weight (optional)",
                },
                "max_concurrent_agents": {
                    "type": "integer",
                    "description": "New max concurrent agents (optional)",
                },
                "budget_limit": {
                    "type": ["integer", "null"],
                    "description": "Token budget limit (optional, null to clear)",
                },
                "discord_channel_id": {
                    "type": ["string", "null"],
                    "description": "Discord channel ID to link (optional, null to unlink)",
                },
                "default_profile_id": {
                    "type": ["string", "null"],
                    "description": "Default agent profile ID for tasks in this project (optional, null to clear)",
                },
                "repo_default_branch": {
                    "type": "string",
                    "description": "Default git branch for the project (e.g. main, dev, master)",
                },
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
                "branch": {
                    "type": "string",
                    "description": "Branch name to use as the new default (e.g. dev, main, master)",
                },
            },
            "required": ["project_id", "branch"],
        },
    },
    {
        "name": "get_project",
        "description": (
            "Get full details for a single project, including repo/GitHub URL, workspace path, "
            "default branch, token usage, and configuration. Use this when you need "
            "project metadata like the GitHub/git repository URL, workspace location, or budget info. "
            "This is the go-to tool for answering questions about a project's URL, location, or setup."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID to look up"},
            },
            "required": ["project_id"],
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
                        "When true, return ONLY completed/failed/blocked tasks. Default false."
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
        "description": "Create a task for an agent to execute. This is your PRIMARY tool for getting work done — prefer creating tasks over doing file work yourself. Agents have full context windows, isolated workspaces, and can run in parallel. Task descriptions must be completely self-contained with all context the agent needs. If no project_id is given, it inherits from the active project.",
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
                    "description": "Complete, self-contained instructions for the agent. Include ALL context: file paths, requirements, error messages, expected behavior, relevant code snippets, and design decisions from this conversation. Write as if the agent has never seen this conversation.",
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
                    "enum": [
                        "feature",
                        "bugfix",
                        "refactor",
                        "test",
                        "docs",
                        "chore",
                        "research",
                        "plan",
                    ],
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
                "auto_approve_plan": {
                    "type": "boolean",
                    "description": (
                        "If true, any plan this task generates will be "
                        "automatically approved without waiting for human review."
                    ),
                    "default": False,
                },
                "skip_verification": {
                    "type": "boolean",
                    "description": (
                        "If true, skip git verification on task completion. "
                        "Use for investigation/research tasks that don't produce "
                        "code changes requiring git cleanup."
                    ),
                    "default": False,
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
        "name": "queue_sync_workspaces",
        "description": (
            "Queue a high-priority Sync Workspaces task that orchestrates a full "
            "workspace synchronization workflow. When executed, the task will: "
            "(1) pause the project, (2) wait for all active tasks to complete, "
            "(3) launch a Claude Code agent to merge all feature branches into the "
            "default branch across all workspaces, (4) resume the project. "
            "Use this when workspaces have drifted from the default branch and "
            "feature work is stuck on feature branches that need consolidation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to sync (optional if active project is set)",
                },
            },
        },
    },
    {
        "name": "list_agents",
        "description": (
            "List agent slots for a project. Each workspace is an agent slot: "
            "locked workspaces are 'busy', unlocked are 'idle'. "
            "Requires project_id (or an active project)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID to list agents for (optional if active project is set)",
                },
            },
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
            "status, max_retries, verification_type, profile_id, auto_approve_plan, "
            "or skip_verification. Use this "
            "to move a task to a different project, rename tasks, change priority, override status "
            "(admin), assign a profile, or adjust retry/verification settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "project_id": {
                    "type": "string",
                    "description": "Move task to a different project (optional)",
                },
                "title": {"type": "string", "description": "New title (optional)"},
                "description": {"type": "string", "description": "New description (optional)"},
                "priority": {"type": "integer", "description": "New priority (optional)"},
                "task_type": {
                    "type": ["string", "null"],
                    "enum": [
                        "feature",
                        "bugfix",
                        "refactor",
                        "test",
                        "docs",
                        "chore",
                        "research",
                        "plan",
                        None,
                    ],
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
                "auto_approve_plan": {
                    "type": "boolean",
                    "description": "If true, any plan this task generates will be automatically approved without human review (optional)",
                },
                "skip_verification": {
                    "type": "boolean",
                    "description": "If true, skip git verification on task completion (optional)",
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
            "reference note is written to ~/.agent-queue/archived_tasks/. "
            "Optionally also archive FAILED and BLOCKED tasks."
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
                        "If true, also archive FAILED and BLOCKED tasks. Default false."
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
                "workspace_path": {
                    "type": "string",
                    "description": "Path to the workspace where the task was executed",
                },
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
                "feedback": {
                    "type": "string",
                    "description": "Feedback describing what changes are needed in the plan",
                },
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
                "project_id": {
                    "type": "string",
                    "description": "Project ID to scan for plan files. Uses active project if omitted.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Optional existing task ID to attach the plan to. If omitted, a new task is created.",
                },
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
                "task_id": {
                    "type": "string",
                    "description": "(Optional) Check a specific blocked task",
                },
                "project_id": {
                    "type": "string",
                    "description": "(Optional) Check all blocked chains in a project",
                },
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
    # File tools (read_file, write_file, edit_file, glob_files, grep,
    # list_directory) migrated to aq-files internal plugin.
    # Their tool definitions are now registered by FilesPlugin.initialize().
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
    # search_files — migrated to aq-files internal plugin
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
        "name": "token_audit",
        "description": (
            "Comprehensive token usage audit over a time range. "
            "Shows totals by project, top tasks, and daily breakdown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to audit (default: 7)",
                },
                "project_id": {
                    "type": "string",
                    "description": "Filter to a specific project (optional)",
                },
            },
        },
    },
    # Hook tool definitions removed — hooks are now an internal implementation
    # detail. All automation is managed through rules (see rule tools below).
    # Notes tools (list_notes, write_note, delete_note, read_note,
    # append_note, promote_note, compare_specs_notes) migrated to
    # aq-notes internal plugin.
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
                        '(e.g. {"task_title": "Fix login bug"})'
                    ),
                },
            },
            "required": ["project_id", "name"],
        },
    },
    # Git tools (get_git_status, git_commit, git_pull, git_push, git_create_branch,
    # git_merge, git_create_pr, git_changed_files, git_log, git_diff, git_branch,
    # git_checkout, checkout_branch) migrated to aq-git internal plugin.
    {
        "name": "restart_daemon",
        "description": "Restart the agent-queue daemon process. The bot will disconnect briefly and reconnect. A reason is required. Use wait_for_tasks=true to let running tasks finish before restarting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the restart is being requested",
                },
                "wait_for_tasks": {
                    "type": "boolean",
                    "description": "If true, pause orchestrator and wait for running tasks to complete before restarting (up to 5 minutes). Default: false.",
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
                "permission_mode": {
                    "type": "string",
                    "description": "New permission mode (optional)",
                },
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
        "description": ("Export an agent profile as YAML. Optionally create a public GitHub gist."),
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
    # Memory tools (memory_search, memory_stats, memory_reindex, view_profile,
    # edit_project_profile, regenerate_profile, compact_memory) migrated to
    # aq-memory internal plugin.
    # ------------------------------------------------------------------
    # On-demand L2 topic context — roadmap 3.3.6
    # ------------------------------------------------------------------
    {
        "name": "recall_topic_context",
        "description": (
            "Load topic-filtered knowledge and memories on-demand when a new "
            "topic emerges during task execution.  Detects topics from the "
            "provided text (or accepts explicit topic names) and returns "
            "matching knowledge-base files and memories.  Use this when you "
            "encounter a topic area that wasn't covered by the initial task "
            "context — e.g. you're working on a UI feature but discover you "
            "need deployment knowledge.  Returns a formatted context block "
            "with relevant project knowledge and past insights."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (defaults to active project).",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Context text to detect topics from.  Can be a "
                        "description of what you're working on, a code "
                        "snippet, or conversation excerpt.  Topics are "
                        "detected via keyword matching."
                    ),
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Explicit list of topic names to load (e.g. "
                        "['deployment', 'testing']).  When provided, "
                        "topic detection is skipped."
                    ),
                },
                "exclude_topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Topics already loaded in your context (e.g. from "
                        "task start).  These will be excluded to avoid "
                        "duplicate content."
                    ),
                },
            },
        },
    },
    # ------------------------------------------------------------------
    # Rule management tools — primary automation interface exposed via MCP
    # ------------------------------------------------------------------
    {
        "name": "list_rules",
        "description": (
            "List all automation rules for the current project and globals. "
            "Rules are the ONLY way to create automation — each active rule "
            "generates hooks that execute automatically. "
            "Alias: browse_rules"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": ("Project ID (optional, defaults to active project)"),
                },
            },
        },
    },
    {
        "name": "load_rule",
        "description": (
            "Load a specific rule's full content and metadata, including its generated hook IDs."
        ),
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
        "description": (
            "Create or update an automation rule. This is the ONLY way to "
            "create automation — never create hooks directly. Active rules with "
            "triggers automatically generate hooks that execute on schedule or "
            "in response to events. Passive rules influence reasoning without "
            "triggering actions. "
            "Include a # Title, ## Trigger (e.g. 'Check every 5 minutes' or "
            "'When a task is completed'), and ## Logic section in the content. "
            "IMPORTANT: Rules are for behavioral logic ONLY — do NOT use "
            "save_rule to store data, timestamps, or key-value state. Use "
            "write_memory/read_memory for persistent data storage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": ("Rule ID (auto-generated if omitted)"),
                },
                "project_id": {
                    "type": "string",
                    "description": ("Project ID (null = global rule visible to all projects)"),
                },
                "type": {
                    "type": "string",
                    "enum": ["active", "passive"],
                    "description": (
                        "Rule type: 'active' for triggered automation, "
                        "'passive' for reasoning guidance"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Rule content (markdown with # Title, ## Trigger, ## Logic sections)",
                },
            },
            "required": ["type", "content"],
        },
    },
    {
        "name": "delete_rule",
        "description": (
            "Remove an automation rule and all its generated hooks. "
            "This is the only way to remove automation — do not delete hooks directly."
        ),
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
        "name": "refresh_hooks",
        "description": (
            "Force reconciliation of all rules and their hooks. "
            "Re-reads all rule files, regenerates hooks for active rules, "
            "and cleans up orphaned hooks. Normally not needed — the file "
            "watcher auto-reconciles when rule files change on disk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # ------------------------------------------------------------------
    # Commands below were added to ensure ALL CommandHandler commands
    # have explicit MCP tool definitions with rich schemas.
    # ------------------------------------------------------------------
    {
        "name": "reload_config",
        "description": (
            "Manually trigger a config hot-reload from disk. Returns a summary "
            "of which sections changed, which were applied, and which require "
            "a daemon restart."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "claude_usage",
        "description": (
            "Get Claude Code usage stats from live session data. Computes real "
            "token usage by scanning active session JSONL files in "
            "~/.claude/projects/. Also reads subscription info from "
            "~/.claude/.credentials.json."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "archive_settings",
        "description": (
            "Return the current auto-archive configuration. Shows archive "
            "policy settings plus the count of currently archived tasks and "
            "how many terminal tasks are eligible right now."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "provide_input",
        "description": (
            "Provide a human reply to an agent question (WAITING_INPUT → READY). "
            "The agent's question is answered by appending the human's response "
            "to the task description so the agent sees it on re-execution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "ID of the task waiting for input",
                },
                "input": {
                    "type": "string",
                    "description": "The human's response text",
                },
            },
            "required": ["task_id", "input"],
        },
    },
    {
        "name": "set_task_status",
        "description": (
            "Directly set a task's status. Administrative override — use with "
            "care as it bypasses normal state machine transitions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "New status value (e.g. READY, DEFINED, COMPLETED, FAILED, BLOCKED)"
                    ),
                },
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": "task_deps",
        "description": (
            "Return upstream dependencies and downstream dependents for a task. "
            "Shows a focused dependency view with visual status for each "
            "related task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID to show dependencies for",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "set_project_channel",
        "description": (
            "Link an existing Discord channel to a project. "
            "Deprecated — prefer edit_project with discord_channel_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID to link",
                },
            },
            "required": ["project_id", "channel_id"],
        },
    },
    {
        "name": "set_control_interface",
        "description": (
            "Set a project's channel by channel name (string lookup). "
            "Resolves the channel name within the guild. "
            "Deprecated — prefer edit_project with discord_channel_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID or project name",
                },
                "channel_name": {
                    "type": "string",
                    "description": "Discord channel name to look up",
                },
            },
            "required": ["project_id", "channel_name"],
        },
    },
    # --- Agent management (deprecated — workspace model) ---
    {
        "name": "create_agent",
        "description": (
            "Deprecated — agents are now derived from workspaces. "
            "Use add_workspace to add agent capacity to a project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent name"},
                "project_id": {"type": "string", "description": "Project to assign to"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "edit_agent",
        "description": (
            "Deprecated — agents are now derived from workspaces. "
            "Use edit_project or workspace commands instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "delete_agent",
        "description": (
            "Deprecated — agents are now derived from workspaces. Use remove_workspace instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to delete"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "pause_agent",
        "description": ("Deprecated — agents are now derived from workspaces."),
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
        "description": ("Deprecated — agents are now derived from workspaces."),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent ID to resume"},
            },
            "required": ["agent_id"],
        },
    },
    # GitHub operations + convenience git commands (create_github_repo, generate_readme,
    # create_branch, checkout_branch, commit_changes, push_branch, merge_branch)
    # migrated to aq-git internal plugin.
    # --- Rule tools ---
    {
        "name": "browse_rules",
        "description": "List rules for a project (plus globals). Alias for list_rules.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (optional, defaults to active project)",
                },
            },
        },
    },
    {
        "name": "fire_rule",
        "description": "Manually trigger all hooks for a rule, ignoring cooldowns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Rule ID to fire"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "rule_runs",
        "description": (
            "Show recent execution history for a rule (aggregated across all its hooks)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Rule ID"},
                "limit": {
                    "type": "integer",
                    "description": "Number of runs to show (default 10)",
                    "default": 10,
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "toggle_rule",
        "description": "Enable or disable all hooks for a rule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Rule ID"},
                "enabled": {
                    "type": "boolean",
                    "description": "True to enable, false to disable",
                },
            },
            "required": ["id", "enabled"],
        },
    },
    {
        "name": "refresh_rules",
        "description": (
            "Force reconciliation of all rules and their hooks. Re-reads all "
            "rule files and regenerates hooks for active rules."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    # --- Communication ---
    {
        "name": "send_message",
        "description": (
            "Post a message to a Discord channel. Use this to notify users, "
            "post updates, or communicate outside the current conversation thread."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID to post to",
                },
                "content": {
                    "type": "string",
                    "description": "Message content to post",
                },
            },
            "required": ["channel_id", "content"],
        },
    },
    # --- Commands that are intentionally excluded by default but still
    #     need definitions so they can be un-excluded via config ---
    {
        "name": "shutdown",
        "description": (
            "Shut down the bot and all running agents. Excluded from MCP by default for safety."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_and_restart",
        "description": (
            "Pull the latest source from git and restart the daemon. "
            "Use wait_for_tasks=true to let running tasks finish before restarting. "
            "Excluded from MCP by default for safety."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the update/restart is being requested",
                },
                "wait_for_tasks": {
                    "type": "boolean",
                    "description": "If true, pause orchestrator and wait for running tasks to complete before restarting (up to 5 minutes). Default: false.",
                },
            },
        },
    },
    {
        "name": "browse_tools",
        "description": (
            "List available tool categories with metadata. "
            "Meta-tool for LLM context management — excluded from MCP by default."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "load_tools",
        "description": (
            "Load a tool category's definitions for the current interaction. "
            "Meta-tool for LLM context management — excluded from MCP by default."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category name to load (e.g. 'git', 'project')",
                },
            },
            "required": ["category"],
        },
    },
    # ------------------------------------------------------------------
    # Playbook commands (spec §15)
    # ------------------------------------------------------------------
    {
        "name": "compile_playbook",
        "description": (
            "Manually trigger compilation of a playbook markdown file. "
            "Provide the full markdown content (including YAML frontmatter) "
            "or a file path. Returns the compiled playbook metadata on "
            "success, or detailed errors on failure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "markdown": {
                    "type": "string",
                    "description": (
                        "Full playbook markdown content including YAML frontmatter. "
                        "Frontmatter must include: id, triggers (list), scope "
                        "(system|project|agent-type:xxx). Either this or 'path' "
                        "is required."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute path to a playbook .md file on disk. "
                        "If provided, the file is read and used as the markdown. "
                        "Either this or 'markdown' is required."
                    ),
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Force recompilation even if source is unchanged. "
                        "Defaults to true for manual compilation."
                    ),
                    "default": True,
                },
            },
        },
    },
    {
        "name": "show_playbook_graph",
        "description": (
            "Render a compiled playbook graph as an ASCII diagram or Mermaid "
            "flowchart syntax. Shows nodes (with type badges), edges, and "
            "transition conditions. Useful for understanding playbook structure "
            "and sharing visual documentation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "The playbook identifier to render",
                },
                "format": {
                    "type": "string",
                    "description": (
                        "Output format: 'ascii' for terminal/text output, "
                        "'mermaid' for Mermaid flowchart syntax. Default: ascii."
                    ),
                    "enum": ["ascii", "mermaid"],
                    "default": "ascii",
                },
                "direction": {
                    "type": "string",
                    "description": (
                        "Mermaid flowchart direction: 'TD' (top-down) or "
                        "'LR' (left-right). Only used with mermaid format. "
                        "Default: TD."
                    ),
                    "enum": ["TD", "LR"],
                    "default": "TD",
                },
                "show_prompts": {
                    "type": "boolean",
                    "description": (
                        "Include truncated prompt previews in node labels. "
                        "Default: true."
                    ),
                    "default": True,
                },
            },
            "required": ["playbook_id"],
        },
    },
    {
        "name": "list_playbooks",
        "description": (
            "List all playbooks across scopes with status, triggers, and last run info. "
            "Returns every active compiled playbook. Optionally filter by scope."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "Filter by scope type",
                    "enum": ["system", "project", "agent-type"],
                },
            },
        },
    },
    {
        "name": "list_playbook_runs",
        "description": (
            "List recent playbook runs with status and path taken through the graph. "
            "Each run includes a compact node trace showing visited nodes and their "
            "outcome. Filter by playbook_id and/or status "
            "(e.g. 'paused' to find runs awaiting human review). Returns newest first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "Filter to a specific playbook ID",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by run status: running, paused, completed, failed, timed_out"
                    ),
                    "enum": ["running", "paused", "completed", "failed", "timed_out"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default 20)",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "inspect_playbook_run",
        "description": (
            "Inspect a playbook run in detail. Returns full node trace "
            "(with per-node timing, transitions, and status), complete "
            "conversation history, token usage, and trigger event. "
            "Use list_playbook_runs first to find run IDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The playbook run ID to inspect",
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "resume_playbook",
        "description": (
            "Resume a paused (human-in-the-loop) playbook run. "
            "Provide your review decision or feedback as human_input — "
            "it will be injected into the conversation and the playbook "
            "will continue from where it paused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The playbook run ID to resume",
                },
                "human_input": {
                    "type": "string",
                    "description": (
                        "Your review decision or feedback text. This is added "
                        "to the conversation history and used to determine the "
                        "next transition."
                    ),
                },
            },
            "required": ["run_id", "human_input"],
        },
    },
]


class ToolRegistry:
    """Registry that categorizes tools into core and on-demand categories.

    Initialised with a list of tool definition dicts (JSON Schema format).
    Each tool is either "core" (always loaded) or belongs to a named
    category that can be loaded on demand via ``load_tools``.

    Usage::

        registry = ToolRegistry()
        core = registry.get_core_tools()          # always-on tools
        git  = registry.get_category_tools("git")  # on-demand category

    The registry is stateless — it doesn't track which categories are
    currently "loaded" in a conversation.  That state lives in the
    Supervisor's ``active_tools`` dict.

    Attributes:
        _all_tools: Mapping of tool name → tool definition dict.
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
        """Add browse_tools, load_tools, send_message, reply_to_user, and rule stubs if absent.

        These tools are synthesised at init time rather than being defined in
        ``_ALL_TOOL_DEFINITIONS`` because they need special handling in the
        Supervisor's tool-use loop (e.g. ``load_tools`` expands the active set,
        ``reply_to_user`` terminates the loop).
        """
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
                            "description": ("Category name to load (e.g. 'git', 'project')"),
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
                            "description": ("Discord channel ID to post to"),
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
        # Rule tools — primary automation interface
        for rule_tool in self._get_rule_tools():
            if rule_tool["name"] not in self._all_tools:
                self._all_tools[rule_tool["name"]] = rule_tool

    @staticmethod
    def _get_rule_tools() -> list[dict]:
        """Return tool definitions for the rule-based automation interface.

        Rules are the primary (and only) way to create automation. Hooks are
        internal execution artifacts generated from rules automatically.
        """
        return [
            {
                "name": "list_rules",
                "description": (
                    "List all automation rules for the current project and globals. "
                    "Rules are the ONLY way to create automation — each active rule "
                    "generates hooks that execute automatically. "
                    "Alias: browse_rules"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": ("Project ID (optional, defaults to active project)"),
                        },
                    },
                },
            },
            {
                "name": "load_rule",
                "description": (
                    "Load a specific rule's full content and metadata, "
                    "including its generated hook IDs."
                ),
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
                "description": (
                    "Create or update an automation rule. This is the ONLY way to "
                    "create automation — never create hooks directly. Active rules with "
                    "triggers automatically generate hooks that execute on schedule or "
                    "in response to events. Passive rules influence reasoning without "
                    "triggering actions. "
                    "Include a # Title, ## Trigger (e.g. 'Check every 5 minutes' or "
                    "'When a task is completed'), and ## Logic section in the content. "
                    "IMPORTANT: Rules are for behavioral logic ONLY — do NOT use "
                    "save_rule to store data, timestamps, or key-value state. Use "
                    "write_memory/read_memory for persistent data storage."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": ("Rule ID (auto-generated if omitted)"),
                        },
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Project ID (null = global rule visible to all projects)"
                            ),
                        },
                        "type": {
                            "type": "string",
                            "enum": ["active", "passive"],
                            "description": (
                                "Rule type: 'active' for triggered automation, "
                                "'passive' for reasoning guidance"
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": "Rule content (markdown with # Title, ## Trigger, ## Logic sections)",
                        },
                    },
                    "required": ["type", "content"],
                },
            },
            {
                "name": "delete_rule",
                "description": (
                    "Remove an automation rule and all its generated hooks. "
                    "This is the only way to remove automation — do not delete hooks directly."
                ),
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
                "name": "refresh_hooks",
                "description": (
                    "Force reconciliation of all rules and their hooks. "
                    "Re-reads all rule files, regenerates hooks for active rules, "
                    "and cleans up orphaned hooks. Normally not needed — the file "
                    "watcher auto-reconciles when rule files change on disk."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    # ------------------------------------------------------------------
    # Schema compression for small-context LLMs
    # ------------------------------------------------------------------

    @staticmethod
    def compress_tool_schema(tool: dict) -> dict:
        """Return a minimal version of a tool definition for small-context LLMs.

        Strips verbose descriptions down to short phrases and removes
        parameter descriptions where the name is self-explanatory.
        Keeps: name, compressed description, input_schema with types/required/enums.
        """
        compressed = {"name": tool["name"]}

        # Compress description to first sentence, max ~80 chars
        desc = tool.get("description", "")
        # Take first sentence
        for sep in [". ", ".\n", ".  "]:
            if sep in desc:
                desc = desc[: desc.index(sep) + 1]
                break
        # Truncate if still long
        if len(desc) > 80:
            desc = desc[:77] + "..."
        compressed["description"] = desc

        # Compress input_schema: keep types, required, enums; drop descriptions
        schema = tool.get("input_schema", {})
        if not schema.get("properties"):
            compressed["input_schema"] = {"type": "object", "properties": {}}
            return compressed

        compressed_props = {}
        for prop_name, prop_def in schema.get("properties", {}).items():
            if not isinstance(prop_def, dict):
                compressed_props[prop_name] = prop_def
                continue
            # Keep only structural info: type, enum, default, items
            compact = {}
            for key in ("type", "enum", "default", "items"):
                if key in prop_def:
                    compact[key] = prop_def[key]
            compressed_props[prop_name] = compact

        compressed_schema = {"type": "object", "properties": compressed_props}
        if "required" in schema:
            compressed_schema["required"] = schema["required"]
        compressed["input_schema"] = compressed_schema
        return compressed

    def _get_plugin_tools(self) -> dict[str, dict]:
        """Collect plugin-registered tools (keyed by name).

        Plugin tools with ``_category`` are included in category queries.
        Plugin tools are merged on top of built-in tools (plugin wins on
        name collision).
        """
        if not self._plugin_registry:
            return {}
        return {t["name"]: t for t in self._plugin_registry.get_all_tool_definitions()}

    def _tool_category(self, name: str, tool: dict) -> str | None:
        """Return the category a tool belongs to, or None if core."""
        # Plugin-declared category takes precedence
        cat = tool.get("_category")
        if cat:
            return cat
        # Fall back to hardcoded mapping
        return _TOOL_CATEGORIES.get(name)

    def get_core_tools(self, compressed: bool = False) -> list[dict]:
        """Return tool definitions that are always loaded.

        Args:
            compressed: If True, return minimal schemas for small-context LLMs.

        Returns:
            List of tool definition dicts for tools not assigned to any
            category (i.e. not present in ``_TOOL_CATEGORIES`` and without
            a ``_category`` tag from a plugin).
        """
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}
        tools = [t for name, t in merged.items() if self._tool_category(name, t) is None]
        if compressed:
            return [self.compress_tool_schema(t) for t in tools]
        return tools

    def get_categories(self) -> list[dict]:
        """Return category metadata list for ``browse_tools`` response.

        Returns:
            List of dicts with ``name``, ``description``, and ``tool_count``
            keys — one per registered category.
        """
        result = []
        for cat_name, meta in CATEGORIES.items():
            tools = self.get_category_tools(cat_name)
            result.append(
                {
                    "name": meta.name,
                    "description": meta.description,
                    "tool_count": len(tools) if tools else 0,
                }
            )
        return result

    def get_category_tools(
        self,
        category: str,
        compressed: bool = False,
    ) -> list[dict] | None:
        """Return all tool definitions for a category.

        Includes both hardcoded ``_TOOL_CATEGORIES`` entries and
        plugin-registered tools with matching ``_category``.

        Args:
            category: Category name (e.g. ``"git"``, ``"rules"``).
            compressed: If True, return minimal schemas for small-context LLMs.

        Returns:
            List of tool definition dicts, or ``None`` if the category
            name is not recognised.
        """
        if category not in CATEGORIES:
            return None

        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}

        tools = [t for name, t in merged.items() if self._tool_category(name, t) == category]
        if compressed:
            return [self.compress_tool_schema(t) for t in tools]
        return tools

    def get_tool_index(self) -> str:
        """Return a compact tool name index grouped by category.

        Lists all tool names (no descriptions or schemas) organized by
        category, one line per category.  Intended for injection into the
        supervisor system prompt so the LLM always knows which tools exist
        without calling ``browse_tools``.

        Returns:
            Markdown-formatted string, e.g.::

                **git**: git_status, git_commit, git_push, ...
                **memory**: memory_search, memory_stats, ...
        """
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}

        lines: list[str] = []
        for cat_name in CATEGORIES:
            names = sorted(
                name for name, t in merged.items() if self._tool_category(name, t) == cat_name
            )
            if names:
                lines.append(f"**{cat_name}**: {', '.join(names)}")
        return "\n".join(lines)

    def get_category_tool_names(self, category: str) -> list[str] | None:
        """Return tool names for a category.

        Args:
            category: Category name (e.g. ``"git"``, ``"rules"``).

        Returns:
            List of tool name strings, or ``None`` if the category
            name is not recognised.
        """
        if category not in CATEGORIES:
            return None

        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}

        return [name for name, t in merged.items() if self._tool_category(name, t) == category]

    def get_all_tools(self) -> list[dict]:
        """Return all tool definitions (core + all categories + plugins).

        Returns:
            List of every tool definition dict known to the registry,
            including any tools contributed by loaded plugins.
        """
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}
        return list(merged.values())

    # ------------------------------------------------------------------
    # Prompt-based tool search
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Tokenize text into lowercase words, splitting on underscores too.

        Filters out very short tokens (len < 3) and common stop words to
        reduce noise in keyword matching.
        """
        _STOP_WORDS = frozenset(
            {
                "the",
                "and",
                "for",
                "this",
                "that",
                "with",
                "from",
                "are",
                "was",
                "were",
                "been",
                "have",
                "has",
                "had",
                "not",
                "but",
                "can",
                "will",
                "all",
                "its",
                "use",
                "set",
                "get",
                "new",
                "one",
                "two",
                "any",
                "our",
                "you",
                "your",
            }
        )
        # Split on non-alphanumeric, underscores, and hyphens
        words = re.split(r"[^a-zA-Z0-9]+", text.lower())
        return {w for w in words if len(w) >= 3 and w not in _STOP_WORDS}

    def _tool_search_text(self, tool: dict) -> str:
        """Build searchable text from a tool definition.

        Combines the tool name (with underscores split into words) and
        the tool description into a single string for keyword matching.
        """
        name = tool.get("name", "")
        desc = tool.get("description", "")
        # Also include property names/descriptions from input_schema
        schema_parts: list[str] = []
        schema = tool.get("input_schema", {})
        for prop_name, prop_def in schema.get("properties", {}).items():
            schema_parts.append(prop_name)
            if isinstance(prop_def, dict) and "description" in prop_def:
                schema_parts.append(prop_def["description"])
        return f"{name} {desc} {' '.join(schema_parts)}"

    def search_relevant_categories(
        self,
        query: str,
        max_categories: int = 3,
        min_score: float = 0.15,
    ) -> list[str]:
        """Search tool definitions and return categories relevant to a query.

        Scores each non-core tool against the query using keyword overlap
        between the query tokens and the tool's name + description + schema.
        Categories are ranked by a composite of their best-matching tool's
        score (primary) and the sum of all tool scores (tiebreaker).

        Args:
            query: The user's prompt or search query.
            max_categories: Maximum number of categories to return.
            min_score: Minimum score threshold (0-1) for a category to be
                included. Categories whose best tool score falls below this
                are excluded.

        Returns:
            List of category names, ordered by relevance (best first).
            May be empty if no categories score above ``min_score``.
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        # Score each categorized tool, track best and sum per category
        category_best: dict[str, float] = {}
        category_sum: dict[str, float] = {}

        # Merge built-in categories with plugin tools
        plugin_tools = self._get_plugin_tools()
        merged = {**self._all_tools, **plugin_tools}

        for tool_name, tool in merged.items():
            category = self._tool_category(tool_name, tool)
            if not category:
                continue
            tool_tokens = self._tokenize(self._tool_search_text(tool))
            if not tool_tokens:
                continue

            # Score: fraction of query tokens found in tool text
            matches = query_tokens & tool_tokens
            score = len(matches) / len(query_tokens)

            if score > 0:
                category_sum[category] = category_sum.get(category, 0.0) + score
                if score > category_best.get(category, 0.0):
                    category_best[category] = score

        # Rank by (best_score, sum_score) so ties are broken by breadth
        ranked = sorted(
            category_best.items(),
            key=lambda x: (x[1], category_sum.get(x[0], 0.0)),
            reverse=True,
        )
        return [cat for cat, score in ranked[:max_categories] if score >= min_score]
