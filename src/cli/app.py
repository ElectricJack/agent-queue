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
import functools
import logging
import sys

import click
from rich.console import Console

from .global_options import AQGroup as GlobalOptionsAQGroup
from .styles import AQ_THEME

logger = logging.getLogger(__name__)


def _installed_version() -> str:
    """Prefer wheel metadata so ``aq --version`` identifies the artifact."""
    try:
        from importlib.metadata import version

        return version("agent-queue")
    except Exception:  # pragma: no cover - source-only checkout fallback
        from . import __version__

        return __version__

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


def _json_mode() -> bool:
    """True when the current invocation carries the global ``--json`` flag."""
    ctx = click.get_current_context(silent=True)
    return bool((ctx.obj or {}).get("json")) if ctx is not None else False


def _render_command_findings(details: dict) -> None:
    """Print a command's structured ``errors``/``warnings`` finding lists.

    ``create_task_graph`` reports every rule that failed at once; without
    this the CLI printed only the summary line ("graph validation failed
    with 3 error(s)") and the author never learned *which* rules failed.
    Rule names are not wrapped in square brackets — Rich would read those
    as markup and swallow the text.
    """
    for severity, key in (("error", "errors"), ("warning", "warnings")):
        for finding in details.get(key) or []:
            if not isinstance(finding, dict):
                continue
            where = f" ({finding['node']})" if finding.get("node") else ""
            from rich.text import Text

            line = Text("  ")
            line.append(severity, style="red" if severity == "error" else "yellow")
            line.append(
                f" {finding.get('rule')}{where}: {finding.get('detail')}"
            )
            console.print(line)


def _handle_errors(func):
    """Decorator that catches CLI client errors and reports them.

    Two modes, per ``docs/specs/design/aq-surface.md`` §4.1:

    - ``--json``: exactly one error envelope on **stdout**, no Rich
      formatting and **no interactive prompt** — agent-facing commands
      (``aq reply``, ``aq inbox``) must never hang on a ``[Y/n]`` or return
      human text into a stream something is parsing.
    - human: Rich output, and an offer to start the daemon when it is down.

    Exit codes: 1 command error, 3 daemon unreachable, 4 auth/scope denied.
    """
    import functools
    from .envelope import emit_error
    from .exceptions import CommandError, DaemonNotRunningError

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        as_json = _json_mode()

        def _fail_command(exc: CommandError) -> None:
            if as_json:
                emit_error(exc.code, exc.detail_message, exc.details or None)
            else:
                from rich.text import Text

                line = Text("Error: ", style="bold red")
                line.append(str(exc))
                console.print(line)
                _render_command_findings(exc.details or {})
            raise SystemExit(exc.exit_code)

        try:
            return func(*args, **kwargs)
        except DaemonNotRunningError as exc:
            if as_json:
                emit_error(exc.code, str(exc))
                raise SystemExit(exc.exit_code)
            console.print("[bold red]Daemon is not running.[/]")
            if console.input("[bold]Start the daemon? [Y/n] [/]").strip().lower() in (
                "",
                "y",
                "yes",
            ):
                from .daemon import start_daemon

                if start_daemon():
                    console.print()
                    # Retry the original command
                    try:
                        return func(*args, **kwargs)
                    except DaemonNotRunningError as retry_exc:
                        console.print("[bold red]Error:[/] Still cannot connect to daemon.")
                        raise SystemExit(retry_exc.exit_code)
                    except CommandError as retry_exc:
                        _fail_command(retry_exc)
                else:
                    raise SystemExit(exc.exit_code)
            else:
                console.print("[dim]Run 'aq start' to start the daemon.[/]")
                raise SystemExit(exc.exit_code)
        except CommandError as exc:
            _fail_command(exc)

    return wrapper


