"""Main CLI application for AgentQueue.

Provides a modern terminal interface that delegates all commands to the
daemon's CommandHandler via a REST API.  Uses Click for command structure
and Rich for beautiful output.

Entry point: ``aq`` console script.

Command modules are loaded from sibling files:
- tasks.py    — aq task {list,details,create,approve,stop,restart,search,select}
- agents.py   — aq agent {list,details}
- hooks.py    — aq hook {list,runs,details}
- projects.py — aq project {list,details,set}
- plugins.py  — aq plugin {list,info,install,remove,enable,disable,update,...}

Auto-generated commands are organized by tool_registry category and merged
into their respective CLI groups (e.g., ``aq git``, ``aq memory``, etc.).
"""

from __future__ import annotations

import asyncio
import sys

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
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _get_client(api_url: str | None = None):
    """Create a CLIClient instance."""
    from .client import CLIClient
    return CLIClient(base_url=api_url)


def _handle_errors(func):
    """Decorator that catches CLI client errors and prints them nicely.

    When the daemon is not running, offers to start it and retry.
    """
    import functools
    from .exceptions import CommandError, DaemonNotRunningError

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except DaemonNotRunningError:
            console.print("[bold red]Daemon is not running.[/]")
            if console.input("[bold]Start the daemon? [Y/n] [/]").strip().lower() in ("", "y", "yes"):
                from .daemon import start_daemon
                if start_daemon():
                    console.print()
                    # Retry the original command
                    try:
                        return func(*args, **kwargs)
                    except DaemonNotRunningError:
                        console.print("[bold red]Error:[/] Still cannot connect to daemon.")
                        raise SystemExit(1)
                    except CommandError as e:
                        console.print(f"[bold red]Error:[/] {e}")
                        raise SystemExit(1)
                else:
                    raise SystemExit(1)
            else:
                console.print("[dim]Run 'aq start' to start the daemon.[/]")
                raise SystemExit(1)
        except CommandError as e:
            console.print(f"[bold red]Error:[/] {e}")
            raise SystemExit(1)

    return wrapper


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option(
    "--api-url", envvar="AGENT_QUEUE_API_URL", default=None,
    help="Daemon API URL (default: from config or http://127.0.0.1:8081)",
)
@click.version_option(version="0.1.0", prog_name="aq")
@click.pass_context
def cli(ctx: click.Context, api_url: str | None) -> None:
    """AgentQueue CLI — Modern terminal interface for task management.

    Connects to the agent-queue daemon via its REST API.
    """
    ctx.ensure_object(dict)
    ctx.obj["api_url"] = api_url

    if ctx.invoked_subcommand is None:
        ctx.invoke(status)


# ---------------------------------------------------------------------------
# /status — System overview (kept here since it's the default command)
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
@_handle_errors
def status(ctx: click.Context) -> None:
    """Show system status overview."""
    from .adapters import agent_proxy, project_proxy
    from .formatters import format_agent_table, format_status_overview

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _run_status():
        async with _get_client(api_url) as client:
            result = await client.execute("get_status")
            return result

    result = _run(_run_status())

    # Adapt get_status response for format_status_overview.
    # The formatter expects (projects: list, agents: list, task_counts: dict).
    # get_status returns {"agents": [...], "tasks": {"by_status": {...}}, ...}
    agents = [agent_proxy(a) for a in result.get("agents", [])]
    task_counts = result.get("tasks", {}).get("by_status", {})
    # Formatter expects uppercase status keys
    task_counts = {k.upper(): v for k, v in task_counts.items()}

    # format_status_overview needs project list — but get_status only returns
    # a count.  We'll create minimal proxies from the agent data.
    project_ids = {a.get("project_id") for a in result.get("agents", []) if a.get("project_id")}
    projects = [project_proxy({"id": pid, "name": pid, "status": "ACTIVE"}) for pid in project_ids]

    panel = format_status_overview(projects, agents, task_counts)
    console.print(panel)

    if agents:
        console.print(format_agent_table(agents))


# ---------------------------------------------------------------------------
# Register command modules — importing them triggers @cli.group() decorators
# ---------------------------------------------------------------------------

from . import daemon   # noqa: E402, F401
from . import tasks    # noqa: E402, F401
from . import agents   # noqa: E402, F401
from . import hooks    # noqa: E402, F401
from . import projects # noqa: E402, F401
from . import plugins  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Auto-generated commands for all other CommandHandler commands
# ---------------------------------------------------------------------------

from .auto_commands import register_auto_commands  # noqa: E402
register_auto_commands(cli, console)


# ---------------------------------------------------------------------------
# Plugin CLI extensions
# ---------------------------------------------------------------------------


def _load_plugin_cli_groups() -> None:
    """Dynamically register CLI groups from installed aq.plugins entry points."""
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
                pass
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
