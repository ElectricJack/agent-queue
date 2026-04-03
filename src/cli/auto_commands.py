"""Auto-generate Click commands from tool definitions, organized by category.

Commands are mounted into their tool_registry category's CLI group.  If a
hand-crafted group already exists (e.g., ``aq task``, ``aq hook``),
auto-generated commands merge into it.  Otherwise a new group is created
(e.g., ``aq git``, ``aq memory``, ``aq file``).

Category prefixes are stripped from command names for cleaner UX:
``git_commit`` becomes ``aq git commit``, ``memory_search`` becomes
``aq memory search``.

Tool definitions are imported from ``src.tool_registry._ALL_TOOL_DEFINITIONS``
(a pure data structure, no heavy deps) so commands appear in ``--help``
even when the daemon is down.  Execution still goes through the REST API.
"""

from __future__ import annotations

from collections import defaultdict

import click
from rich.console import Console

from src.tool_registry import (
    CATEGORIES,
    _ALL_TOOL_DEFINITIONS,
    _CLI_CATEGORY_OVERRIDES,
    _TOOL_CATEGORIES,
)

# CommandHandler commands covered by hand-crafted CLI commands.
# Auto-generation skips these to avoid duplicates.
HANDCRAFTED_COVERAGE = {
    # app.py
    "get_status",
    # tasks.py
    "list_tasks", "get_task", "create_task", "approve_task",
    "stop_task", "restart_task",
    # agents.py
    "list_agents",
    # hooks.py
    "list_hooks", "list_hook_runs",
    # projects.py
    "list_projects", "edit_project", "set_default_branch", "set_project_channel",
    # plugins.py (all plugin commands are hand-crafted with direct-DB access)
    "plugin_list", "plugin_info", "plugin_install", "plugin_update",
    "plugin_remove", "plugin_enable", "plugin_disable", "plugin_reload",
    "plugin_config", "plugin_prompts", "plugin_reset_prompts",
}

# Commands to exclude entirely from the CLI (dangerous or irrelevant).
EXCLUDED = {
    "shutdown", "restart_daemon", "update_and_restart",
    "run_command",
    "browse_tools", "load_tools",
    # Core messaging tools — not useful from CLI
    "send_message", "reply_to_user",
}

# Map tool_registry category names → CLI group names.
# Singular forms where the hand-crafted group already uses singular.
CATEGORY_CLI_NAMES: dict[str, str] = {
    "task": "task",
    "project": "project",
    "agent": "agent",
    "hooks": "hook",
    "plugin": "plugin",
    "git": "git",
    "memory": "memory",
    "files": "file",
    "system": "system",
}

# Human-readable group descriptions for newly created groups.
CATEGORY_CLI_DESCRIPTIONS: dict[str, str] = {
    "git": "Git operations — branch, commit, push, PR, merge.",
    "memory": "Memory, notes, and project profiles.",
    "file": "File operations — read, write, edit, glob, grep.",
    "system": "System diagnostics, config, and prompt management.",
}


def _schema_to_click_type(prop_schema: dict) -> type | click.Choice | None:
    """Map a JSON Schema property to a Click parameter type."""
    if "enum" in prop_schema:
        return click.Choice(prop_schema["enum"], case_sensitive=False)

    schema_type = prop_schema.get("type", "string")
    if schema_type == "integer":
        return int
    elif schema_type == "number":
        return float
    elif schema_type == "boolean":
        return bool
    elif schema_type == "string":
        return str
    return str


def _strip_category_prefix(cmd_name: str, category: str) -> str:
    """Strip category name from a command name for cleaner CLI UX.

    Handles prefixes, suffixes, and singular/plural variants::

        git_commit    (git)    → commit
        memory_search (memory) → search
        compact_memory(memory) → compact
        archive_task  (task)   → archive
        get_task_result(task)  → get-result
        list_hook_runs(hooks)  → list-runs

    Falls back to the full name if stripping would leave nothing useful.
    """
    singular = category.rstrip("s")

    # Try prefix: git_commit → commit, plugin_list → list
    for pfx in (f"{category}_", f"{singular}_"):
        if cmd_name.startswith(pfx) and len(cmd_name) > len(pfx):
            return cmd_name[len(pfx):]

    # Try suffix: compact_memory → compact, archive_task → archive
    for sfx in (f"_{category}", f"_{singular}"):
        if cmd_name.endswith(sfx) and len(cmd_name) > len(sfx):
            return cmd_name[: -len(sfx)]

    # Try infix: get_task_result → get_result, list_hook_runs → list_runs
    for infix in (f"_{category}_", f"_{singular}_"):
        if infix in cmd_name:
            return cmd_name.replace(infix, "_", 1)

    return cmd_name


