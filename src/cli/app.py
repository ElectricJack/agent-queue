"""Main CLI application for AgentQueue.

Provides a modern terminal interface mirroring Discord slash commands.
Uses Click for command structure and Rich for beautiful output.

Entry point: ``aq`` console script.

Command modules are loaded from sibling files:
- tasks.py    — aq task {list,details,create,approve,stop,restart,search,select}
- agents.py   — aq agent {list,details}
- hooks.py    — aq hook {list,runs,details}
- projects.py — aq project {list,details,set}
- plugins.py  — aq plugin {list,info,install,remove,enable,disable,update,...}
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import click
from rich.console import Console

from .styles import AQ_THEME

# Create themed console
console = Console(theme=AQ_THEME)

# ---------------------------------------------------------------------------
# Async runner helper
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop (unlikely for CLI)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _get_client():
    """Create a CLIClient instance."""
    from .client import CLIClient
    db_path = os.environ.get("AGENT_QUEUE_DB")
    return CLIClient(db_path=db_path)


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option("--db", envvar="AGENT_QUEUE_DB", default=None, help="Path to AgentQueue database")
@click.version_option(version="0.1.0", prog_name="aq")
@click.pass_context
def cli(ctx: click.Context, db: str | None) -> None:
    """AgentQueue CLI — Modern terminal interface for task management.

    Mirrors Discord slash commands with rich formatting and interactive menus.
    """
    ctx.ensure_object(dict)
    if db:
        os.environ["AGENT_QUEUE_DB"] = db

    if ctx.invoked_subcommand is None:
        # Default: show status
        ctx.invoke(status)


# ---------------------------------------------------------------------------
# /status — System overview (kept here since it's the default command)
# ---------------------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show system status overview."""
    from .formatters import format_status_overview, format_agent_table

    async def _run_status():
        async with _get_client() as client:
            projects = await client.list_projects()
            agents = await client.list_agents()
            task_counts = await client.count_tasks_by_status()
            return projects, agents, task_counts

    try:
        projects, agents, task_counts = _run(_run_status())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    panel = format_status_overview(projects, agents, task_counts)
    console.print(panel)


# ---------------------------------------------------------------------------
# Register command modules — importing them triggers @cli.group() decorators
# ---------------------------------------------------------------------------

from . import tasks    # noqa: E402, F401
from . import agents   # noqa: E402, F401
from . import hooks    # noqa: E402, F401
from . import projects # noqa: E402, F401
from . import plugins  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Plugin CLI extensions
# ---------------------------------------------------------------------------


def _load_plugin_cli_groups() -> None:
    """Dynamically register CLI groups from installed aq.plugins entry points.

    Iterates over all ``aq.plugins`` entry points, instantiates each Plugin
    class, and calls ``cli_group()`` to get a Click group.  If the plugin
    provides one, it is mounted on the main ``cli`` group as
    ``aq <entry-point-name> ...``.

    Failures are silently ignored so that a broken plugin never prevents the
    CLI from starting.
    """
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="aq.plugins"):
            try:
                cls = ep.load()
                instance = cls()
                group = instance.cli_group()
                if group is not None:
                    cli.add_command(group, ep.name)
            except Exception:
                pass  # Plugin CLI failure must not break the CLI
    except Exception:
        pass


_load_plugin_cli_groups()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
