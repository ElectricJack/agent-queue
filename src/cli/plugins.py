"""Plugin management CLI commands (``aq plugin ...``).

These commands combine direct database and filesystem operations, so they
cannot use the daemon-backed generated-command path.  They still use the
shared output funnel: human renderers stay Rich-formatted while ``--json``
always emits exactly one versioned document.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, NoReturn

import click
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .app import _run, cli, console
from .envelope import emit, emit_error


def _get_plugin_client():
    from .client import PluginClient

    return PluginClient(db_path=os.environ.get("AGENT_QUEUE_DB"))


def _json_mode(ctx: click.Context) -> bool:
    return bool((ctx.obj or {}).get("json"))


def _safe_line(label: str, value: Any, *, label_style: str = "bold") -> Text:
    """Build a Rich line without interpreting command-controlled markup."""
    line = Text(label, style=label_style)
    line.append(str(value))
    return line


def _fail(ctx: click.Context, code: str, message: str, *, exit_code: int = 1) -> NoReturn:
    if _json_mode(ctx):
        emit_error(code, message)
    else:
        console.print(_safe_line("Error: ", message, label_style="bold red"))
    raise SystemExit(exit_code)


def _confirm_mutation(ctx: click.Context, yes: bool, prompt: str) -> None:
    """Never put a confirmation prompt in a JSON output stream."""
    if yes:
        return
    if _json_mode(ctx):
        _fail(ctx, "usage_error", "--yes is required with --json", exit_code=2)
    if not click.confirm(prompt):
        raise click.Abort()


@cli.group()
def plugin() -> None:
    """Plugin management commands."""


@plugin.command("list")
@click.pass_context
def plugin_list(ctx: click.Context) -> None:
    """List installed plugins."""

    async def _list():
        async with _get_plugin_client() as client:
            return await client.list_plugins()

    try:
        plugins = _run(_list())
    except Exception as exc:
        _fail(ctx, "command_error", f"Unable to list plugins: {exc}")

    def _render(rows: list[dict]) -> None:
        if not rows:
            console.print("No plugins installed.", style="dim", markup=False)
            return
        table = Table(title="Installed Plugins", show_lines=True)
        table.add_column("Name", style="bold cyan")
        table.add_column("Version")
        table.add_column("Status")
        table.add_column("Source")
        colors = {"active": "green", "installed": "yellow", "disabled": "dim", "error": "red"}
        for row in rows:
            status = str(row.get("status", "unknown"))
            table.add_row(
                Text(str(row.get("id", "?"))),
                Text(str(row.get("version", "?"))),
                Text(status, style=colors.get(status, "white")),
                Text(str(row.get("source_url", ""))),
            )
        console.print(table)

    emit(ctx, plugins, render=_render)


@plugin.command("info")
@click.argument("name")
@click.pass_context
def plugin_info(ctx: click.Context, name: str) -> None:
    """Show detailed plugin info."""

    async def _info():
        async with _get_plugin_client() as client:
            return await client.get_plugin(name)

    try:
        row = _run(_info())
    except Exception as exc:
        _fail(ctx, "command_error", f"Unable to inspect plugin {name!r}: {exc}")
    if not row:
        _fail(ctx, "not_found", f"Plugin {name!r} not found.")

    def _render(data: dict) -> None:
        lines = [
            _safe_line("Name: ", data.get("id", name)),
            _safe_line("Version: ", data.get("version", "?")),
            _safe_line("Status: ", data.get("status", "?")),
            _safe_line("Source: ", data.get("source_url", "?")),
            _safe_line("Rev: ", str(data.get("source_rev", "?"))[:12]),
            _safe_line("Path: ", data.get("install_path", "?")),
        ]
        if data.get("error_message"):
            lines.append(_safe_line("Error: ", data["error_message"], label_style="bold red"))
        console.print(Panel(Group(*lines), title=Text(f"Plugin: {name}")))

    emit(ctx, row, render=_render)


@plugin.command("install")
@click.argument("url")
@click.option("--branch", "-b", default=None, help="Branch to install")
@click.option("--name", "-n", default=None, help="Override plugin name")
@click.pass_context
def plugin_install(ctx: click.Context, url: str, branch: str | None, name: str | None) -> None:
    """Install a plugin from a git repository."""
    if not _json_mode(ctx):
        console.print(_safe_line("Installing plugin from ", f"{url}..."))

    async def _install():
        from src.plugins.loader import install_plugin_from_url

        async with _get_plugin_client() as client:
            data_dir = Path(
                os.environ.get("AGENT_QUEUE_DATA", os.path.expanduser("~/.agent-queue"))
            )
            result = await install_plugin_from_url(
                url, data_dir / "plugins", data_dir / "plugin-data", branch=branch, name=name
            )
            await client.create_plugin(
                plugin_id=result["name"],
                version=result["version"],
                source_url=url,
                source_rev=result["source_rev"],
                source_branch=branch or "",
                install_path=result["install_path"],
                status="installed",
                config=json.dumps(result["default_config"]),
                permissions=json.dumps(result["permissions"]),
            )
            return result

    try:
        result = _run(_install())
    except Exception as exc:
        _fail(ctx, "command_error", f"Installation failed: {exc}")
    data = {
        "id": result["name"],
        "version": result["version"],
        "source_url": url,
        "source_rev": result["source_rev"],
        "install_path": result["install_path"],
        "status": "installed",
        "restart_required": True,
    }

    def _render(payload: dict) -> None:
        console.print(
            _safe_line(
                "Installed plugin ",
                f"'{payload['id']}' v{payload['version']}",
                label_style="bold green",
            )
        )
        console.print("Restart the daemon to activate the plugin.", style="dim", markup=False)

    emit(ctx, data, render=_render)


@plugin.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.pass_context
def plugin_remove(ctx: click.Context, name: str, yes: bool) -> None:
    """Remove an installed plugin."""
    import shutil

    _confirm_mutation(ctx, yes, "Are you sure you want to remove this plugin?")

    async def _remove():
        async with _get_plugin_client() as client:
            row = await client.get_plugin(name)
            if not row:
                return None
            install_path = row.get("install_path")
            await client.delete_plugin_data_all(name)
            await client.delete_plugin(name)
            shared = any(p.get("install_path") == install_path for p in await client.list_plugins())
            return install_path, shared

    try:
        result = _run(_remove())
        if result is None:
            _fail(ctx, "not_found", f"Plugin {name!r} not found.")
        install_path, shared = result
        if install_path and os.path.exists(install_path) and not shared:
            shutil.rmtree(install_path)
    except SystemExit:
        raise
    except Exception as exc:
        _fail(ctx, "command_error", f"Removal failed: {exc}")
    data = {"id": name, "removed": True, "shared_install_path": shared}

    def _render(payload: dict) -> None:
        if payload["shared_install_path"]:
            console.print(
                "Warning: another plugin record shares this directory; skipping directory removal.",
                style="yellow",
                markup=False,
            )
        console.print(_safe_line("Plugin removed: ", payload["id"], label_style="bold green"))

    emit(ctx, data, render=_render)


def _set_plugin_status(ctx: click.Context, name: str, status: str) -> None:
    async def _update():
        async with _get_plugin_client() as client:
            await client.update_plugin(name, status=status)

    action = "Enable" if status == "installed" else "Disable"
    try:
        _run(_update())
    except Exception as exc:
        _fail(ctx, "command_error", f"{action} failed: {exc}")
    data = {"id": name, "status": status, "restart_required": status == "installed"}

    def _render(payload: dict) -> None:
        verb = "enabled" if status == "installed" else "disabled"
        console.print(_safe_line("Plugin ", f"'{payload['id']}' {verb}.", label_style="bold green"))
        if payload["restart_required"]:
            console.print("Restart the daemon to activate.", style="dim", markup=False)

    emit(ctx, data, render=_render)


@plugin.command("enable")
@click.argument("name")
@click.pass_context
def plugin_enable(ctx: click.Context, name: str) -> None:
    """Enable a disabled plugin."""
    _set_plugin_status(ctx, name, "installed")


@plugin.command("disable")
@click.argument("name")
@click.pass_context
def plugin_disable(ctx: click.Context, name: str) -> None:
    """Disable a plugin without removing it."""
    _set_plugin_status(ctx, name, "disabled")


@plugin.command("update")
@click.argument("name")
@click.pass_context
def plugin_update(ctx: click.Context, name: str) -> None:
    """Update a plugin (git pull + reinstall)."""
    from src.plugins.loader import (
        has_pyproject,
        install_plugin_package,
        load_plugin_via_entry_point,
        parse_plugin_metadata,
        parse_plugin_yaml,
        pull_plugin_repo,
    )

    async def _update():
        async with _get_plugin_client() as client:
            row = await client.get_plugin(name)
            if not row:
                raise LookupError(f"Plugin {name!r} not found.")
            install_path = row["install_path"]
            new_rev = await pull_plugin_repo(install_path)
            install_plugin_package(install_path)
            if has_pyproject(install_path):
                plugin_class = load_plugin_via_entry_point(name)
                info = (
                    parse_plugin_metadata(install_path, plugin_class)
                    if plugin_class
                    else parse_plugin_yaml(install_path)
                )
            else:
                info = parse_plugin_yaml(install_path)
            await client.update_plugin(name, version=info.version, source_rev=new_rev)
            return info.version, new_rev

    if not _json_mode(ctx):
        console.print(_safe_line("Updating plugin ", f"'{name}'..."))
    try:
        version, rev = _run(_update())
    except LookupError as exc:
        _fail(ctx, "not_found", f"Update failed: {exc}")
    except Exception as exc:
        _fail(ctx, "command_error", f"Update failed: {exc}")
    data = {
        "id": name,
        "version": version,
        "source_rev": rev,
        "updated": True,
        "restart_required": True,
    }

    def _render(payload: dict) -> None:
        console.print(
            _safe_line(
                "Plugin updated: ",
                f"'{payload['id']}' v{payload['version']} (rev {payload['source_rev'][:12]})",
                label_style="bold green",
            )
        )
        console.print("Restart the daemon to activate changes.", style="dim", markup=False)

    emit(ctx, data, render=_render)


@plugin.command("reload")
@click.argument("name")
@click.pass_context
def plugin_reload(ctx: click.Context, name: str) -> None:
    """Reload a plugin module."""
    from src.plugins.loader import import_plugin_module, parse_plugin_yaml

    async def _reload():
        async with _get_plugin_client() as client:
            row = await client.get_plugin(name)
            if not row:
                raise LookupError(f"Plugin {name!r} not found.")
            info = parse_plugin_yaml(row["install_path"])
            import_plugin_module(row["install_path"])
            await client.update_plugin(name, version=info.version)
            return info.version

    try:
        version = _run(_reload())
    except LookupError as exc:
        _fail(ctx, "not_found", f"Reload failed: {exc}")
    except Exception as exc:
        _fail(ctx, "command_error", f"Reload failed: {exc}")
    data = {"id": name, "version": version, "reloaded": True, "restart_required": True}

    def _render(payload: dict) -> None:
        console.print(
            _safe_line(
                "Plugin reloaded: ",
                f"'{payload['id']}' (v{payload['version']}).",
                label_style="bold green",
            )
        )
        console.print("Restart the daemon to apply in-process.", style="dim", markup=False)

    emit(ctx, data, render=_render)


@plugin.command("config")
@click.argument("name")
@click.argument("key_values", nargs=-1)
@click.pass_context
def plugin_config(ctx: click.Context, name: str, key_values: tuple[str, ...]) -> None:
    """View or set plugin configuration using optional KEY=VALUE pairs."""

    async def _config(updates: dict[str, str] | None):
        async with _get_plugin_client() as client:
            row = await client.get_plugin(name)
            if not row:
                raise LookupError(f"Plugin {name!r} not found.")
            current = json.loads(row.get("config", "{}") or "{}")
            if updates is not None:
                current.update(updates)
                await client.update_plugin(name, config=json.dumps(current))
            return current

    updates: dict[str, str] = {}
    for item in key_values:
        if "=" not in item:
            _fail(ctx, "usage_error", f"Invalid format: {item!r}; expected KEY=VALUE", exit_code=2)
        key, value = item.split("=", 1)
        updates[key] = value
    try:
        config = _run(_config(updates if key_values else None))
    except LookupError as exc:
        _fail(ctx, "not_found", str(exc))
    except Exception as exc:
        _fail(ctx, "command_error", f"Config error: {exc}")
    data = {"id": name, "config": config, "updated": bool(key_values)}

    def _render(payload: dict) -> None:
        if not payload["config"]:
            console.print(f"No configuration for plugin {name!r}.", style="dim", markup=False)
            return
        if payload["updated"]:
            console.print(_safe_line("Config updated for ", repr(name), label_style="bold green"))
        table = Table(title=Text(f"Config: {name}"))
        table.add_column("Key", style="bold cyan")
        table.add_column("Value")
        for key, value in sorted(payload["config"].items()):
            table.add_row(Text(str(key)), Text(str(value)))
        console.print(table)

    emit(ctx, data, render=_render)


# Guidance for the retired ``aq plugin logs`` stub. Defined once so the human
# and ``--json`` paths cannot drift apart.
_PLUGIN_LOGS_REMOVED_WHY = (
    "`aq plugin logs` has been removed: the hook engine it read is gone, "
    "so there is no plugin hook execution history to return."
)
# Each entry is (what you actually want, the command that gives it). Kept as
# short standalone lines because Rich hard-wraps human output at terminal
# width, and a wrapped paragraph splits a command mid-name.
_PLUGIN_LOGS_REPLACEMENTS = (
    ("Recent automation runs (playbooks replaced hooks)", "aq playbook list-runs"),
    ("One run in detail", "aq playbook inspect-run --run-id <run-id>"),
    (
        "A plugin's own diagnostic output - daemon logging, not hook history",
        "aq logs --grep <plugin-name>",
    ),
)

PLUGIN_LOGS_REMOVED_MESSAGE = " ".join(
    (_PLUGIN_LOGS_REMOVED_WHY,)
    + tuple(f"{what}: `{cmd}`." for what, cmd in _PLUGIN_LOGS_REPLACEMENTS)
)


@plugin.command("logs")
@click.argument("name")
@click.option(
    "--limit",
    default=None,
    type=int,
    hidden=True,
    help="Accepted and ignored; retained so legacy invocations reach this message.",
)
@click.pass_context
def plugin_logs(ctx: click.Context, name: str, limit: int | None) -> None:
    """Removed - plugin hook execution history no longer exists.

    This stub is deliberately an error, not an empty success: a command
    named ``logs`` that exits 0 with no rows reads as "this plugin has no
    history", which is a different (and false) claim from "the history this
    command read no longer exists". The legacy paging option stays
    accepted-and-ignored so an old script lands on this guidance instead of
    a Click usage error.
    """
    if _json_mode(ctx):
        emit_error("command_error", PLUGIN_LOGS_REMOVED_MESSAGE)
    else:
        console.print(f"[bold red]Error:[/] {_PLUGIN_LOGS_REMOVED_WHY}")
        console.print("Use instead:")
        for what, cmd in _PLUGIN_LOGS_REPLACEMENTS:
            console.print(f"  [bold]{cmd}[/]", highlight=False, soft_wrap=True)
            console.print(f"    [dim]{what}[/]")
    raise SystemExit(1)


@plugin.command("prompts")
@click.argument("name")
@click.pass_context
def plugin_prompts(ctx: click.Context, name: str) -> None:
    """List prompts provided by a plugin."""

    async def _path():
        async with _get_plugin_client() as client:
            row = await client.get_plugin(name)
            if not row:
                raise LookupError(f"Plugin {name!r} not found.")
            return row["install_path"]

    try:
        install_path = _run(_path())
    except LookupError as exc:
        _fail(ctx, "not_found", str(exc))
    except Exception as exc:
        _fail(ctx, "command_error", f"Unable to list plugin prompts: {exc}")
    inst_dir, src_dir = Path(install_path) / "prompts", Path(install_path) / "src" / "prompts"
    names = {
        entry.name
        for directory in (src_dir, inst_dir)
        if directory.exists()
        for entry in directory.iterdir()
        if entry.is_file()
    }
    rows = [
        {"file": item, "source": (src_dir / item).exists(), "instance": (inst_dir / item).exists()}
        for item in sorted(names)
    ]

    def _render(data: list[dict]) -> None:
        if not data:
            console.print(f"No prompts found for plugin {name!r}.", style="dim", markup=False)
            return
        table = Table(title=Text(f"Prompts: {name}"))
        table.add_column("File", style="bold cyan")
        table.add_column("Source", style="dim")
        table.add_column("Instance")
        for row in data:
            table.add_row(
                Text(row["file"]),
                Text("yes" if row["source"] else "no"),
                Text("yes" if row["instance"] else "no"),
            )
        console.print(table)

    emit(ctx, rows, render=_render)


@plugin.command("diff-prompts")
@click.argument("name")
@click.pass_context
def plugin_diff_prompts(ctx: click.Context, name: str) -> None:
    """Diff instance prompts vs source defaults."""
    import difflib

    async def _path():
        async with _get_plugin_client() as client:
            row = await client.get_plugin(name)
            if not row:
                raise LookupError(f"Plugin {name!r} not found.")
            return row["install_path"]

    try:
        install_path = _run(_path())
    except LookupError as exc:
        _fail(ctx, "not_found", str(exc))
    except Exception as exc:
        _fail(ctx, "command_error", f"Unable to diff plugin prompts: {exc}")
    src_dir, inst_dir = Path(install_path) / "src" / "prompts", Path(install_path) / "prompts"
    files: list[dict[str, Any]] = []
    if src_dir.exists():
        for src_file in sorted(src_dir.iterdir()):
            if not src_file.is_file():
                continue
            inst_file = inst_dir / src_file.name
            if not inst_file.exists():
                files.append({"file": src_file.name, "status": "instance_missing", "diff": []})
                continue
            diff = [
                line.rstrip("\n")
                for line in difflib.unified_diff(
                    src_file.read_text(encoding="utf-8").splitlines(keepends=True),
                    inst_file.read_text(encoding="utf-8").splitlines(keepends=True),
                    fromfile=f"source/{src_file.name}",
                    tofile=f"instance/{src_file.name}",
                )
            ]
            if diff:
                files.append({"file": src_file.name, "status": "different", "diff": diff})
    data = {
        "plugin": name,
        "source_available": src_dir.exists(),
        "matches": not files,
        "files": files,
    }

    def _render(payload: dict) -> None:
        if not payload["source_available"]:
            console.print(f"No source prompts for plugin {name!r}.", style="dim", markup=False)
        elif payload["matches"]:
            console.print(
                _safe_line(
                    "All prompts match source defaults for ", repr(name), label_style="bold green"
                )
            )
        else:
            for entry in payload["files"]:
                if entry["status"] == "instance_missing":
                    console.print(
                        _safe_line("Instance file missing: ", entry["file"], label_style="yellow")
                    )
                    continue
                console.print(Text(f"\n{entry['file']}", style="bold"))
                for line in entry["diff"]:
                    style = (
                        "green" if line.startswith("+") else "red" if line.startswith("-") else None
                    )
                    console.print(Text(line, style=style))

    emit(ctx, data, render=_render)


@plugin.command("reset-prompts")
@click.argument("name")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.pass_context
def plugin_reset_prompts(ctx: click.Context, name: str, yes: bool) -> None:
    """Reset instance prompts to source defaults."""
    from src.plugins.loader import reset_prompts

    _confirm_mutation(ctx, yes, "Reset all prompts to source defaults?")

    async def _path():
        async with _get_plugin_client() as client:
            row = await client.get_plugin(name)
            if not row:
                raise LookupError(f"Plugin {name!r} not found.")
            return row["install_path"]

    try:
        count = reset_prompts(_run(_path()))
    except LookupError as exc:
        _fail(ctx, "not_found", str(exc))
    except Exception as exc:
        _fail(ctx, "command_error", f"Reset failed: {exc}")
    data = {"id": name, "reset": count}
    emit(
        ctx,
        data,
        render=lambda payload: console.print(
            _safe_line(
                "Prompts reset: ",
                f"{payload['reset']} for {payload['id']!r}.",
                label_style="bold green",
            )
        ),
    )