def _make_auto_command(
    cmd_name: str,
    cli_name: str,
    tool_def: dict,
    console: Console,
) -> click.Command:
    """Build a Click command from a tool definition."""
    input_schema = tool_def.get("input_schema", {})
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    params: list[click.Parameter] = []
    for prop_name, prop_schema in properties.items():
        click_type = _schema_to_click_type(prop_schema)
        description = prop_schema.get("description", "")
        option_name = f"--{prop_name.replace('_', '-')}"

        if isinstance(click_type, type) and click_type is bool:
            params.append(click.Option(
                [option_name + "/--no-" + prop_name.replace("_", "-")],
                default=None,
                help=description,
            ))
        else:
            params.append(click.Option(
                [option_name],
                type=click_type,
                required=prop_name in required,
                default=None,
                help=description,
            ))

    def _make_callback(name: str):
        from .app import _get_client, _handle_errors, _run

        @_handle_errors
        @click.pass_context
        def callback(ctx, **kwargs):
            api_url = ctx.obj.get("api_url") if ctx.obj else None
            args = {
                k.replace("-", "_"): v
                for k, v in kwargs.items()
                if v is not None
            }

            async def _exec():
                async with _get_client(api_url) as client:
                    return await client.execute(name, args)

            result = _run(_exec())
            console.print_json(data=result)

        return callback

    return click.Command(
        name=cli_name.replace("_", "-"),
        callback=_make_callback(cmd_name),
        params=params,
        help=tool_def.get("description", f"Execute the {cmd_name} command."),
    )


def register_auto_commands(cli_group: click.Group, console: Console) -> None:
    """Register auto-generated commands into category-based CLI groups.

    For each tool_registry category, either merges into an existing
    hand-crafted group (task, hook, project, agent, plugin) or creates
    a new one (git, memory, file, system).
    """
    # Build complete tool map: explicit defs + auto-discovered
    tool_map: dict[str, dict] = {t["name"]: t for t in _ALL_TOOL_DEFINITIONS}
    try:
        from src.mcp_registration import _discover_all_commands
        discovered = _discover_all_commands()
        for name, defn in discovered.items():
            if name not in tool_map:
                tool_map[name] = defn
    except Exception:
        pass

    # Group tools by category
    category_tools: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    categorized_names: set[str] = set()

    for cmd_name, defn in tool_map.items():
        if cmd_name in HANDCRAFTED_COVERAGE or cmd_name in EXCLUDED:
            continue
        # Determine category: check _TOOL_CATEGORIES, then _CLI_CATEGORY_OVERRIDES
        cat = _TOOL_CATEGORIES.get(cmd_name) or _CLI_CATEGORY_OVERRIDES.get(cmd_name)
        if cat:
            category_tools[cat].append((cmd_name, defn))
            categorized_names.add(cmd_name)

    # Register commands into each category's CLI group
    for cat_name in sorted(CATEGORIES.keys()):
        cli_name = CATEGORY_CLI_NAMES.get(cat_name, cat_name)
        tools = category_tools.get(cat_name, [])
        if not tools:
            continue

        # Check if a hand-crafted group already exists
        existing_group = cli_group.commands.get(cli_name)
        if existing_group and isinstance(existing_group, click.Group):
            target_group = existing_group
        else:
            # Create a new group for this category
            desc = CATEGORY_CLI_DESCRIPTIONS.get(
                cli_name,
                CATEGORIES[cat_name].description,
            )

            @click.group(cli_name, help=desc)
            def new_group():
                pass

            cli_group.add_command(new_group, cli_name)
            target_group = new_group

        # Add auto-generated commands to this group
        for cmd_name, defn in sorted(tools):
            stripped = _strip_category_prefix(cmd_name, cat_name)
            click_name = stripped.replace("_", "-")

            # Avoid collision with existing commands in the group
            if hasattr(target_group, "commands") and click_name in target_group.commands:
                continue

            try:
                auto_cmd = _make_auto_command(cmd_name, stripped, defn, console)
                target_group.add_command(auto_cmd)
            except Exception:
                pass

    # Handle uncategorized commands that aren't excluded or hand-crafted
    # (safety net for commands missing from _TOOL_CATEGORIES)
    uncategorized = []
    for cmd_name, defn in tool_map.items():
        if cmd_name in categorized_names:
            continue
        if cmd_name in HANDCRAFTED_COVERAGE or cmd_name in EXCLUDED:
            continue
        uncategorized.append((cmd_name, defn))

    if uncategorized:
        # Put uncategorized commands into the system group as fallback
        system_group = cli_group.commands.get("system")
        if system_group and isinstance(system_group, click.Group):
            for cmd_name, defn in sorted(uncategorized):
                click_name = cmd_name.replace("_", "-")
                if hasattr(system_group, "commands") and click_name in system_group.commands:
                    continue
                try:
                    auto_cmd = _make_auto_command(cmd_name, cmd_name, defn, console)
                    system_group.add_command(auto_cmd)
                except Exception:
                    pass
