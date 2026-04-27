"""Hand-crafted CLI for editing the YAML config.

The auto-generator already exposes ``aq system get-config`` /
``aq system update-config``.  Those are awkward for interactive use, so
this module adds a friendlier ``aq system config {get,set,edit,schema}``
group with dotted-key set syntax and ``$EDITOR`` integration.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

import click

from .app import _get_client, _handle_errors, _run, cli, console


def _parse_yaml_scalar(text: str) -> Any:
    """Parse ``text`` as a YAML scalar so ``true``/``42``/``[a, b]`` etc. work."""
    import yaml

    return yaml.safe_load(text)


def _set_dotted(doc: dict, path: str, value: Any) -> None:
    """Apply ``value`` to ``doc`` at dotted ``path``, creating dicts as needed."""
    parts = path.split(".")
    cur = doc
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


_system_group = cli.commands.get("system")
if not isinstance(_system_group, click.Group):
    raise RuntimeError(
        "Expected `system` to be a click.Group registered by auto_commands; "
        "system_config must be imported after register_auto_commands()."
    )


@_system_group.group("config")
def system_config() -> None:
    """Read and edit the daemon's YAML config (~/.agent-queue/config.yaml)."""
    pass


@system_config.command("get")
@click.argument("section", required=False)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of YAML.")
@click.pass_context
@_handle_errors
def config_get(ctx: click.Context, section: str | None, as_json: bool) -> None:
    """Print the raw YAML config (optionally one section)."""
    import yaml

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _do() -> dict:
        async with _get_client(api_url) as client:
            return await client.execute("get_config", {"section": section} if section else {})

    result = _run(_do())
    config = result.get("config", {})
    if section:
        config = config.get(section)
    if as_json:
        console.print_json(data=config)
    else:
        console.print(yaml.safe_dump(config, sort_keys=False).rstrip())

    refs = [r for r in result.get("env_var_references", []) if not r.get("resolved")]
    if refs:
        console.print(f"[yellow]Warning:[/] {len(refs)} unresolved ${{ENV_VAR}} reference(s):")
        for r in refs:
            console.print(f"  • {r['path']} → ${{{r['var']}}}")


@system_config.command("set")
@click.argument("assignment")
@click.option("--dry-run", is_flag=True, help="Validate without writing.")
@click.pass_context
@_handle_errors
def config_set(ctx: click.Context, assignment: str, dry_run: bool) -> None:
    """Set one key by dotted path, e.g. ``aq system config set scheduling.rolling_window_hours=48``.

    The value is parsed as YAML, so ``=true``, ``=42``, ``=[a, b]`` all work.
    """
    if "=" not in assignment:
        raise click.UsageError("Expected KEY=VALUE (with at least one =).")
    key, _, raw_value = assignment.partition("=")
    key = key.strip()
    if not key or "." not in key:
        raise click.UsageError("KEY must be dotted, e.g. scheduling.rolling_window_hours.")
    value = _parse_yaml_scalar(raw_value)

    section, *_rest = key.split(".", 1)
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _do() -> dict:
        async with _get_client(api_url) as client:
            cur = await client.execute("get_config", {"section": section})
            section_data = cur.get("config", {}).get(section, {})
            if not isinstance(section_data, dict):
                section_data = {}
            # Apply the dotted change scoped *inside* the section.
            inner_path = key[len(section) + 1 :]
            _set_dotted(section_data, inner_path, value)
            return await client.execute(
                "update_config",
                {"section": section, "data": section_data, "dry_run": dry_run},
            )

    result = _run(_do())
    if result.get("validation_errors"):
        console.print("[red]Validation failed:[/]")
        for err in result["validation_errors"]:
            console.print(f"  • {err}")
        ctx.exit(1)
    if result.get("dry_run"):
        console.print(f"[green]OK (dry-run)[/] would set [cyan]{key}[/] = {value!r}")
        return
    if result.get("requires_restart"):
        console.print(
            f"[yellow]Saved[/] [cyan]{key}[/] = {value!r} — section "
            f"[bold]{section}[/] requires daemon restart."
        )
    else:
        console.print(f"[green]Saved + applied live[/] [cyan]{key}[/] = {value!r}")


@system_config.command("edit")
@click.pass_context
@_handle_errors
def config_edit(ctx: click.Context) -> None:
    """Open the full config in $EDITOR; on save, validate + apply.

    This is the power-user path for bulk changes. Comments INSIDE sections
    are NOT preserved by this path — use ``set`` for surgical edits.
    """
    import yaml

    api_url = ctx.obj.get("api_url") if ctx.obj else None
    editor = os.environ.get("EDITOR", "vi")

    async def _fetch() -> dict:
        async with _get_client(api_url) as client:
            return await client.execute("get_config", {})

    state = _run(_fetch())
    raw = state.get("config", {})

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, prefix="aq-config-"
    ) as tmp:
        yaml.safe_dump(raw, tmp, sort_keys=False)
        tmp_path = tmp.name

    try:
        rc = subprocess.call([editor, tmp_path])
        if rc != 0:
            console.print(f"[red]Editor exited with {rc}; aborting.[/]")
            ctx.exit(rc)
        with open(tmp_path) as f:
            new_doc = yaml.safe_load(f) or {}
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

    if new_doc == raw:
        console.print("[dim]No changes.[/]")
        return

    # Diff section-by-section and push each changed top-level section.
    changed = [k for k in set(raw) | set(new_doc) if raw.get(k) != new_doc.get(k)]

    async def _push() -> list[dict]:
        results = []
        async with _get_client(api_url) as client:
            for section in changed:
                data = new_doc.get(section)  # None → delete
                results.append(
                    await client.execute("update_config", {"section": section, "data": data})
                )
        return results

    results = _run(_push())
    for section, r in zip(changed, results, strict=True):
        if r.get("validation_errors"):
            console.print(f"[red]✗ {section}:[/] {'; '.join(r['validation_errors'])}")
        elif r.get("requires_restart"):
            console.print(f"[yellow]✓ {section}:[/] saved (restart required)")
        else:
            console.print(f"[green]✓ {section}:[/] applied live")


@system_config.command("schema")
@click.option("--json", "as_json", is_flag=True, default=True, help="Emit JSON.")
@click.pass_context
@_handle_errors
def config_schema(ctx: click.Context, as_json: bool) -> None:
    """Print the JSON Schema describing all config fields."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _do() -> dict:
        async with _get_client(api_url) as client:
            return await client.execute("get_config_schema", {})

    schema = _run(_do()).get("schema", {})
    console.print(json.dumps(schema, indent=2))