class AQGroup(GlobalOptionsAQGroup):
    """Root group with position-independent options and JSON usage errors."""

    @staticmethod
    def _wants_json(args) -> bool:
        values = list(sys.argv[1:] if args is None else args)
        try:
            end = values.index("--")
        except ValueError:
            end = len(values)
        return "--json" in values[:end]

    def main(
        self,
        args=None,
        prog_name=None,
        complete_var=None,
        standalone_mode=True,
        windows_expand_args=True,
        **extra,
    ):
        # In non-standalone mode Click deliberately exposes exceptions to its
        # caller; preserve that API for embedding/tests.
        if not standalone_mode or not self._wants_json(args):
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )

        from .envelope import emit_error

        try:
            rv = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except click.UsageError as exc:
            emit_error("usage_error", exc.format_message())
            raise SystemExit(2) from exc
        except click.ClickException as exc:
            emit_error("command_error", exc.format_message())
            raise SystemExit(exc.exit_code) from exc
        raise SystemExit(rv if isinstance(rv, int) else 0)


# ---------------------------------------------------------------------------
# Full help dump for LLM ingestion
# ---------------------------------------------------------------------------


def _print_full_help(ctx: click.Context) -> None:
    """Print complete help for every command, recursively.

    Output is plain text, structured for easy LLM consumption.
    """
    group = ctx.command
    assert isinstance(group, click.Group)

    # Top-level help
    click.echo(group.get_help(ctx))
    click.echo()

    def _walk(grp: click.Group, prefix: str) -> None:
        for name in sorted(grp.list_commands(ctx)):
            cmd = grp.get_command(ctx, name)
            if cmd is None:
                continue
            full_name = f"{prefix} {name}"
            click.echo("=" * 72)
            click.echo(f"  {full_name}")
            click.echo("=" * 72)
            # Build a sub-context with just the leaf name so Usage shows correctly
            sub_ctx = click.Context(cmd, info_name=full_name)
            click.echo(cmd.get_help(sub_ctx))
            click.echo()
            if isinstance(cmd, click.Group):
                _walk(cmd, full_name)

    _walk(group, "aq")


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------


