"""Plugin management CLI commands (aq plugin ...)."""

from __future__ import annotations

import os

import click

from .app import cli, console, _run


def _get_plugin_client():
    """Create a PluginClient for direct-DB plugin operations."""
    from .client import PluginClient

    db_path = os.environ.get("AGENT_QUEUE_DB")
    return PluginClient(db_path=db_path)


@cli.group()
def plugin() -> None:
    """Plugin management commands."""
    pass


@plugin.command("list")
def plugin_list() -> None:
    """List installed plugins."""
    from rich.table import Table

    async def _run_list():
        async with _get_plugin_client() as client:
            return await client.list_plugins()

    try:
        plugins = _run(_run_list())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not plugins:
        console.print("[dim]No plugins installed.[/dim]")
        return

    table = Table(title="Installed Plugins", show_lines=True)
    table.add_column("Name", style="bold cyan")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Source")

    status_colors = {
        "active": "green",
        "installed": "yellow",
        "disabled": "dim",
        "error": "red",
    }

    for p in plugins:
        status = p.get("status", "unknown")
        color = status_colors.get(status, "white")
        table.add_row(
            p.get("id", "?"),
            p.get("version", "?"),
            f"[{color}]{status}[/{color}]",
            p.get("source_url", ""),
        )

    console.print(table)


@plugin.command("info")
@click.argument("name")
def plugin_info(name: str) -> None:
    """Show detailed plugin info."""
    from rich.panel import Panel

    async def _run_info():
        async with _get_plugin_client() as client:
            return await client.get_plugin(name)

    try:
        p = _run(_run_info())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not p:
        console.print(f"[bold red]Plugin '{name}' not found.[/]")
        return

    lines = []
    lines.append(f"[bold]Name:[/] {p.get('id', name)}")
    lines.append(f"[bold]Version:[/] {p.get('version', '?')}")
    lines.append(f"[bold]Status:[/] {p.get('status', '?')}")
    lines.append(f"[bold]Source:[/] {p.get('source_url', '?')}")
    lines.append(f"[bold]Rev:[/] {p.get('source_rev', '?')[:12]}")
    lines.append(f"[bold]Path:[/] {p.get('install_path', '?')}")
    if p.get("error_message"):
        lines.append(f"[bold red]Error:[/] {p['error_message']}")

    console.print(Panel("\n".join(lines), title=f"Plugin: {name}"))


