"""Hand-crafted CLI for editing the YAML config.

The auto-generator already exposes ``aq system get-config`` /
``aq system update-config``.  Those are awkward for interactive use, so
this module adds a friendlier ``aq system config {get,set,edit,schema}``
group with dotted-key set syntax and ``$EDITOR`` integration.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

import click

from .app import _get_client, _handle_errors, _run, cli, console
from .envelope import emit, emit_error


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
        ctx.find_root().obj["json"] = True

    emit(
        ctx,
        config,
        legacy_data=config,
        render=lambda data: console.print(yaml.safe_dump(data, sort_keys=False).rstrip()),
    )

    hidden = [
        path
        for path in result.get("redacted", [])
        if not section or path == section or path.startswith(f"{section}.")
    ]
    if hidden:
        target_stderr = as_json or bool((ctx.obj or {}).get("json"))
        placeholder = result.get("secret_placeholder", "")
        click.echo(
            f"Note: {len(hidden)} literal credential(s) shown as {placeholder!r}; "
            "saving them unchanged keeps the stored value:",
            err=target_stderr,
        )
        for path in hidden:
            click.echo(f"  • {path}", err=target_stderr)

    refs = [r for r in result.get("env_var_references", []) if not r.get("resolved")]
    if refs:
        target_stderr = as_json or bool((ctx.obj or {}).get("json"))
        click.echo(
            f"Warning: {len(refs)} unresolved ${{ENV_VAR}} reference(s):",
            err=target_stderr,
        )
        for r in refs:
            click.echo(f"  • {r['path']} → ${{{r['var']}}}", err=target_stderr)


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
        if (ctx.obj or {}).get("json"):
            emit_error(
                "command_error",
                "configuration validation failed",
                {"validation_errors": result["validation_errors"]},
            )
            ctx.exit(1)
        console.print("[red]Validation failed:[/]")
        for err in result["validation_errors"]:
            console.print(f"  • {err}")
        ctx.exit(1)
    def _render(data: dict) -> None:
        if data.get("dry_run"):
            console.print(f"[green]OK (dry-run)[/] would set [cyan]{key}[/] = {value!r}")
        elif data.get("requires_restart"):
            console.print(
                f"[yellow]Saved[/] [cyan]{key}[/] = {value!r} — section "
                f"[bold]{section}[/] requires daemon restart."
            )
        else:
            console.print(f"[green]Saved + applied live[/] [cyan]{key}[/] = {value!r}")

    emit(ctx, result, render=_render)


@system_config.command("edit")
@click.pass_context
@_handle_errors
def config_edit(ctx: click.Context) -> None:
    """Open the full config in $EDITOR; on save, validate + apply.

    This is the power-user path for bulk changes. Comments INSIDE sections
    are NOT preserved by this path — use ``set`` for surgical edits.
    """
    import yaml

    if (ctx.obj or {}).get("json"):
        raise click.UsageError("system config edit is interactive and does not support JSON mode")

    api_url = ctx.obj.get("api_url") if ctx.obj else None
    editor = os.environ.get("EDITOR", "vi")

    async def _fetch() -> dict:
        async with _get_client(api_url) as client:
            return await client.execute("get_config", {})

    state = _run(_fetch())
    raw = state.get("config", {})

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, prefix="aq-config-", encoding="utf-8"
    ) as tmp:
        yaml.safe_dump(raw, tmp, sort_keys=False)
        tmp_path = tmp.name

    try:
        rc = subprocess.call([editor, tmp_path])
        if rc != 0:
            console.print(f"[red]Editor exited with {rc}; aborting.[/]")
            ctx.exit(rc)
        with open(tmp_path, encoding="utf-8") as f:
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
    if as_json:
        ctx.find_root().obj["json"] = True
    emit(ctx, schema)


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.agent-queue/config.yaml")


@system_config.command("tune")
@click.option("--apply", "apply_", is_flag=True, help="Write the recommendation (default: preview).")
@click.option(
    "--overwrite",
    is_flag=True,
    help="Replace sections you have already customized instead of keeping them.",
)
@click.option("--cores", type=int, help="Pretend the box has this many cores.")
@click.option("--memory-gb", type=float, help="Pretend the box has this much RAM.")
@click.option("--explain", is_flag=True, help="Print the reason and override for every key.")
@click.option(
    "--config",
    "config_path",
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Config file to read and write.",
)
@click.pass_context
@_handle_errors
def config_tune(
    ctx: click.Context,
    apply_: bool,
    overwrite: bool,
    cores: int | None,
    memory_gb: float | None,
    explain: bool,
    config_path: str,
) -> None:
    """Write resource-aware default tuning into the config file.

    Reads the machine, not the daemon: this is the install-time path, so it
    works before there is a database to talk to. Rationale for every value is
    in ``docs/guides/default-tuning.md`` and behind ``--explain``.
    """
    import yaml

    from src.config_editor import read_raw_config
    from src.config_tuning import (
        DERIVED_KEYS,
        MachineResources,
        apply_tuning,
        recommended_tuning,
        tuning_notes,
        tuning_plan,
    )

    detected = MachineResources.detect()
    machine = MachineResources(
        cores=cores or detected.cores,
        memory_gb=memory_gb if memory_gb is not None else detected.memory_gb,
    )

    if not os.path.exists(config_path):
        raise click.UsageError(
            f"No config file at {config_path}. Run `aq setup` (or create the file) first — "
            "tuning edits an existing config, it does not create one."
        )

    if apply_:
        result = apply_tuning(config_path, machine, overwrite=overwrite)
        result["machine"] = machine.as_dict()
        if result.get("validation_errors"):
            if (ctx.obj or {}).get("json"):
                emit_error(
                    "command_error",
                    "tuned configuration failed validation; nothing was written",
                    {"validation_errors": result["validation_errors"]},
                )
                ctx.exit(1)
            console.print("[red]Validation failed; nothing written:[/]")
            for err in result["validation_errors"]:
                console.print(f"  • {err}")
            ctx.exit(1)

        def _render_applied(data: dict) -> None:
            _print_machine(machine)
            for section in data.get("written", []):
                console.print(f"[green]✓ {section}[/] written")
            for section in data.get("kept", []):
                console.print(f"[yellow]• {section}[/] kept (already customized; --overwrite replaces it)")
            for err in data.get("preexisting_errors", []):
                console.print(f"[yellow]![/] pre-existing config problem: {err}")
            unchanged = data.get("unchanged", [])
            if unchanged:
                console.print(f"[dim]{len(unchanged)} section(s) already match the recommendation.[/]")
            if data.get("applied"):
                console.print(
                    "\n[dim]Restart the daemon to pick up sections that are not hot-reloadable.[/]"
                )

        emit(ctx, result, render=_render_applied)
        return

    plans = tuning_plan(read_raw_config(config_path), machine, overwrite=overwrite)
    payload = {
        "machine": machine.as_dict(),
        "config_path": config_path,
        "plan": [
            {"section": p.section, "action": p.action, "recommended": p.recommended}
            for p in plans
        ],
        "recommended": recommended_tuning(machine),
    }
    if explain:
        payload["notes"] = [
            {"key": n.key, "why": n.why, "override": n.override} for n in tuning_notes(machine)
        ]
        payload["derived"] = [
            {"key": n.key, "why": n.why, "override": n.override} for n in DERIVED_KEYS
        ]

    def _render_preview(data: dict) -> None:
        _print_machine(machine)
        for entry in data["plan"]:
            marker = {"add": "[green]+[/]", "replace": "[yellow]~[/]", "keep": "[dim]=[/]"}.get(
                entry["action"], "[dim]=[/]"
            )
            console.print(f"  {marker} {entry['section']} ({entry['action']})")
        console.print()
        console.print(yaml.safe_dump(data["recommended"], sort_keys=False).rstrip())
        if explain:
            console.print("\n[bold]Why these values[/]")
            for note in data["notes"]:
                console.print(f"  [cyan]{note['key']}[/]: {note['why']}")
                console.print(f"    [dim]override: {note['override']}[/]")
            console.print("\n[bold]Deliberately left derived[/]")
            for note in data["derived"]:
                console.print(f"  [cyan]{note['key']}[/]: {note['why']}")
                console.print(f"    [dim]override: {note['override']}[/]")
        console.print("\n[dim]Nothing written. Re-run with --apply.[/]")

    emit(ctx, payload, render=_render_preview)


def _print_machine(machine) -> None:
    console.print(
        f"[bold]{machine.cores} cores, {machine.memory_gb:.0f} GiB[/] → "
        f"{machine.size_class}: {machine.concurrent_agents} concurrent agent(s), "
        f"{machine.cpu_share} core(s) each, {machine.test_slots} test slot(s)\n"
    )


def _stdin_is_interactive() -> bool:
    """Whether a person is at the terminal (a seam for tests)."""
    import sys

    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _render_git_identity(data: dict) -> None:
    from src.install.git_identity import NOT_CONFIGURED_NOTE

    installation = data.get("installation")
    if installation:
        source = data.get("installation_source")
        origin = f" [dim]({source})[/]" if source else ""
        console.print(
            f"Installation default: [cyan]{installation['name']} <{installation['email']}>[/]"
            f"{origin}"
        )
    else:
        console.print(f"[yellow]{NOT_CONFIGURED_NOTE}[/]")
    effective = data.get("effective")
    if effective:
        console.print(
            f"Project {data.get('project_id')}: [cyan]{effective['name']} <{effective['email']}>[/]"
            f" [dim]({effective.get('source')})[/]"
        )


@system_config.command("git-identity")
@click.option("--name", help="Commit name (with --email).")
@click.option("--email", help="Commit email (with --name).")
@click.option(
    "--source",
    default=None,
    help="Where the identity came from, recorded with it (default: manual).",
)
@click.option("--clear", is_flag=True, help="Unset the default; AQ commits as the fallback.")
@click.option("--show", is_flag=True, help="Print the current default and exit.")
@click.option(
    "--project",
    "project_id",
    default=None,
    help="With --show: also print the identity this project's commits resolve to.",
)
@click.option(
    "--host",
    default="github.com",
    show_default=True,
    help="GitHub host whose gh account is suggested (interactive mode).",
)
@click.pass_context
@_handle_errors
def config_git_identity(
    ctx: click.Context,
    name: str | None,
    email: str | None,
    source: str | None,
    clear: bool,
    show: bool,
    project_id: str | None,
    host: str,
) -> None:
    """Show or set the Git commit identity AQ's commits use (``git_identity:``).

    \b
    aq system config git-identity                 # interactive: suggest, confirm or edit
    aq system config git-identity --show          # print it (the default without a terminal)
    aq system config git-identity --name "Ada Lovelace" --email ada@example.com
    aq system config git-identity --clear         # back to the fallback identity

    On a terminal with no options it shows the current default, suggests one
    from your authenticated `gh` account (public, verified or noreply email)
    and your global git config, and lets you confirm or edit it.  A project
    can override the default in its settings.
    """
    from src.git.identity import GitIdentity, GitIdentityError

    if (name is None) != (email is None):
        raise click.UsageError("--name and --email must be given together.")
    setting = name is not None
    if sum((setting, clear, show)) > 1:
        raise click.UsageError("Use one of --name/--email, --clear or --show.")
    if project_id and not show:
        raise click.UsageError("--project only applies to --show.")
    if setting:
        try:
            GitIdentity.parse(name, email)
        except GitIdentityError as error:
            raise click.BadParameter(error.message, param_hint=f"--{error.field}") from error

    api_url = ctx.obj.get("api_url") if ctx.obj else None
    json_mode = bool((ctx.obj or {}).get("json"))

    async def _execute(command: str, args: dict) -> dict:
        async with _get_client(api_url) as client:
            return await client.execute(command, args)

    def _set(args: dict) -> None:
        result = _run(_execute("set_git_identity", args))

        def _render(data: dict) -> None:
            installation = data.get("installation")
            if installation:
                verb = "Saved" if data.get("changed", True) else "Unchanged"
                console.print(
                    f"[green]{verb}[/] AQ commits as "
                    f"[cyan]{installation['name']} <{installation['email']}>[/]"
                )
                if data.get("applies_to"):
                    console.print(f"[dim]{data['applies_to']}[/]")
            else:
                from src.install.git_identity import NOT_CONFIGURED_NOTE

                console.print(f"[yellow]Cleared.[/] {NOT_CONFIGURED_NOTE}")

        emit(ctx, result, render=_render)

    if setting:
        _set({"name": name, "email": email, "source": source or "manual"})
        return
    if clear:
        _set({"clear": True})
        return

    current = _run(_execute("get_git_identity", {"project_id": project_id} if project_id else {}))
    if show or json_mode or not _stdin_is_interactive():
        emit(ctx, current, render=_render_git_identity)
        return

    from src.install.git_identity import discover, interview

    installation = current.get("installation") or None
    present = (
        GitIdentity(installation["name"], installation["email"]) if installation else None
    )
    click.echo("  (looking up your GitHub account with gh...)", err=True)
    choice = interview(
        discover(host=host),
        current=present,
        current_source=str(current.get("installation_source") or ""),
    )
    if choice is None:
        console.print("[dim]No change.[/]")
        return
    _set({"name": choice.name, "email": choice.email, "source": choice.source})
