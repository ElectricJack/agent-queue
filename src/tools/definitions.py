"""Tool definitions and category mappings.

Contains the raw tool definition dicts and category mapping tables.
These are pure data — no logic.
"""

from __future__ import annotations

# Which category each tool belongs to.
# Tools not listed here are "core" (always loaded).
_TOOL_CATEGORIES: dict[str, str] = {
    # git — migrated to aq-git internal plugin (src/plugins/internal/git.py)
    # project
    "list_projects": "project",
    "create_project": "project",
    "pause_project": "project",
    "resume_project": "project",
    "set_project_constraint": "project",
    "release_project_constraint": "project",
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
    # vault — reference stub management
    "scan_stub_staleness": "system",
    # memory — provided by the external aq-memory plugin (install via `aq plugin install`)
    # notes — migrated to aq-notes internal plugin (src/plugins/internal/notes.py)
    # files — migrated to aq-files internal plugin (src/plugins/internal/files.py)
    # task — lifecycle, approval, dependencies, archives, results
    "stop_task": "task",
    "restart_task": "task",
    "reopen_with_feedback": "task",
    "delete_task": "task",
    "skip_task": "task",
    "set_task_status": "task",
    "approve_task": "task",
    "process_task_completion": "task",
    "approve_plan": "task",
    "reject_plan": "task",
    "delete_plan": "task",
    "process_plan": "task",
    "archive_task": "task",
    "archive_settings": "task",
    "list_archived": "task",
    "get_task_result": "task",
    "get_task_tree": "task",
    "get_task_dependencies": "task",
    "task_deps": "task",
    "add_dependency": "task",
    "remove_dependency": "task",
    "get_chain_health": "task",
    "list_active_tasks_all_projects": "task",
    # playbook — compilation, run management, human-in-the-loop resume
    "compile_playbook": "playbook",
    "run_playbook": "playbook",
    "dry_run_playbook": "playbook",
    "show_playbook_graph": "playbook",
    "list_playbooks": "playbook",
    "list_playbook_runs": "playbook",
    "inspect_playbook_run": "playbook",
    "resume_playbook": "playbook",
    "recover_workflow": "playbook",
    "playbook_health": "playbook",
    "playbook_graph_view": "playbook",
    "get_playbook_source": "playbook",
    "update_playbook_source": "playbook",
    "create_playbook": "playbook",
    "delete_playbook": "playbook",
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
    "list_event_triggers": "system",
    "read_logs": "system",
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
    "get_stuck_tasks": "system",
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
        "name": "set_project_constraint",
        "description": (
            "Set a temporary scheduling constraint on a project. Supports exclusive access "
            "(only one agent at a time), per-agent-type concurrency limits, and pausing all "
            "scheduling. Constraints stack: calling this on a project that already has a "
            "constraint merges the new fields with the existing ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "exclusive": {
                    "type": "boolean",
                    "description": (
                        "If true, only one agent may work on the project at a time "
                        "(overrides max_concurrent_agents to 1)."
                    ),
                },
                "max_agents_by_type": {
                    "type": "object",
                    "description": (
                        "Per-agent-type concurrency limits, e.g. "
                        '{"claude": 2, "codex": 1}. Agent types not listed are unrestricted.'
                    ),
                    "additionalProperties": {"type": "integer"},
                },
                "pause_scheduling": {
                    "type": "boolean",
                    "description": (
                        "If true, the scheduler skips this project entirely — "
                        "no new tasks are assigned until the constraint is released."
                    ),
                },
                "created_by": {
                    "type": "string",
                    "description": (
                        "Identifier of who/what set the constraint "
                        "(e.g. workflow ID, admin name). Informational only."
                    ),
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "release_project_constraint",
        "description": (
            "Release (remove) a scheduling constraint from a project. "
            "If specific fields are provided, only those fields are cleared; "
            "otherwise the entire constraint is removed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Specific constraint fields to release: "
                        "'exclusive', 'pause_scheduling', 'max_agents_by_type'. "
                        "If omitted, the entire constraint is removed."
                    ),
                },
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
                "agent_type": {
                    "type": "string",
                    "description": (
                        "Type of agent needed for this task (e.g. 'coding', "
                        "'code-review', 'qa'). Used by coordination playbooks "
                        "to match tasks with appropriately-typed agents."
                    ),
                },
                "affinity_agent_id": {
                    "type": "string",
                    "description": (
                        "Preferred agent ID for context continuity. The scheduler "
                        "will prefer this agent when assigning the task, but will "
                        "fall back to any available agent if the preferred one is busy."
                    ),
                },
                "affinity_reason": {
                    "type": "string",
                    "enum": ["context", "workspace", "type"],
                    "description": (
                        "Why this agent is preferred: 'context' (has relevant "
                        "conversation history), 'workspace' (already has the "
                        "workspace locked), 'type' (matches the required agent type)."
                    ),
                },
                "workspace_mode": {
                    "type": "string",
                    "enum": ["exclusive", "branch-isolated", "directory-isolated"],
                    "description": (
                        "Workspace lock mode. 'exclusive' (default): one agent per "
                        "workspace. 'branch-isolated': multiple agents on separate "
                        "branches in the same repo. 'directory-isolated': multiple "
                        "agents on separate directories (future)."
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
            "skip_verification, agent_type, affinity_agent_id, affinity_reason, "
            "or workspace_mode. Use this "
            "to move a task to a different project, rename tasks, change priority, override status "
            "(admin), assign a profile, adjust retry/verification settings, or set coordination "
            "parameters."
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
                "agent_type": {
                    "type": ["string", "null"],
                    "description": (
                        "Type of agent needed (e.g. 'coding', 'code-review', 'qa'). "
                        "Set to null to clear (optional)"
                    ),
                },
                "affinity_agent_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Preferred agent ID for context continuity. Set to null to clear (optional)"
                    ),
                },
                "affinity_reason": {
                    "type": ["string", "null"],
                    "enum": ["context", "workspace", "type", None],
                    "description": ("Why this agent is preferred. Set to null to clear (optional)"),
                },
                "workspace_mode": {
                    "type": ["string", "null"],
                    "enum": ["exclusive", "branch-isolated", "directory-isolated", None],
                    "description": ("Workspace lock mode. Set to null to clear (optional)"),
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
        "name": "archive_task",
        "description": (
            "Archive tasks. Provide task_id to archive a single task, or "
            "project_id to bulk-archive all completed tasks in a project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": (
                        "Archive a single task by ID (must be COMPLETED, FAILED, or BLOCKED)"
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": (
                        "Bulk-archive all completed tasks in this project (alternative to task_id)"
                    ),
                },
                "include_failed": {
                    "type": "boolean",
                    "description": (
                        "When bulk-archiving by project_id, also archive "
                        "FAILED and BLOCKED tasks. Default false."
                    ),
                },
            },
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
        "name": "list_event_triggers",
        "description": (
            "List event types that are valid playbook triggers, grouped by category "
            "(e.g. 'git', 'task', 'file'). Excludes 'notify.*' transport events and "
            "dynamically-generated 'timer.*' / 'cron.*' types (UIs should offer those "
            "via a dedicated picker). Intended for trigger-picker components in the "
            "dashboard and CLI."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_events",
        "description": (
            "Get recent system events (task completions, failures, agent questions, "
            "budget warnings, etc.) from the event database. Supports filtering by "
            "event type, time range, project, agent, or task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return (default 10).",
                    "default": 10,
                },
                "event_type": {
                    "type": "string",
                    "description": (
                        "Filter by event type. Exact match or prefix with '*' "
                        "(e.g. 'task.*' matches task.started, task.completed, etc.)."
                    ),
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Only return events newer than this relative time. "
                        "Accepts durations like '5m', '1h', '2d', '30s'."
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": "Filter by project ID.",
                },
                "agent_id": {
                    "type": "string",
                    "description": "Filter by agent ID.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Filter by task ID.",
                },
            },
        },
    },
    {
        "name": "read_logs",
        "description": (
            "Read and filter the daemon's structured JSONL log file. Returns parsed "
            "log entries with severity, timestamps, and context fields. Use this to "
            "inspect detailed system behavior, diagnose errors, or analyze "
            "component-level activity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "description": (
                        "Minimum log severity level. Only entries at or above this "
                        "level are returned."
                    ),
                    "enum": ["debug", "info", "warning", "error", "critical"],
                    "default": "info",
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Only return log entries newer than this relative time. "
                        "Accepts durations like '5m', '1h', '2d', '30s'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": ("Maximum number of log entries to return (default 100)."),
                    "default": 100,
                },
                "component": {
                    "type": "string",
                    "description": (
                        "Filter by component name (e.g. 'orchestrator', 'supervisor', "
                        "'api', 'hooks', 'discord')."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": "Filter by task ID.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Filter by project ID.",
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Substring search in the log message/event field (case-insensitive)."
                    ),
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
        "name": "get_stuck_tasks",
        "description": (
            "Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their "
            "per-status threshold.  Detection and time arithmetic run in "
            "the database — the caller passes thresholds and a reference "
            "``now`` timestamp and receives a structured list back.  "
            "Defaults match the system-health-check playbook's stuck "
            "definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  "
            "Each entry carries ``id``, ``project_id``, ``status``, "
            "``assigned_agent``, ``updated_at``, and ``seconds_in_state`` "
            "so remediation (``restart_task`` vs "
            "``set_task_status(..., status=\"READY\")``) can branch on "
            "the agent state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "assigned_threshold_seconds": {
                    "type": "integer",
                    "description": (
                        "Max seconds a task may stay ASSIGNED before being "
                        "flagged as stuck.  Default 1800 (30 minutes)."
                    ),
                    "default": 1800,
                },
                "in_progress_threshold_seconds": {
                    "type": "integer",
                    "description": (
                        "Max seconds a task may stay IN_PROGRESS before "
                        "being flagged as stuck.  Default 7200 (2 hours)."
                    ),
                    "default": 7200,
                },
                "now": {
                    "type": "number",
                    "description": (
                        "Reference Unix timestamp (seconds since epoch).  "
                        "Playbooks should pass the trigger event's "
                        "``tick_time`` so repeated runs are deterministic.  "
                        "Defaults to the server's current time."
                    ),
                },
                "project_id": {
                    "type": "string",
                    "description": (
                        "Optional project filter.  When omitted, all "
                        "projects are scanned."
                    ),
                },
            },
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
    # Hook and rule tool definitions removed (playbooks spec §13 Phase 3).
    # All automation is now managed through playbooks.
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
            "Pass either (project_id, name) for a project-scoped template, "
            "or path=<absolute path> for a bundled template that ships "
            "with the daemon (in playbooks, use aq://prompts/<path> — compiler rewrites)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (required unless path is set)",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Template name from list_prompts (e.g. 'plan-generation'), "
                        "or the filename (e.g. 'plan-generation.md'). Required "
                        "unless path is set."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute filesystem path to a bundled template, e.g. "
                        "'/opt/agent-queue/src/prompts/consolidation_task.md'. In "
                        "playbook authoring, use aq://prompts/<name>.md instead — "
                        "the playbook compiler rewrites it to an absolute path. "
                        "Mutually exclusive with (project_id, name)."
                    ),
                },
            },
        },
    },
    {
        "name": "render_prompt",
        "description": (
            "Render a prompt template with variable substitution. Replaces "
            "{{variable}} placeholders with provided values. Returns the "
            "fully rendered prompt text. Pass either (project_id, name) for a "
            "project-scoped template, or path=<absolute path> for a bundled "
            "template (in playbooks, use aq://prompts/<path> — compiler rewrites)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID (required unless path is set)",
                },
                "name": {
                    "type": "string",
                    "description": "Template name to render (required unless path is set)",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute filesystem path to a bundled template, e.g. "
                        "'/opt/agent-queue/src/prompts/consolidation_task.md'. In "
                        "playbook authoring, use aq://prompts/<name>.md instead — "
                        "the playbook compiler rewrites it to an absolute path. "
                        "Mutually exclusive with (project_id, name)."
                    ),
                },
                "variables": {
                    "type": "object",
                    "description": (
                        "Key-value pairs for template variables "
                        '(e.g. {"task_title": "Fix login bug"})'
                    ),
                },
            },
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
    # ------------------------------------------------------------------
    # Vault / reference stub management (Roadmap 6.3.4)
    # ------------------------------------------------------------------
    {
        "name": "scan_stub_staleness",
        "description": (
            "Scan vault reference stubs to detect staleness.  Compares each "
            "stub's recorded source_hash against the current source file on "
            "disk.  Reports stubs that are stale (source changed), missing "
            "(source deleted), unenriched (placeholder content), or orphaned "
            "(no source metadata).  Use to audit reference stub health before "
            "triggering re-enrichment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": (
                        "Project ID to scan.  If omitted, scans all projects "
                        "that have reference stubs in the vault."
                    ),
                },
            },
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
    # --- Communication ---
    {
        "name": "get_system_channel",
        "description": (
            "Resolve a system-level Discord channel by its config key "
            "(e.g. 'notifications', 'control', 'agent_questions') and return "
            "its channel_id. Use this when a playbook or system-scope task "
            "needs to post to a named channel from config without hardcoding "
            "a channel id. Pass the returned channel_id to send_message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Config key under discord.channels (e.g. "
                        "'notifications', 'control', 'agent_questions')"
                    ),
                },
            },
            "required": ["name"],
        },
    },
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
        "name": "find_applicable_tool",
        "description": (
            "Search for tools by describing what you want to do. Returns "
            "the most relevant tools ranked by semantic similarity. "
            "Example: 'create a new task for a project' → create_task, "
            "edit_task, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": ("Natural language description of what you want to do"),
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default 5)",
                },
            },
            "required": ["description"],
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
                        "(system|project|agent-type:xxx). One of 'markdown', "
                        "'path', or 'playbook_id' is required."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute path to a playbook .md file on disk. "
                        "If provided, the file is read and used as the markdown."
                    ),
                },
                "playbook_id": {
                    "type": "string",
                    "description": (
                        "ID of an already-compiled playbook. Resolves to its "
                        "source path via the playbook manager and recompiles it. "
                        "Use this to recompile by ID without remembering the "
                        "vault path. One of 'markdown', 'path', or 'playbook_id' "
                        "is required."
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
        "name": "run_playbook",
        "description": (
            "Manually trigger a playbook run. Executes the full compiled "
            "playbook graph from entry to terminal with real LLM calls, "
            "database persistence, and event emission. Use this to start "
            "a playbook on demand rather than waiting for its configured "
            "trigger (timer, event, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "The compiled playbook ID to execute",
                },
                "event": {
                    "type": "object",
                    "description": (
                        "Trigger event data to seed the run. Defaults to "
                        '{"type": "manual"} if not provided. '
                        "Include fields your playbook expects "
                        "(e.g. project_id, task_id) for context."
                    ),
                },
            },
            "required": ["playbook_id"],
        },
    },
    {
        "name": "dry_run_playbook",
        "description": (
            "Simulate playbook execution with a mock event, producing no side "
            "effects. Walks the graph from entry to terminal without real LLM "
            "calls, DB writes, or event emission. Returns the node trace "
            "showing the path that would be taken. Useful for testing and "
            "validating playbook design before deploying."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "The compiled playbook ID to simulate",
                },
                "event": {
                    "type": "object",
                    "description": (
                        "Mock trigger event data. Defaults to "
                        '{"type": "dry_run"} if not provided. '
                        "Include fields your playbook expects "
                        "(e.g. project_id, task_id) for realistic simulation."
                    ),
                },
            },
            "required": ["playbook_id"],
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
                        "Include truncated prompt previews in node labels. Default: true."
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
            "Returns every active compiled playbook. Optionally filter by scope. "
            "When project_id is provided, project-scoped playbooks belonging to a "
            "different project are excluded; system and agent-type scoped playbooks "
            "are always included because they apply across projects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "Filter by scope type",
                    "enum": ["system", "project", "agent-type"],
                },
                "project_id": {
                    "type": "string",
                    "description": (
                        "Restrict project-scoped playbooks to this project. "
                        "System and agent-type playbooks are still returned."
                    ),
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
    {
        "name": "recover_workflow",
        "description": (
            "Recover an orphaned coordination workflow whose playbook run "
            "has died (crashed, failed, timed out). If the playbook was "
            "paused waiting for stage completion and all tasks are done, "
            "re-emits the missed event to resume the playbook. If the "
            "playbook run failed, emits a workflow.orphaned event for "
            "manual intervention. Tasks in the workflow continue executing "
            "independently regardless of playbook state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workflow_id": {
                    "type": "string",
                    "description": "The workflow ID to recover",
                },
            },
            "required": ["workflow_id"],
        },
    },
    {
        "name": "playbook_health",
        "description": (
            "Compute health metrics for playbook runs: tokens per node, "
            "run duration statistics, transition paths, and failure rates. "
            "Returns a comprehensive report with per-node metrics (avg duration, "
            "token usage, failure rate), run duration percentiles (p50, p95), "
            "most common paths through the graph, and failure analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "Filter to a specific playbook ID. Omit for all playbooks.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by run status: running, paused, completed, failed, timed_out."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max runs to analyse (default 200).",
                    "default": 200,
                },
            },
        },
    },
    {
        "name": "playbook_graph_view",
        "description": (
            "Get structured graph view data for dashboard rendering of a playbook. "
            "Returns nodes as positioned boxes (color-coded by type), transitions "
            "as labelled arrows, with optional overlays for live state (current node "
            "highlighting for running instances), run path highlighting, and per-node "
            "health metrics. Suitable for interactive visualization."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "The playbook identifier to visualize.",
                },
                "direction": {
                    "type": "string",
                    "description": (
                        "Layout direction: 'TD' (top-down) or 'LR' (left-right). Default: TD."
                    ),
                    "enum": ["TD", "LR"],
                    "default": "TD",
                },
                "show_prompts": {
                    "type": "boolean",
                    "description": (
                        "Include truncated prompt previews in node labels. Default: true."
                    ),
                    "default": True,
                },
                "run_id": {
                    "type": "string",
                    "description": (
                        "Overlay a specific run's path on the graph. Shows which nodes "
                        "were visited, timing, and token usage per node."
                    ),
                },
                "include_live_state": {
                    "type": "boolean",
                    "description": (
                        "Include live state overlay for running/paused instances. "
                        "Highlights the current node. Default: true."
                    ),
                    "default": True,
                },
                "include_metrics": {
                    "type": "boolean",
                    "description": (
                        "Include per-node health metrics overlay (failure rate, avg "
                        "duration, token usage). Default: false."
                    ),
                    "default": False,
                },
                "include_history": {
                    "type": "boolean",
                    "description": (
                        "Include run history timeline showing past runs and paths "
                        "taken. Default: false."
                    ),
                    "default": False,
                },
                "history_limit": {
                    "type": "integer",
                    "description": "Max runs in the history timeline (default 20).",
                    "default": 20,
                },
            },
            "required": ["playbook_id"],
        },
    },
    {
        "name": "get_playbook_source",
        "description": (
            "Return the raw markdown of a playbook plus its content hash. "
            "Used by the dashboard to load a playbook for editing; the hash "
            "is sent back on save for optimistic-concurrency conflict detection."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "The playbook identifier.",
                },
            },
            "required": ["playbook_id"],
        },
    },
    {
        "name": "update_playbook_source",
        "description": (
            "Write new playbook markdown to the vault atomically and compile "
            "synchronously. On successful compile returns the new version; on "
            "validation failure returns 'errors' with previous compiled version "
            "still live. If 'expected_source_hash' is supplied and does not match "
            "the current vault copy, returns a conflict error (vault changed underneath)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "The playbook identifier.",
                },
                "markdown": {
                    "type": "string",
                    "description": "Full markdown content including YAML frontmatter.",
                },
                "expected_source_hash": {
                    "type": "string",
                    "description": (
                        "Content hash the caller last saw (from get_playbook_source). "
                        "When provided, the update is rejected with a conflict error if "
                        "the vault copy has changed underneath."
                    ),
                },
            },
            "required": ["playbook_id", "markdown"],
        },
    },
    {
        "name": "create_playbook",
        "description": (
            "Create a new playbook markdown file in the vault at the scope-appropriate "
            "location. Does NOT compile — authors iterate on the source and compile "
            "explicitly via update_playbook_source (or let the vault watcher pick it up). "
            "Fails if a playbook with the same id already exists anywhere in the vault."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "The new playbook identifier (used as filename without .md).",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Where the file lives on disk: 'system', 'project:<project_id>', "
                        "or 'agent-type:<type>'. The frontmatter scope field takes the "
                        "bare form ('system' / 'project' / 'agent-type:<type>') because "
                        "the project id is recovered from the vault path."
                    ),
                },
                "markdown": {
                    "type": "string",
                    "description": "Full markdown content including YAML frontmatter.",
                },
            },
            "required": ["playbook_id", "scope", "markdown"],
        },
    },
    {
        "name": "delete_playbook",
        "description": (
            "Archive a playbook's source file to vault/trash/playbooks/ and remove it "
            "from the active registry. Historical playbook_runs rows are preserved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "playbook_id": {
                    "type": "string",
                    "description": "The playbook identifier to delete.",
                },
            },
            "required": ["playbook_id"],
        },
    },
]