@plugin.command("install")
@click.argument("url")
@click.option("--branch", "-b", default=None, help="Branch to install")
@click.option("--name", "-n", default=None, help="Override plugin name")
def plugin_install(url: str, branch: str | None, name: str | None) -> None:
    """Install a plugin from a git repository."""
    console.print(f"[bold]Installing plugin from {url}...[/]")

    async def _run_install():
        async with _get_plugin_client() as client:
            from src.plugins.loader import install_plugin_from_url
            from pathlib import Path
            import json

            data_dir = Path(
                os.environ.get("AGENT_QUEUE_DATA", os.path.expanduser("~/.agent-queue"))
            )
            result = await install_plugin_from_url(
                url,
                data_dir / "plugins",
                data_dir / "plugin-data",
                branch=branch,
                name=name,
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
            return result["name"], result["version"]

    try:
        pname, pversion = _run(_run_install())
        console.print(f"[bold green]Installed plugin '{pname}' v{pversion}[/]")
        console.print("[dim]Restart the daemon to activate the plugin.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Installation failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("remove")
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to remove this plugin?")
def plugin_remove(name: str) -> None:
    """Remove an installed plugin."""
    import shutil

    async def _run_remove():
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                return None, False
            install_path = p.get("install_path")
            await client.delete_plugin_data_all(name)
            await client.delete_plugin(name)
            # Check if another plugin record shares this install path
            all_plugins = await client.list_plugins()
            shared = any(pp.get("install_path") == install_path for pp in all_plugins)
            return install_path, shared

    try:
        result = _run(_run_remove())
        if result[0] is None:
            console.print(f"[bold red]Plugin '{name}' not found.[/]")
            raise SystemExit(1)

        install_path, shared = result
        if install_path and os.path.exists(install_path):
            if shared:
                console.print(
                    "[yellow]Warning: another plugin record shares this directory — "
                    "skipping directory removal.[/]"
                )
            else:
                shutil.rmtree(install_path)

        console.print(f"[bold green]Plugin '{name}' removed.[/]")
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[bold red]Removal failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("enable")
@click.argument("name")
def plugin_enable(name: str) -> None:
    """Enable a disabled plugin."""

    async def _run_enable():
        async with _get_plugin_client() as client:
            await client.update_plugin(name, status="installed")

    try:
        _run(_run_enable())
        console.print(f"[bold green]Plugin '{name}' enabled.[/]")
        console.print("[dim]Restart the daemon to activate.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Enable failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("disable")
@click.argument("name")
def plugin_disable(name: str) -> None:
    """Disable a plugin without removing it."""

    async def _run_disable():
        async with _get_plugin_client() as client:
            await client.update_plugin(name, status="disabled")

    try:
        _run(_run_disable())
        console.print(f"[bold green]Plugin '{name}' disabled.[/]")
    except Exception as e:
        console.print(f"[bold red]Disable failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("update")
@click.argument("name")
def plugin_update(name: str) -> None:
    """Update a plugin (git pull + reinstall)."""
    from src.plugins.loader import (
        has_pyproject,
        install_plugin_package,
        load_plugin_via_entry_point,
        parse_plugin_metadata,
        parse_plugin_yaml,
        pull_plugin_repo,
    )

    async def _run_update():
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            install_path = p["install_path"]
            new_rev = await pull_plugin_repo(install_path)
            install_plugin_package(install_path)
            if has_pyproject(install_path):
                plugin_class = load_plugin_via_entry_point(name)
                if plugin_class:
                    info = parse_plugin_metadata(install_path, plugin_class)
                else:
                    info = parse_plugin_yaml(install_path)
            else:
                info = parse_plugin_yaml(install_path)
            await client.update_plugin(
                name,
                version=info.version,
                source_rev=new_rev,
            )
            return info.version, new_rev

    try:
        console.print(f"[bold]Updating plugin '{name}'...[/]")
        version, rev = _run(_run_update())
        console.print(f"[bold green]Plugin '{name}' updated to v{version} (rev {rev[:12]})[/]")
        console.print("[dim]Restart the daemon to activate changes.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Update failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("reload")
@click.argument("name")
def plugin_reload(name: str) -> None:
    """Reload a plugin module."""
    from src.plugins.loader import parse_plugin_yaml, import_plugin_module

    async def _run_reload():
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            install_path = p["install_path"]
            info = parse_plugin_yaml(install_path)
            import_plugin_module(install_path)
            await client.update_plugin(name, version=info.version)
            return info.version

    try:
        version = _run(_run_reload())
        console.print(f"[bold green]Plugin '{name}' reloaded (v{version}).[/]")
        console.print("[dim]Restart the daemon to apply in-process.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Reload failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("config")
@click.argument("name")
@click.argument("key_values", nargs=-1)
def plugin_config(name: str, key_values: tuple[str, ...]) -> None:
    """View or set plugin configuration.

    With no KEY=VALUE arguments, shows current config.
    With KEY=VALUE pairs, sets those values.
    """
    import json

    async def _run_config():
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            return p

    async def _set_config(updates: dict):
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            current = json.loads(p.get("config", "{}") or "{}")
            current.update(updates)
            await client.update_plugin(name, config=json.dumps(current))
            return current

    try:
        if not key_values:
            p = _run(_run_config())
            cfg = json.loads(p.get("config", "{}") or "{}")
            if not cfg:
                console.print(f"[dim]No configuration for plugin '{name}'.[/dim]")
                return
            from rich.table import Table

            table = Table(title=f"Config: {name}")
            table.add_column("Key", style="bold cyan")
            table.add_column("Value")
            for k, v in sorted(cfg.items()):
                table.add_row(k, str(v))
            console.print(table)
        else:
            updates = {}
            for kv in key_values:
                if "=" not in kv:
                    console.print(f"[bold red]Invalid format:[/] '{kv}' (expected KEY=VALUE)")
                    raise SystemExit(1)
                k, v = kv.split("=", 1)
                updates[k] = v
            result = _run(_set_config(updates))
            console.print(f"[bold green]Config updated for '{name}'.[/]")
            for k, v in sorted(result.items()):
                console.print(f"  [cyan]{k}[/] = {v}")
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[bold red]Config error:[/] {e}")
        raise SystemExit(1)


@plugin.command("logs")
@click.argument("name")
@click.option("--limit", default=20, help="Number of recent runs to show")
def plugin_logs(name: str, limit: int) -> None:
    """View plugin hook execution history."""
    from .formatters import format_hook_run_table

    async def _run_logs():
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            hooks = await client.list_hooks()
            plugin_hooks = [h for h in hooks if getattr(h, "plugin_id", None) == name]
            all_runs = []
            for h in plugin_hooks:
                runs = await client.list_hook_runs(h.id, limit=limit)
                all_runs.extend(runs)
            all_runs.sort(
                key=lambda r: getattr(r, "started_at", 0) or 0,
                reverse=True,
            )
            return all_runs[:limit]

    try:
        runs = _run(_run_logs())
    except Exception as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not runs:
        console.print(f"[dim]No hook runs found for plugin '{name}'.[/dim]")
        return

    table = format_hook_run_table(runs)
    console.print(table)


@plugin.command("prompts")
@click.argument("name")
def plugin_prompts(name: str) -> None:
    """List prompts provided by a plugin."""
    from pathlib import Path

    async def _run_prompts():
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            return p["install_path"]

    try:
        install_path = _run(_run_prompts())
    except Exception as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    inst_dir = Path(install_path) / "prompts"
    src_dir = Path(install_path) / "src" / "prompts"

    from rich.table import Table

    table = Table(title=f"Prompts: {name}")
    table.add_column("File", style="bold cyan")
    table.add_column("Source", style="dim")
    table.add_column("Instance")

    prompt_names: set[str] = set()
    if src_dir.exists():
        for f in src_dir.iterdir():
            if f.is_file():
                prompt_names.add(f.name)
    if inst_dir.exists():
        for f in inst_dir.iterdir():
            if f.is_file():
                prompt_names.add(f.name)

    if not prompt_names:
        console.print(f"[dim]No prompts found for plugin '{name}'.[/dim]")
        return

    for pname in sorted(prompt_names):
        has_src = (src_dir / pname).exists() if src_dir.exists() else False
        has_inst = (inst_dir / pname).exists() if inst_dir.exists() else False
        table.add_row(
            pname,
            "[green]yes[/]" if has_src else "[dim]no[/]",
            "[green]yes[/]" if has_inst else "[dim]no[/]",
        )

    console.print(table)


@plugin.command("diff-prompts")
@click.argument("name")
def plugin_diff_prompts(name: str) -> None:
    """Diff instance prompts vs source defaults."""
    import difflib
    from pathlib import Path

    async def _run_diff():
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            return p["install_path"]

    try:
        install_path = _run(_run_diff())
    except Exception as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    src_dir = Path(install_path) / "src" / "prompts"
    inst_dir = Path(install_path) / "prompts"

    if not src_dir.exists():
        console.print(f"[dim]No source prompts for plugin '{name}'.[/dim]")
        return

    any_diff = False
    for src_file in sorted(src_dir.iterdir()):
        if not src_file.is_file():
            continue
        inst_file = inst_dir / src_file.name
        if not inst_file.exists():
            console.print(f"[yellow]{src_file.name}:[/] instance file missing")
            any_diff = True
            continue

        src_lines = src_file.read_text().splitlines(keepends=True)
        inst_lines = inst_file.read_text().splitlines(keepends=True)
        diff = list(
            difflib.unified_diff(
                src_lines,
                inst_lines,
                fromfile=f"source/{src_file.name}",
                tofile=f"instance/{src_file.name}",
            )
        )
        if diff:
            any_diff = True
            console.print(f"\n[bold]{src_file.name}[/]")
            for line in diff:
                line = line.rstrip("\n")
                if line.startswith("+"):
                    console.print(f"[green]{line}[/]")
                elif line.startswith("-"):
                    console.print(f"[red]{line}[/]")
                else:
                    console.print(line)

    if not any_diff:
        console.print(f"[bold green]All prompts match source defaults for '{name}'.[/]")


@plugin.command("reset-prompts")
@click.argument("name")
@click.confirmation_option(prompt="Reset all prompts to source defaults?")
def plugin_reset_prompts(name: str) -> None:
    """Reset instance prompts to source defaults."""
    from src.plugins.loader import reset_prompts

    async def _run_reset():
        async with _get_plugin_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            return p["install_path"]

    try:
        install_path = _run(_run_reset())
        count = reset_prompts(install_path)
        console.print(f"[bold green]Reset {count} prompt(s) for '{name}'.[/]")
    except Exception as e:
        console.print(f"[bold red]Reset failed:[/] {e}")
        raise SystemExit(1)
