"""``aq playbook`` — CLI group for playbooks.

Most subcommands are auto-generated from the CommandHandler tool registry via
``register_auto_commands``; this module anchors the group and holds the
hand-crafted subcommands that need custom exit-code / output shaping:

* ``aq playbook import --path reviewed-playbooks/<id> [--activate]`` — import
  one reviewed Playbook V2 bundle from the vault (``playbook_v2_import``) and,
  only when asked, activate the exact hash it stored (``playbook_activate``).
"""
from __future__ import annotations

import click

from .app import _get_client, _handle_errors, _run, cli, console
from .envelope import emit


@cli.group("playbook")
def playbook_group():
    """Playbook commands."""


def _activate_command(imported: dict) -> str:
    return (
        f"aq playbook activate --playbook-id {imported['playbook_id']} "
        f"--artifact-sha256 {imported['artifact_sha256']}"
    )


def _render_import(result: dict) -> None:
    scope = result.get("scope") or "?"
    if result.get("scope_identifier"):
        scope = f"{scope} {result['scope_identifier']}"
    # soft_wrap: the hash and the command stay whole lines, so they copy-paste.
    console.print(
        f"Imported [bold]{result['playbook_id']}[/bold] ({scope}) as {result['artifact_sha256']}",
        soft_wrap=True,
    )
    activation = result.get("activation")
    if activation is None:
        console.print("Not activated. Activate it with:")
        console.print(f"  {_activate_command(result)}", soft_wrap=True, markup=False)
    elif activation.get("blocked"):
        console.print("[red]Activation refused:[/red]")
        for blocker in activation.get("blockers") or []:
            console.print(f"  - {blocker}", markup=False)
    else:
        console.print("Activated.")


@playbook_group.command("import")
@click.option(
    "--path",
    "path",
    required=True,
    help=(
        "Reviewed bundle directory inside the vault, vault-relative "
        "(reviewed-playbooks/<id>) or absolute."
    ),
)
@click.option(
    "--activate",
    is_flag=True,
    default=False,
    help="Also activate the imported artifact hash (default: import only).",
)
@click.pass_context
@_handle_errors
def playbook_import(ctx: click.Context, path: str, activate: bool) -> None:
    """Import a reviewed Playbook V2 bundle from the vault.

    The bundle directory holds artifact.json, artifact.sha256, source.md and
    manifest.md.  The daemon checks the canonical bytes, the digests and the
    manifest, revalidates the artifact against the live command, profile and
    event registries, and stores it.  Import never activates: pass --activate
    to activate the exact hash it stored, or run the printed command later.
    """

    async def run() -> dict:
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            imported = await client.execute("playbook_v2_import", {"path": path})
            if not activate:
                return imported
            activation = await client.execute(
                "playbook_activate",
                {
                    "playbook_id": imported["playbook_id"],
                    "artifact_sha256": imported["artifact_sha256"],
                },
            )
            return {**imported, "activated": not activation.get("blocked"), "activation": activation}

    result = _run(run())
    emit(ctx, result, render=_render_import)
    if activate and not result.get("activated"):
        raise SystemExit(1)