@click.group(cls=AQGroup, invoke_without_command=True)
@click.option(
    "--api-url",
    envvar="AGENT_QUEUE_API_URL",
    default=None,
    help="Daemon API URL (default: from config or http://127.0.0.1:8081)",
)
@click.option(
    "--help-all",
    is_flag=True,
    default=False,
    help="Print complete help for all commands (for LLM ingestion).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output one versioned JSON document instead of human formatting.",
)
@click.option(
    "--brief",
    "brief",
    is_flag=True,
    default=False,
    help="Trim output to each entity's lite projection (composes with --json).",
)
@click.version_option(version=_installed_version(), prog_name="aq")
@click.pass_context
def cli(
    ctx: click.Context,
    api_url: str | None,
    help_all: bool,
    output_json: bool,
    brief: bool,
) -> None:
    """Agent Q CLI — Modern terminal interface for task management.

    Connects to the agent-queue daemon via its REST API.
    """
    ctx.ensure_object(dict)
    ctx.obj["api_url"] = api_url
    ctx.obj["json"] = output_json
    ctx.obj["brief"] = brief

    if help_all:
        _print_full_help(ctx)
        ctx.exit(0)
        return

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
    from .adapters import project_proxy
    from .formatters import format_status_overview

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _run_status():
        async with _get_client(api_url) as client:
            result = await client.execute("get_status")
            return result

    result = _run(_run_status())

    def _render(data):
        # Adapt get_status response for format_status_overview.  The command
        # may return a typed object or a plain dictionary.
        def _get(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            val = getattr(obj, key, default)
            if type(val).__name__ == "Unset":
                return default
            return val

        tasks_section = _get(data, "tasks", {})
        if isinstance(tasks_section, dict):
            task_counts = tasks_section.get("by_status", {})
        else:
            task_counts = _get(tasks_section, "by_status", {})
        task_counts = {k.upper(): v for k, v in task_counts.items()}
        num_projects = _get(data, "projects", 0)
        proj_list = [
            project_proxy(
                {"id": f"project-{i}", "name": f"project-{i}", "status": "ACTIVE"}
            )
            for i in range(num_projects)
        ]
        console.print(format_status_overview(proj_list, task_counts))

    from .envelope import emit

    emit(ctx, result, render=_render)


# ---------------------------------------------------------------------------
# Register command modules — importing them triggers @cli.group() decorators
# ---------------------------------------------------------------------------

from . import daemon  # noqa: E402, F401
from . import db as _db_cli  # noqa: E402, F401
from . import doctor  # noqa: E402, F401
from . import install as _install_cli  # noqa: E402, F401
from . import logs  # noqa: E402, F401
from . import tasks  # noqa: E402, F401
from . import projects  # noqa: E402, F401
from . import plugins  # noqa: E402, F401
from . import vault  # noqa: E402, F401
from . import agent_surface  # noqa: E402, F401
from . import formulas as _formulas_cli  # noqa: E402, F401
from . import sessions as _sessions_cli  # noqa: E402, F401
from . import messages as _messages_cli  # noqa: E402, F401
from . import agent_messages as _agent_messages_cli  # noqa: E402, F401
from . import questions as _questions_cli  # noqa: E402, F401
from . import streams as _streams_cli  # noqa: E402, F401
from . import playbook as _playbook_cli  # noqa: E402, F401
from . import test_runner as _test_runner_cli  # noqa: E402, F401
from . import integration as _integration_cli  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Auto-generated commands for all other CommandHandler commands
# ---------------------------------------------------------------------------

from .auto_commands import register_auto_commands  # noqa: E402

register_auto_commands(cli, console)


# Hand-crafted subgroups that hang off auto-generated parent groups.
# Must come AFTER register_auto_commands() so the parent groups exist.
from . import system_config as _system_config_cli  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Plugin CLI extensions
# ---------------------------------------------------------------------------


_PLUGIN_CONFIG_TIMEOUT_SECONDS = 3.0


def _load_plugin_config_from_db(plugin_id: str) -> dict | None:
    """Read one plugin's saved config without initializing the database.

    This function is called only from the plugin group's Click callback, so
    imports and eager help options never touch the database.  The narrow
    reader exposes no mutation methods, and the outer timeout bounds both
    connecting and querying an unavailable PostgreSQL server.
    """
    from .client import PluginConfigReader

    async def _fetch():
        async with PluginConfigReader() as reader:
            return await reader.get_config(plugin_id)

    try:
        return _run(asyncio.wait_for(_fetch(), timeout=_PLUGIN_CONFIG_TIMEOUT_SECONDS))
    except TimeoutError as exc:
        raise RuntimeError(
            f"database lookup timed out after {_PLUGIN_CONFIG_TIMEOUT_SECONDS:g}s"
        ) from exc


def _configure_plugin_on_invoke(
    plugin_id: str,
    instance: object,
    *,
    config_loader=None,
) -> None:
    """Merge persisted config immediately before a plugin command runs.

    Plugin CLI extensions historically fell back to their declared defaults
    when the daemon database was unavailable.  Preserve that useful offline
    behaviour, but make the fallback visible and actionable instead of
    swallowing every exception during module import.
    """
    config_loader = config_loader or _load_plugin_config_from_db
    try:
        db_config = config_loader(plugin_id)
    except Exception as exc:
        click.echo(
            f"Warning: could not load saved config for plugin '{plugin_id}': {exc}. "
            "Using plugin defaults; check database.url or run this command from an "
            "operator shell.",
            err=True,
        )
        return

    if db_config is None:
        return
    current = getattr(instance, "config", {})
    if not isinstance(current, dict):
        current = {}
    instance.config = {**current, **db_config}


def _defer_plugin_config(
    plugin_id: str,
    instance: object,
    group: click.Group,
    *,
    config_loader=None,
) -> None:
    """Attach lazy configuration to *group* without affecting help paths."""
    original_callback = group.callback

    def configured_callback(*args, **kwargs):
        _configure_plugin_on_invoke(plugin_id, instance, config_loader=config_loader)
        if original_callback is not None:
            return original_callback(*args, **kwargs)
        return None

    if original_callback is not None:
        configured_callback = functools.wraps(original_callback)(configured_callback)
    group.callback = configured_callback


def _broken_plugin_group(plugin_id: str, exc: Exception) -> click.Group:
    """Return a discoverable command that reports an entry-point failure."""
    detail = f"{type(exc).__name__}: {exc}"

    @click.group(
        plugin_id,
        invoke_without_command=True,
        help=f"Unavailable plugin command ({detail}).",
    )
    @click.pass_context
    def broken(ctx: click.Context) -> None:
        if ctx.invoked_subcommand is None:
            raise click.ClickException(
                f"plugin '{plugin_id}' could not be loaded: {detail}. "
                "Reinstall the plugin or inspect `aq plugin info`."
            )

    return broken


def _tag_plugin_cli_tree(command: click.Command, plugin_name: str) -> None:
    """Mark an external plugin's Click tree for inventory provenance."""
    command._aq_registration = "plugin-extension"  # type: ignore[attr-defined]
    command._aq_owner_kind = "external-plugin"  # type: ignore[attr-defined]
    command._aq_owner = plugin_name  # type: ignore[attr-defined]
    if isinstance(command, click.Group):
        for child in command.commands.values():
            _tag_plugin_cli_tree(child, plugin_name)


def _load_plugin_cli_groups(
    cli_group: click.Group | None = None,
    *,
    entry_point_provider=None,
    config_loader=None,
) -> list[str]:
    """Register installed plugin CLI groups without allowing core shadowing.

    The injectable providers keep plugin-present and plugin-absent startup
    behavior testable without installing packages or contacting a database.
    Returns the names that were successfully mounted.
    """
    cli_group = cli_group or cli
    config_loader = config_loader or _load_plugin_config_from_db
    mounted: list[str] = []
    try:
        if entry_point_provider is None:
            from importlib.metadata import entry_points

            entry_point_provider = entry_points

        for ep in entry_point_provider(group="aq.plugins"):
            if ep.name in cli_group.commands:
                logger.warning(
                    "Plugin CLI entry point '%s' conflicts with an existing command; skipped",
                    ep.name,
                )
                continue
            try:
                cls = ep.load()
                instance = cls()
                group = instance.cli_group()
                if group is not None:
                    _defer_plugin_config(
                        ep.name,
                        instance,
                        group,
                        config_loader=config_loader,
                    )
                    _tag_plugin_cli_tree(group, ep.name)
                    cli_group.add_command(group, ep.name)
                    mounted.append(ep.name)
            except Exception as exc:
                logger.warning("Plugin CLI entry point '%s' failed: %s", ep.name, exc)
                group = _broken_plugin_group(ep.name, exc)
                _tag_plugin_cli_tree(group, ep.name)
                cli_group.add_command(group, ep.name)
    except Exception as exc:
        logger.warning("Plugin CLI entry-point discovery failed: %s", exc)
    return mounted


_load_plugin_cli_groups()


# ---------------------------------------------------------------------------
# Global options at every position
# ---------------------------------------------------------------------------
# Must run last: it walks the finished command tree, so anything registered
# after this point would not get the global options.  See
# ``global_options.py`` for the grammar and the two exclusions.

from .global_options import install_global_options  # noqa: E402

install_global_options(cli)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    cli()


if __name__ == "__main__":
    # ``python -m src.cli.app`` executes this file as ``__main__`` and then
    # imports it AGAIN as ``src.cli.app`` when the submodules above run
    # ``from .app import cli`` — so every hand-crafted command (inbox, reply,
    # message, schema, prime, handoff, …) registers on the *other* module's
    # ``cli`` group and silently vanishes from this one.  Delegate to the
    # canonical module so both entry points see the same command set.
    from src.cli.app import main as _canonical_main

    _canonical_main()
