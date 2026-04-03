"""Auto-generate Click commands from tool definitions.

Every CommandHandler command that lacks a hand-crafted CLI equivalent
gets a generated Click command under ``aq cmd <command-name>``.  This
means new ``_cmd_*`` methods added to CommandHandler are instantly
available in the CLI with ``--help``, typed flags, and enums — zero
manual work required.

Tool definitions are imported from ``src.tool_registry._ALL_TOOL_DEFINITIONS``
(a pure data structure, no heavy deps) so commands appear in ``--help``
even when the daemon is down.  Execution still goes through the REST API.
"""

from __future__ import annotations

import click
from rich.console import Console

from src.tool_registry import _ALL_TOOL_DEFINITIONS

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
}

# Commands to exclude entirely from the CLI (dangerous or irrelevant).
EXCLUDED = {
    "shutdown", "restart_daemon", "update_and_restart",
    "run_command",
    "browse_tools", "load_tools",
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


def _make_auto_command(
    cmd_name: str,
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
        from .app import _run, _get_client, _handle_errors

        @_handle_errors
        @click.pass_context
        def callback(ctx, **kwargs):
            api_url = ctx.obj.get("api_url") if ctx.obj else None
            # Strip None values (unset flags)
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
        name=cmd_name.replace("_", "-"),
        callback=_make_callback(cmd_name),
        params=params,
        help=tool_def.get("description", f"Execute the {cmd_name} command."),
    )


def register_auto_commands(cli_group: click.Group, console: Console) -> None:
    """Register auto-generated commands under ``aq cmd <name>``."""

    @cli_group.group()
    def cmd() -> None:
        """All CommandHandler commands (auto-generated).

        These commands map directly to the daemon's CommandHandler. Use
        --help on any subcommand to see its parameters.
        """
        pass

    # Build the set of tool definitions to auto-generate
    tool_map = {t["name"]: t for t in _ALL_TOOL_DEFINITIONS}

    # Also include auto-discovered commands (those without explicit defs)
    # by using the same discovery mechanism as the MCP server.
    try:
        from src.mcp_registration import _discover_all_commands
        discovered = _discover_all_commands()
        for name, defn in discovered.items():
            if name not in tool_map:
                tool_map[name] = defn
    except Exception:
        pass  # Graceful degradation

    for name, defn in sorted(tool_map.items()):
        if name in HANDCRAFTED_COVERAGE or name in EXCLUDED:
            continue
        try:
            auto_cmd = _make_auto_command(name, defn, console)
            cmd.add_command(auto_cmd)
        except Exception:
            pass  # Skip commands that fail to generate
